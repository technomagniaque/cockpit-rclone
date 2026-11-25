#!/usr/bin/env python3
"""Construit le paquet Arch Linux cockpit-rclone-<version>-<pkgrel>-any.pkg.tar.zst.

    python3 packaging/build_arch.py --out dist
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

VERSION = "1.0.2"
PKGREL = "1"
REPO = Path(__file__).resolve().parent.parent
ARCH_DIR = REPO / "packaging" / "arch"


def build_arch(out_dir: Path, version: str = VERSION, pkgrel: str = PKGREL) -> Path:
    if shutil.which("makepkg") is None:
        raise RuntimeError("makepkg introuvable : exécutez ce script sur Arch Linux (ou dans le conteneur archlinux:base-devel).")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("makepkg refuse de tourner en root : lancez ce script sous un utilisateur non privilégié.")

    out_dir.mkdir(parents=True, exist_ok=True)
    abs_out = out_dir.resolve()
    pkg_name = f"cockpit-rclone-{version}-{pkgrel}-any.pkg.tar.zst"
    pkg_path = abs_out / pkg_name

    with tempfile.TemporaryDirectory(prefix="cockpit-rclone-arch-") as tmp:
        build_dir = Path(tmp)
        for fname in (
            "PKGBUILD",
            "cockpit-rclone.install",
            "cockpit-rclone.sysusers",
            "cockpit-rclone.tmpfiles",
        ):
            shutil.copy2(ARCH_DIR / fname, build_dir / fname)

        pkgbuild_path = build_dir / "PKGBUILD"
        content = pkgbuild_path.read_text(encoding="utf-8")
        content = re.sub(r"^pkgver=.*$", f"pkgver={version}", content, count=1, flags=re.MULTILINE)
        content = re.sub(r"^pkgrel=.*$", f"pkgrel={pkgrel}", content, count=1, flags=re.MULTILINE)
        pkgbuild_path.write_text(content, encoding="utf-8")

        env = os.environ.copy()
        env["COCKPIT_RCLONE_SRC"] = str(REPO)
        env["PKGDEST"] = str(abs_out)
        env["PKGEXT"] = ".pkg.tar.zst"

        subprocess.run(
            ["makepkg", "--force", "--noconfirm", "--nodeps", "--cleanbuild", "--clean"],
            cwd=build_dir,
            env=env,
            check=True,
        )

    if not pkg_path.is_file():
        raise RuntimeError(f"Paquet Arch attendu introuvable après makepkg : {pkg_path}")
    return pkg_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Construire le paquet Arch Linux cockpit-rclone")
    ap.add_argument("--out", default="dist", help="Dossier de sortie (défaut : dist)")
    ap.add_argument("--version", default=VERSION, help="Version du paquet (pkgver)")
    ap.add_argument("--pkgrel", default=PKGREL, help="Révision du paquet (pkgrel)")
    args = ap.parse_args(argv)
    pkg = build_arch(Path(args.out), args.version, args.pkgrel)
    print(pkg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
