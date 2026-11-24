#!/usr/bin/env python3
"""Construit le paquet Debian cockpit-rclone_<version>_all.deb (Debian 13 Trixie).

    python3 packaging/build_deb.py --out dist
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

VERSION = "1.0.1"
REPO = Path(__file__).resolve().parent.parent

CONTROL_TPL = """Package: cockpit-rclone
Version: {version}
Section: admin
Priority: optional
Architecture: all
Maintainer: Etienne <technomagniaque@users.noreply.github.com>
Depends: cockpit-bridge, python3 (>= 3.9), rclone, fuse3
Suggests: zfsutils-linux
Description: Extension Cockpit de gestion rclone optimisée NAS ZFS et Google Drive
 Interface Cockpit complète pour configurer les remotes rclone (assistant
 Google Drive OAuth personnel et Crypt), synchroniser ou migrer des données
 entre un NAS ZFS et Google Drive (profils 750 Go/j, snapshots ZFS atomiques,
 API --rc temps réel, timers systemd), explorer en double panneau et gérer des
 montages FUSE fiables sur serveur comme sur poste de travail (ex. Z640).
"""

POSTINST = """#!/bin/sh
set -e
install -d -m 0700 /etc/cockpit-rclone
install -d -m 0755 /var/cache/cockpit-rclone
if [ -f /etc/fuse.conf ] && ! grep -Eq '^[[:space:]]*user_allow_other[[:space:]]*$' /etc/fuse.conf; then
    if grep -Eq '^[[:space:]]*#[[:space:]]*user_allow_other[[:space:]]*$' /etc/fuse.conf; then
        sed -i 's/^[[:space:]]*#[[:space:]]*user_allow_other[[:space:]]*$/user_allow_other/' /etc/fuse.conf
    else
        echo "user_allow_other" >> /etc/fuse.conf
    fi
fi
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi
"""

POSTRM = """#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi
"""

COPYRIGHT = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: cockpit-rclone

Files: *
Copyright: 2026 technomagniaque
License: GPL-3+
 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
"""


def _copy(src: Path, dst: Path, mode: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    os.chmod(dst, mode)


def build_deb(out_dir: Path, version: str = VERSION) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    deb_path = out_dir / f"cockpit-rclone_{version}_all.deb"

    with tempfile.TemporaryDirectory(prefix="cockpit-rclone-deb-") as tmp:
        root = Path(tmp) / "pkg"
        debian = root / "DEBIAN"
        debian.mkdir(parents=True)

        (debian / "control").write_text(CONTROL_TPL.format(version=version), encoding="utf-8")
        for name, content in (("postinst", POSTINST), ("postrm", POSTRM)):
            p = debian / name
            p.write_text(content, encoding="utf-8")
            os.chmod(p, 0o755)

        # Fichiers frontend Cockpit
        for fname in ("manifest.json", "index.html", "styles.css", "api.js", "profiles.js", "app.js"):
            _copy(REPO / "cockpit" / fname, root / "usr/share/cockpit/rclone" / fname, 0o644)

        # Backend Python
        _copy(REPO / "backend/rclone_core.py", root / "usr/lib/cockpit-rclone/rclone_core.py", 0o644)
        _copy(REPO / "backend/rclone_oauth_listener.py", root / "usr/lib/cockpit-rclone/rclone_oauth_listener.py", 0o755)
        _copy(REPO / "backend/rclone_ctl.py", root / "usr/lib/cockpit-rclone/rclone-ctl.py", 0o755)
        _copy(REPO / "backend/rclone_job_runner.py", root / "usr/lib/cockpit-rclone/rclone-job-runner.py", 0o755)

        # Unités systemd templates
        for unit in (
            "cockpit-rclone-job@.service",
            "cockpit-rclone-job-dry@.service",
            "cockpit-rclone-mount@.service",
            "cockpit-rclone-serve@.service",
        ):
            _copy(REPO / "systemd" / unit, root / "usr/lib/systemd/system" / unit, 0o644)

        # Documentation paquet
        doc_dir = root / "usr/share/doc/cockpit-rclone"
        doc_dir.mkdir(parents=True, exist_ok=True)
        (doc_dir / "copyright").write_text(COPYRIGHT, encoding="utf-8")
        os.chmod(doc_dir / "copyright", 0o644)

        # Normalise les permissions de tous les dossiers à 0755 (indépendant de umask)
        os.chmod(root, 0o755)
        for dirpath, dirnames, _ in os.walk(root):
            os.chmod(dirpath, 0o755)
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o755)

        subprocess.run(
            ["dpkg-deb", "--build", "--root-owner-group", str(root), str(deb_path)],
            check=True,
        )
    return deb_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Construire le paquet .deb cockpit-rclone")
    ap.add_argument("--out", default="dist", help="Dossier de sortie (défaut : dist)")
    ap.add_argument("--version", default=VERSION, help="Version du paquet")
    args = ap.parse_args(argv)
    deb = build_deb(Path(args.out), args.version)
    print(deb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
