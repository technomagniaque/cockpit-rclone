import hashlib
import importlib.util
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ArchPackagingTest(unittest.TestCase):
    def test_versions_alignees_deb_et_arch(self) -> None:
        deb_mod = _load_module("build_deb", REPO / "packaging/build_deb.py")
        arch_mod = _load_module("build_arch", REPO / "packaging/build_arch.py")
        pkgbuild = (REPO / "packaging/arch/PKGBUILD").read_text(encoding="utf-8")
        m = re.search(r"^pkgver=(.+)$", pkgbuild, flags=re.MULTILINE)
        self.assertIsNotNone(m, "pkgver manquant dans PKGBUILD")
        assert m is not None
        self.assertEqual(deb_mod.VERSION, arch_mod.VERSION)
        self.assertEqual(deb_mod.VERSION, m.group(1).strip())

    def test_sha256sums_pkgbuild_exacts(self) -> None:
        pkgbuild = (REPO / "packaging/arch/PKGBUILD").read_text(encoding="utf-8")
        for fname in ("cockpit-rclone.sysusers", "cockpit-rclone.tmpfiles"):
            digest = hashlib.sha256((REPO / "packaging/arch" / fname).read_bytes()).hexdigest()
            self.assertIn(digest, pkgbuild, f"SHA-256 absent ou obsolète dans PKGBUILD pour {fname}")

    def test_install_script_syntaxe_shell(self) -> None:
        res = subprocess.run(
            ["sh", "-n", str(REPO / "packaging/arch/cockpit-rclone.install")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
