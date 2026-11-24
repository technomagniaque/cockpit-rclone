"""Tests des corrections de sécurité de rclone serve et d'OAuth (vérification Claude du 2026-11-24, R1 à R4)."""
import json
import os
import pwd
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))
import rclone_core as core  # noqa: E402
import rclone_job_runner as runner  # noqa: E402
import rclone_oauth_listener as listener  # noqa: E402

ROOTS = ["/H1/p1", "/H1/d1"]


def serve(**kw):
    base = {"protocol": "webdav", "remote_path": "gdrive:", "addr": "127.0.0.1:8088"}
    base.update(kw)
    return base


class AddrTest(unittest.TestCase):
    def test_loopback(self):
        for a in ("127.0.0.1:8088", "localhost:80", "[::1]:8080", "127.1.2.3:9"):
            self.assertTrue(core.addr_is_loopback(a), a)
        for a in ("0.0.0.0:8088", ":8088", "192.168.0.9:8088", "[::]:8080"):
            self.assertFalse(core.addr_is_loopback(a), a)


class R1ServeSecurityTest(unittest.TestCase):
    def test_lan_sans_authentification_refuse(self):
        for addr in ("0.0.0.0:8088", ":8088", "192.168.0.9:8088"):
            with self.assertRaises(ValueError, msg=addr):
                core.build_serve_argv(serve(addr=addr), config_path="/c", allowed_roots=ROOTS)

    def test_lecture_seule_par_defaut(self):
        argv = core.build_serve_argv(serve(), config_path="/c", allowed_roots=ROOTS)
        self.assertIn("--read-only", argv)
        argv = core.build_serve_argv(serve(read_only=False), config_path="/c", allowed_roots=ROOTS)
        self.assertNotIn("--read-only", argv)

    def test_dlna_reseau_exige_accord_et_reste_en_lecture_seule(self):
        with self.assertRaises(ValueError):
            core.build_serve_argv(serve(protocol="dlna", addr="0.0.0.0:8200"), config_path="/c", allowed_roots=ROOTS)
        argv = core.build_serve_argv(
            serve(protocol="dlna", addr="0.0.0.0:8200", dlna_lan_ack=True, read_only=False),
            config_path="/c", allowed_roots=ROOTS,
        )
        self.assertIn("--read-only", argv)

    def test_dossiers_locaux(self):
        ok = core.build_serve_argv(serve(remote_path="/H1/p1/photos"), config_path="/c", allowed_roots=ROOTS)
        self.assertIn("/H1/p1/photos", ok)
        for bad in ("/", "/etc", "/root/.ssh", "/H1", "/H1/p1/../../etc", "etc", ":local:/", ":sftp,host=x:/"):
            with self.assertRaises(ValueError, msg=bad):
                core.build_serve_argv(serve(remote_path=bad), config_path="/c", allowed_roots=ROOTS)

    def test_racine_systeme_jamais_autorisee_meme_si_listee(self):
        self.assertEqual(core.serve_allowed_roots([{"mountpoint": "/"}, {"mountpoint": "/H1/p1"}], ["/etc"]), ["/H1/p1"])
        with self.assertRaises(ValueError):
            core.build_serve_argv(serve(remote_path="/etc"), config_path="/c", allowed_roots=["/", "/etc"])

    def test_lien_symbolique_hors_du_dataset_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = Path(tmp) / "H1"
            ds.mkdir()
            (ds / "fuite").symlink_to("/etc")
            with self.assertRaises(ValueError):
                core.build_serve_argv(serve(remote_path=str(ds / "fuite")), config_path="/c", allowed_roots=[str(ds)])

    def test_jamais_root(self):
        with self.assertRaises(ValueError):
            core.build_serve_argv(serve(run_as="root"), config_path="/c", allowed_roots=ROOTS)
        self.assertEqual(core.validate_run_as(""), "cockpit-rclone")


class R2PasswordTest(unittest.TestCase):
    def test_mot_de_passe_jamais_dans_argv(self):
        s = serve(addr="0.0.0.0:8088", user="eti", **{"pass": "SECRETXYZ"})
        argv = core.build_serve_argv(s, config_path="/c", allowed_roots=ROOTS)
        self.assertNotIn("SECRETXYZ", " ".join(argv))
        self.assertNotIn("--pass", argv)
        self.assertEqual(core.serve_env(s), {"RCLONE_USER": "eti", "RCLONE_PASS": "SECRETXYZ"})

    @unittest.skipUnless(os.geteuid() == 0, "chown exige root")
    def test_runner_perd_les_droits_root_et_passe_le_mot_de_passe_par_env(self):
        nobody = pwd.getpwnam("nobody")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rclone.conf").write_text("[gdrive]\ntype = drive\n")
            (root / "etc").mkdir()
            (root / "etc" / "state.json").write_text(json.dumps({
                "config_path": str(root / "rclone.conf"),
                "serves": {"w1": {"protocol": "webdav", "remote_path": "gdrive:", "addr": "0.0.0.0:8088",
                                  "user": "eti", "pass": "SECRETXYZ", "run_as": "nobody"}},
            }))
            captured = {}

            class FakeProc:
                def __init__(self, argv, **kw):
                    captured["argv"], captured["kw"] = argv, kw

                def wait(self):
                    return 0

                def poll(self):
                    return 0

            with mock.patch.object(runner, "ETC_DIR", root / "etc"), \
                 mock.patch.object(runner, "RUN_DIR", root / "run"), \
                 mock.patch.object(runner, "_list_zfs_datasets", return_value=[]), \
                 mock.patch.object(runner.subprocess, "Popen", FakeProc):
                self.assertEqual(runner.run_serve("w1"), 0)

            self.assertEqual(captured["kw"]["user"], nobody.pw_uid)
            self.assertEqual(captured["kw"]["extra_groups"], [])
            self.assertEqual(captured["kw"]["env"]["RCLONE_PASS"], "SECRETXYZ")
            self.assertNotIn("SECRETXYZ", " ".join(captured["argv"]))
            copy = root / "run" / "serve-w1" / "rclone.conf"
            self.assertIn(str(copy), captured["argv"])  # copie privée, pas le vrai rclone.conf
            st = copy.stat()
            self.assertEqual((st.st_uid, st.st_mode & 0o777), (nobody.pw_uid, 0o600))

    def test_runner_refuse_un_serveur_ouvert_sans_mot_de_passe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc").mkdir()
            (root / "etc" / "state.json").write_text(json.dumps({
                "serves": {"w2": {"protocol": "sftp", "remote_path": "/", "addr": "0.0.0.0:2022"}},
            }))
            with mock.patch.object(runner, "ETC_DIR", root / "etc"), \
                 mock.patch.object(runner, "RUN_DIR", root / "run"), \
                 mock.patch.object(runner, "_list_zfs_datasets", return_value=[]), \
                 mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("ne doit pas démarrer")):
                self.assertEqual(runner.run_serve("w2"), 2)


class R3UnitsTest(unittest.TestCase):
    def test_unites_durcies(self):
        units = Path(__file__).resolve().parent.parent / "systemd"
        serve_unit = (units / "cockpit-rclone-serve@.service").read_text()
        for line in ("NoNewPrivileges=yes", "ProtectSystem=full", "CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_CHOWN"):
            self.assertIn(line, serve_unit)
        for name in ("job", "job-dry", "mount"):
            u = (units / f"cockpit-rclone-{name}@.service").read_text()
            self.assertIn("NoNewPrivileges=yes", u)
            self.assertNotIn("ProtectSystem", u)  # pas d'espace de noms : snapshots ZFS et FUSE visibles


class R4OAuthStateTest(unittest.TestCase):
    def _session(self, tmp: str) -> Path:
        p = Path(tmp) / "oauth-s.json"
        p.write_text(json.dumps({"state": "ETAT-ATTENDU", "expires_at": 9e12, "status": "pending",
                                 "client_id": "x", "client_secret": "y", "remote": "g",
                                 "config_path": str(Path(tmp) / "rclone.conf"), "code_verifier": "v",
                                 "redirect_uri": "http://127.0.0.1:53682/"}))
        return p

    def test_rappel_sans_state_refuse_sur_l_ecouteur(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "state"):
                listener.exchange_and_store_oauth_code(self._session(tmp), "code", None,
                                                       exchange_fn=lambda **k: self.fail("échange interdit"))

    def test_state_faux_refuse_meme_en_saisie_manuelle(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "state"):
                listener.exchange_and_store_oauth_code(self._session(tmp), "code", "AUTRE", allow_missing_state=True,
                                                       exchange_fn=lambda **k: self.fail("échange interdit"))


if __name__ == "__main__":
    unittest.main()
