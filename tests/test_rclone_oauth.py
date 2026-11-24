"""Tests unitaires pour le flux OAuth2 Google Drive direct dans cockpit-rclone."""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

import rclone_core as core  # noqa: E402
import rclone_ctl as ctl  # noqa: E402
import rclone_oauth_listener as listener  # noqa: E402


class TestGDriveOAuthCore(unittest.TestCase):
    def test_pkce_and_auth_url(self) -> None:
        verifier, challenge = core.generate_pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertNotIn("=", challenge)

        url = core.build_gdrive_oauth_url(
            client_id="123456.apps.googleusercontent.com",
            state="state-abc123",
            code_challenge=challenge,
            scope="drive",
        )
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "accounts.google.com")
        self.assertEqual(parsed.path, "/o/oauth2/v2/auth")
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs["client_id"], ["123456.apps.googleusercontent.com"])
        self.assertEqual(qs["redirect_uri"], ["http://127.0.0.1:53682/"])
        self.assertEqual(qs["scope"], ["https://www.googleapis.com/auth/drive"])
        self.assertEqual(qs["access_type"], ["offline"])
        self.assertEqual(qs["prompt"], ["consent"])
        self.assertEqual(qs["code_challenge_method"], ["S256"])

    def test_parse_callback_url_and_raw_code(self) -> None:
        code, st = core.parse_oauth_callback_input("http://127.0.0.1:53682/?state=xyz99&code=4/0Atestcode&scope=drive")
        self.assertEqual(code, "4/0Atestcode")
        self.assertEqual(st, "xyz99")

        code2, st2 = core.parse_oauth_callback_input("4/0Arawcodeonly")
        self.assertEqual(code2, "4/0Arawcodeonly")
        self.assertIsNone(st2)

        with self.assertRaises(ValueError):
            core.parse_oauth_callback_input("http://127.0.0.1:53682/?error=access_denied")

    def test_format_rclone_token_and_has_token_flag(self) -> None:
        now = datetime.datetime(2026, 11, 23, 22, 0, 0, tzinfo=datetime.timezone.utc)
        tok = core.format_rclone_oauth_token(
            {"access_token": "ya29.fake", "token_type": "Bearer", "refresh_token": "1//fake", "expires_in": 3600},
            now=now,
        )
        obj = json.loads(tok)
        self.assertEqual(obj["access_token"], "ya29.fake")
        self.assertEqual(obj["refresh_token"], "1//fake")
        self.assertEqual(obj["expiry"], "2026-11-23T23:00:00.000000Z")

        conf = core.dump_rclone_conf({
            "t1": {"type": "drive", "client_id": "abc"},
            "t2": {"type": "drive", "client_id": "abc", "token": tok},
        })
        remotes = {r["name"]: r for r in core.parse_rclone_conf(conf, mask_secrets=True)}
        self.assertFalse(remotes["t1"]["has_token"])
        self.assertTrue(remotes["t2"]["has_token"])
        self.assertEqual(remotes["t2"]["options"]["token"], core.MASKED_VALUE)


class TestGDriveOAuthFlowEndToEnd(unittest.TestCase):
    def test_start_status_and_finish_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            oauth_file = t / "oauth-credentials.json"
            oauth_file.write_text(
                json.dumps({"clientId": "999999.apps.googleusercontent.com", "clientSecret": "fake-secret-val"}),
                encoding="utf-8",
            )
            old_etc, old_run, old_cfg = ctl.ETC_DIR, ctl.RUN_DIR, ctl.DEFAULT_RCLONE_CONF
            old_env_files = os.environ.get("COCKPIT_RCLONE_OAUTH_FILES")
            old_no_listener = os.environ.get("COCKPIT_RCLONE_NO_OAUTH_LISTENER")
            try:
                ctl.ETC_DIR = t / "etc"
                ctl.RUN_DIR = t / "run"
                ctl.DEFAULT_RCLONE_CONF = str(t / "rclone.conf")
                os.environ["COCKPIT_RCLONE_OAUTH_FILES"] = str(oauth_file)
                os.environ["COCKPIT_RCLONE_NO_OAUTH_LISTENER"] = "1"

                started = ctl.cmd_gdrive_oauth_start({"name": "t1", "use_oauth_file": str(oauth_file)})
                self.assertTrue(started["ok"])
                self.assertTrue(started["auth_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
                self.assertNotIn("fake-secret-val", json.dumps(started))

                st1 = ctl.cmd_gdrive_oauth_status(started["session_id"])
                self.assertEqual(st1["status"], "pending")
                self.assertNotIn("fake-secret-val", json.dumps(st1))

                # Récupère le state depuis auth_url comme le ferait Google
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(started["auth_url"]).query)
                oauth_state = qs["state"][0]

                session_path = ctl.RUN_DIR / f"oauth-{started['session_id']}.json"

                def fake_exchange(**kwargs):
                    self.assertEqual(kwargs["code"], "4/0AfakeCode")
                    self.assertEqual(kwargs["client_id"], "999999.apps.googleusercontent.com")
                    self.assertEqual(kwargs["client_secret"], "fake-secret-val")
                    return {
                        "access_token": "ya29.access",
                        "refresh_token": "1//refresh",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }

                done = listener.exchange_and_store_oauth_code(
                    session_path,
                    "4/0AfakeCode",
                    oauth_state,
                    exchange_fn=fake_exchange,
                )
                self.assertTrue(done["ok"])
                self.assertEqual(done["remote"], "t1")

                # Vérifie que le token est écrit dans rclone.conf en mode 0600 et les secrets purgés de la session
                cfg_path = Path(ctl.DEFAULT_RCLONE_CONF)
                self.assertEqual(cfg_path.stat().st_mode & 0o777, 0o600)
                parsed = core.parse_rclone_conf(cfg_path.read_text(encoding="utf-8"), mask_secrets=False)
                self.assertEqual(len(parsed), 1)
                self.assertTrue(parsed[0]["has_token"])
                tok_obj = json.loads(parsed[0]["options"]["token"])
                self.assertEqual(tok_obj["refresh_token"], "1//refresh")

                st2 = ctl.cmd_gdrive_oauth_status(started["session_id"])
                self.assertEqual(st2["status"], "completed")
                session_after = json.loads(session_path.read_text(encoding="utf-8"))
                self.assertNotIn("client_secret", session_after)
                self.assertNotIn("code_verifier", session_after)
            finally:
                ctl.ETC_DIR, ctl.RUN_DIR, ctl.DEFAULT_RCLONE_CONF = old_etc, old_run, old_cfg
                if old_env_files is None:
                    os.environ.pop("COCKPIT_RCLONE_OAUTH_FILES", None)
                else:
                    os.environ["COCKPIT_RCLONE_OAUTH_FILES"] = old_env_files
                if old_no_listener is None:
                    os.environ.pop("COCKPIT_RCLONE_NO_OAUTH_LISTENER", None)
                else:
                    os.environ["COCKPIT_RCLONE_NO_OAUTH_LISTENER"] = old_no_listener


if __name__ == "__main__":
    unittest.main()
