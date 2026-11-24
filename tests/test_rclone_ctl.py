"""Tests d'intégration de backend/rclone_ctl.py (faux rclone/zfs/systemctl, zéro effet hôte)."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))
import rclone_ctl as ctl  # noqa: E402


class RcloneCtlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.etc = self.root / "etc"
        self.run_dir = self.root / "run"
        self.systemd = self.root / "systemd"
        self.systemd.mkdir()
        self.rclone_conf = self.root / "rclone.conf"
        self.fuse_conf = self.root / "fuse.conf"
        self.fuse_conf.write_text("user_allow_other\n", encoding="utf-8")
        self.oauth_file = self.root / "oauth-credentials.json"
        self.oauth_file.write_text(
            json.dumps({"clientId": "9999999999-test.apps.googleusercontent.com", "clientSecret": "SECRET_TEST_123"}),
            encoding="utf-8",
        )
        os.chmod(self.oauth_file, 0o600)

        self.patches = [
            mock.patch.object(ctl, "ETC_DIR", self.etc),
            mock.patch.object(ctl, "RUN_DIR", self.run_dir),
            mock.patch.object(ctl, "SYSTEMD_DIR", self.systemd),
            mock.patch.object(ctl, "DEFAULT_RCLONE_CONF", str(self.rclone_conf)),
            mock.patch.object(ctl, "FUSE_CONF", self.fuse_conf),
            mock.patch.dict(os.environ, {"COCKPIT_RCLONE_OAUTH_FILES": str(self.oauth_file)}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_overview_detects_oauth_without_exposing_secret(self):
        ov = ctl.cmd_overview()
        self.assertTrue(ov["system"]["fuse_allow_other"])
        paths = [x["path"] for x in ov["oauth_detected"]]
        self.assertIn(str(self.oauth_file), paths)
        self.assertNotIn("SECRET_TEST_123", json.dumps(ov))

    def test_remote_save_imports_oauth_file_server_side_and_chmod_600(self):
        res = ctl.cmd_remote_save({
            "name": "gdrive",
            "type": "drive",
            "use_oauth_file": str(self.oauth_file),
            "options": {"scope": "drive", "token": '{"access_token":"A","refresh_token":"R"}'},
        })
        self.assertTrue(res["ok"])
        mode = stat.S_IMODE(self.rclone_conf.stat().st_mode)
        self.assertEqual(mode, 0o600)
        raw = self.rclone_conf.read_text(encoding="utf-8")
        self.assertIn("9999999999-test.apps.googleusercontent.com", raw)
        self.assertIn("SECRET_TEST_123", raw)

        # Mise à jour sans resaisir le secret : préserve l'existant
        ctl.cmd_remote_save({
            "name": "gdrive",
            "type": "drive",
            "options": {"scope": "drive.readonly", "client_id": "9999999999-test.apps.googleusercontent.com", "client_secret": "", "token": ""},
        })
        raw2 = self.rclone_conf.read_text(encoding="utf-8")
        self.assertIn("SECRET_TEST_123", raw2)
        self.assertIn("drive.readonly", raw2)

        # Overview masque le secret et le token
        ov = ctl.cmd_overview()
        self.assertNotIn("SECRET_TEST_123", json.dumps(ov))
        self.assertEqual(ov["remotes"][0]["options"]["client_secret"], "***CONFIGURÉ***")

    def test_job_mount_serve_and_timer_lifecycle(self):
        with mock.patch.object(ctl.shutil, "which", return_value=None):
            res = ctl.cmd_job_save({
                "id": "nas-p1-gdrive",
                "name": "Sauvegarde H1/p1",
                "mode": "sync",
                "profile": "gdrive-nas-heavy",
                "source": "/H1/p1",
                "destination": "gdrive:NAS/p1",
                "zfs_snapshot": True,
                "excludes": ["*.tmp"],
                "options": {"backup_dir": "gdrive:_archive/p1"},
            })
            self.assertTrue(res["ok"])
            self.assertIn("--drive-chunk-size", res["argv_preview"])

            tres = ctl.cmd_timer_set({"id": "nas-p1-gdrive", "schedule": "*-*-* 01:00:00", "enabled": True})
            self.assertTrue(tres["ok"])
            timer_file = self.systemd / "cockpit-rclone-job@nas-p1-gdrive.timer"
            self.assertTrue(timer_file.is_file())
            self.assertIn("OnCalendar=*-*-* 01:00:00", timer_file.read_text(encoding="utf-8"))

            mres = ctl.cmd_mount_save({
                "id": "gdrive-z640",
                "profile": "gdrive-z640-desktop",
                "remote_path": "gdrive:",
                "mountpoint": "/home/eti/GoogleDrive",
                "uid": 1000,
                "gid": 1000,
            })
            self.assertTrue(mres["ok"])
            self.assertIn("--vfs-cache-mode", mres["argv_preview"])

            sres = ctl.cmd_serve_save({
                "id": "webdav1",
                "protocol": "webdav",
                "remote_path": "gdrive:",
                "addr": "127.0.0.1:8088",
                "user": "eti",
                "pass": "MOTDEPASSE",
            })
            self.assertEqual(sres["serve"]["pass"], "***CONFIGURÉ***")

            # Lecture des logs refuse une unité non-cockpit-rclone
            with self.assertRaises(ValueError):
                ctl.cmd_logs("sshd.service")
            with self.assertRaises(ValueError):
                ctl.cmd_logs("cockpit-rclone-job@a;id.service")

            ctl.cmd_timer_remove("nas-p1-gdrive")
            self.assertFalse(timer_file.exists())
            ctl.cmd_job_delete("nas-p1-gdrive")
            ctl.cmd_mount_delete("gdrive-z640")
            ctl.cmd_serve_delete("webdav1")

    def test_stdin_dispatch(self):
        fake_stdin = io.StringIO(json.dumps({"oauth_path": str(self.oauth_file)}))
        with mock.patch.object(sys, "stdin", fake_stdin):
            res = ctl.dispatch(["gdrive-auth-hint"])
        self.assertTrue(res["ok"])
        self.assertIn("rclone", res["command"])
        self.assertNotIn("SECRET_TEST_123", res["command"])


if __name__ == "__main__":
    unittest.main()
