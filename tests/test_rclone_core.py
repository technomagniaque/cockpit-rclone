"""Tests unitaires de backend/rclone_core.py (profils GDrive, ZFS, montages Z640, sécurité)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import rclone_core as core  # noqa: E402


class ValidationTest(unittest.TestCase):
    def test_remote_name_validation(self):
        self.assertEqual(core.validate_remote_name("gdrive-nas_1.0"), "gdrive-nas_1.0")
        for bad in ("", "../etc", "-flag", "a b", "foo:bar", "a;rm"):
            with self.assertRaises(ValueError, msg=bad):
                core.validate_remote_name(bad)

    def test_job_id_validation(self):
        self.assertEqual(core.validate_job_id("NAS-P1_Sync"), "nas-p1_sync")
        for bad in ("", "../x", "a b", "x;y", "-bad"):
            with self.assertRaises(ValueError, msg=bad):
                core.validate_job_id(bad)

    def test_bwlimit_simple_and_scheduled(self):
        self.assertEqual(core.validate_bwlimit("8.5M"), "8.5M")
        self.assertEqual(core.validate_bwlimit("08:00,5M 23:00,off"), "08:00,5M 23:00,off")
        self.assertEqual(core.validate_bwlimit("Mon-08:00,2M:10M"), "Mon-08:00,2M:10M")
        for bad in ("8M;id", "$(whoami)", "99:99,5M"):
            with self.assertRaises(ValueError, msg=bad):
                core.validate_bwlimit(bad)

    def test_path_rejects_control_chars_and_leading_dash(self):
        self.assertEqual(core.validate_path("/H1/p1"), "/H1/p1")
        self.assertEqual(core.validate_path("gdrive:NAS/p1"), "gdrive:NAS/p1")
        for bad in ("", "-rf", "/H1/a\nb"):
            with self.assertRaises(ValueError, msg=bad):
                core.validate_path(bad)


class OAuthAndSecretsTest(unittest.TestCase):
    def test_parse_mygdoc_and_google_cloud_json(self):
        cid, csec = core.parse_google_oauth_json('{"clientId": "123.apps.googleusercontent.com", "clientSecret": "FAKE-SEC"}')
        self.assertEqual((cid, csec), ("123.apps.googleusercontent.com", "FAKE-SEC"))

        cid2, csec2 = core.parse_google_oauth_json('{"installed": {"client_id": "abc.apps", "client_secret": "SEC2"}}')
        self.assertEqual((cid2, csec2), ("abc.apps", "SEC2"))

        with self.assertRaises(ValueError):
            core.parse_google_oauth_json('{"foo": "bar"}')

    def test_parse_rclone_conf_masks_all_secrets(self):
        conf = (
            "[gdrive]\n"
            "type = drive\n"
            "client_id = 123.apps.googleusercontent.com\n"
            "client_secret = SUPER_SECRET_VALUE\n"
            "scope = drive\n"
            'token = {"access_token":"TOK","refresh_token":"REF"}\n'
        )
        remotes = core.parse_rclone_conf(conf, mask_secrets=True)
        self.assertEqual(len(remotes), 1)
        r = remotes[0]
        self.assertTrue(r["has_own_client_id"])
        self.assertEqual(r["options"]["client_id"], "123.apps.googleusercontent.com")
        self.assertEqual(r["options"]["client_secret"], core.MASKED_VALUE)
        self.assertEqual(r["options"]["token"], core.MASKED_VALUE)
        self.assertNotIn("SUPER_SECRET_VALUE", str(r))
        self.assertNotIn("REF", str(r))


class ZfsHelpersTest(unittest.TestCase):
    def test_resolve_most_specific_zfs_dataset(self):
        raw = (
            "H1\t20.2T\t1.06T\t/H1\n"
            "H1/d1\t19.3T\t885G\t/H1/d1\n"
            "H1/p1\t91.5G\t885G\t/H1/p1\n"
        )
        datasets = core.parse_zfs_list(raw)
        self.assertEqual(
            core.resolve_zfs_dataset("/H1/p1/photos/2026", datasets),
            ("H1/p1", "/H1/p1", "photos/2026"),
        )
        self.assertEqual(
            core.resolve_zfs_dataset("/H1/d1", datasets),
            ("H1/d1", "/H1/d1", ""),
        )
        self.assertIsNone(core.resolve_zfs_dataset("gdrive:NAS", datasets))
        self.assertIsNone(core.resolve_zfs_dataset("/var/tmp", datasets))
        self.assertEqual(
            core.snapshot_source_path("/H1/p1", "cockpit-rclone-j1-1", "photos"),
            "/H1/p1/.zfs/snapshot/cockpit-rclone-j1-1/photos",
        )


class ArgvBuildTest(unittest.TestCase):
    def test_gdrive_heavy_profile_flags_and_backup_dir(self):
        job = {
            "id": "nas-p1",
            "mode": "sync",
            "profile": "gdrive-nas-heavy",
            "source": "/H1/p1",
            "destination": "gdrive:NAS/p1",
            "excludes": [".DS_Store", "*.tmp"],
            "options": {"backup_dir": "gdrive:_archive/p1", "checksum": True},
        }
        argv = core.build_job_argv(
            job,
            config_path="/root/.config/rclone/rclone.conf",
            dry_run=True,
            rc_addr="127.0.0.1:55555",
            rc_user="cockpit",
            rc_pass="rnd",
            backup_dir_suffix="2026-11-23_20-00-00",
        )
        self.assertEqual(argv[:4], ["rclone", "sync", "--config", "/root/.config/rclone/rclone.conf"])
        self.assertIn("--dry-run", argv)
        self.assertIn("--drive-chunk-size", argv)
        self.assertIn("128M", argv)
        self.assertIn("--drive-stop-on-upload-limit", argv)
        self.assertIn("--fast-list", argv)
        self.assertIn("--drive-use-trash=true", argv)
        self.assertIn("--checksum", argv)
        self.assertIn("--backup-dir", argv)
        self.assertIn("gdrive:_archive/p1/2026-11-23_20-00-00", argv)
        self.assertIn("--exclude", argv)
        self.assertIn(".DS_Store", argv)

    def test_z640_desktop_mount_profile(self):
        mount = {
            "id": "gdrive-z640",
            "profile": "gdrive-z640-desktop",
            "remote_path": "gdrive:",
            "mountpoint": "/home/eti/GoogleDrive",
            "cache_dir": "/var/cache/cockpit-rclone",
            "uid": 1000,
            "gid": 1000,
        }
        argv = core.build_mount_argv(mount, config_path="/root/.config/rclone/rclone.conf")
        self.assertEqual(argv[:4], ["rclone", "mount", "gdrive:", "/home/eti/GoogleDrive"])
        self.assertIn("--vfs-cache-mode", argv)
        self.assertIn("full", argv)
        self.assertIn("--vfs-cache-max-size", argv)
        self.assertIn("20G", argv)
        self.assertIn("--vfs-write-back", argv)
        self.assertIn("5s", argv)
        self.assertIn("--poll-interval", argv)
        self.assertIn("30s", argv)
        self.assertIn("--uid", argv)
        self.assertIn("1000", argv)
        self.assertIn("--allow-other", argv)

    def test_render_timer_unit(self):
        unit = core.render_timer_unit("nas-p1", "*-*-* 01:00:00", "5m")
        self.assertIn("OnCalendar=*-*-* 01:00:00", unit)
        self.assertIn("Unit=cockpit-rclone-job@nas-p1.service", unit)
        self.assertIn("Persistent=true", unit)


if __name__ == "__main__":
    unittest.main()
