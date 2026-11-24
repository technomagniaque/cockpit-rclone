"""Tests de backend/rclone_job_runner.py (snapshot ZFS atomique + cleanup garanti + runtime 0600)."""
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
import rclone_job_runner as runner  # noqa: E402


class JobRunnerTest(unittest.TestCase):
    def test_zfs_snapshot_created_used_and_destroyed_even_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            etc = root / "etc"
            run_dir = root / "run"
            etc.mkdir()
            (etc / "state.json").write_text(json.dumps({
                "config_path": str(root / "rclone.conf"),
                "jobs": {
                    "nas-p1": {
                        "id": "nas-p1",
                        "mode": "sync",
                        "profile": "gdrive-nas-heavy",
                        "source": "/H1/p1/docs",
                        "destination": "gdrive:NAS/p1/docs",
                        "zfs_snapshot": True,
                        "options": {},
                    },
                },
            }), encoding="utf-8")

            calls: list[list[str]] = []
            seen_runtime_modes: list[int] = []

            def fake_run(cmd, **kw):
                calls.append(list(cmd))
                if cmd[:2] == ["zfs", "list"]:
                    return mock.Mock(returncode=0, stdout="H1/p1\t91G\t885G\t/H1/p1\n", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            def fake_run_sub(argv):
                calls.append(list(argv))
                rt_file = run_dir / "job-nas-p1.json"
                self.assertTrue(rt_file.is_file())
                seen_runtime_modes.append(stat.S_IMODE(rt_file.stat().st_mode))
                rt = json.loads(rt_file.read_text(encoding="utf-8"))
                self.assertTrue(rt["rc_addr"].startswith("127.0.0.1:"))
                self.assertTrue(rt["rc_pass"])
                # Vérifie que la source passée à rclone pointe dans .zfs/snapshot/...
                self.assertTrue(any("/H1/p1/.zfs/snapshot/cockpit-rclone-nas-p1-" in a for a in argv), argv)
                return 1  # simule un échec rclone

            with mock.patch.object(runner, "ETC_DIR", etc), \
                 mock.patch.object(runner, "RUN_DIR", run_dir), \
                 mock.patch.object(runner.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(runner, "_run_subprocess", side_effect=fake_run_sub):
                rc = runner.run_job("nas-p1", dry_run=False)

            self.assertEqual(rc, 1)
            self.assertEqual(seen_runtime_modes, [0o600])
            # Le fichier runtime doit être nettoyé
            self.assertFalse((run_dir / "job-nas-p1.json").exists())
            # Le snapshot ZFS doit avoir été créé PUIS détruit malgré l'échec
            snap_creates = [c for c in calls if c[:2] == ["zfs", "snapshot"]]
            snap_destroys = [c for c in calls if c[:2] == ["zfs", "destroy"]]
            self.assertEqual(len(snap_creates), 1)
            self.assertEqual(len(snap_destroys), 1)
            self.assertEqual(snap_creates[0][2], snap_destroys[0][2])


if __name__ == "__main__":
    unittest.main()
