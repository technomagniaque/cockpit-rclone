"""rclone_core — logique pure pour cockpit-rclone (sans effet de bord système).

Regroupe :
  - validation stricte des identifiants (remotes, jobs, montages, chemins, drapeaux) ;
  - masquage des secrets lors de la lecture de rclone.conf ;
  - profils d'optimisation NAS ZFS <-> Google Drive (750 Go/j, chunks 128M, fast-list) ;
  - construction sûre des listes d'arguments rclone (jamais de shell=True) ;
  - détection du dataset ZFS d'un chemin local pour snapshot atomique avant transfert ;
  - génération des fichiers .timer systemd.
"""
from __future__ import annotations

import base64
import configparser
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import urllib.parse
from typing import Any, Mapping, Sequence

REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
SIZE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?[kKmMgGtTpP]?[bB]?$")
DURATION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d|w|y)$")
BWLIMIT_TOKEN_RE = re.compile(
    r"^(?:off|0|[0-9]+(?:\.[0-9]+)?[kKmMgGtT]?(?::[0-9]+(?:\.[0-9]+)?[kKmMgGtT]?)?)$"
)
BWLIMIT_SCHED_RE = re.compile(
    r"^(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)-)?[0-2][0-9]:[0-5][0-9],"
    r"(?:off|0|[0-9]+(?:\.[0-9]+)?[kKmMgGtT]?(?::[0-9]+(?:\.[0-9]+)?[kKmMgGtT]?)?)$"
)
SAFE_ADDR_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,64}$")
SAFE_CALENDAR_RE = re.compile(r"^[A-Za-z0-9*,./: _-]{1,120}$")
ALLOWED_MODES = ("copy", "sync", "move", "bisync", "check", "dedupe")
ALLOWED_DEDUPE_MODES = ("interactive", "skip", "first", "newest", "oldest", "largest", "smallest", "rename")
ALLOWED_VFS_CACHE_MODES = ("off", "minimal", "writes", "full")
ALLOWED_SERVE_PROTOCOLS = ("webdav", "http", "sftp", "dlna")

SECRET_KEYS = frozenset({
    "token",
    "client_secret",
    "pass",
    "password",
    "password2",
    "secret_access_key",
    "access_key_id",
    "key",
    "key_file_pass",
    "sasl_password",
    "service_account_credentials",
    "oauth_token",
    "refresh_token",
    "app_key",
    "app_secret",
    "api_key",
})
MASKED_VALUE = "***CONFIGURÉ***"

# Profils d'optimisation NAS ZFS (debian13zfs1, 30 Go RAM) <-> Google Drive
GDRIVE_PROFILES: dict[str, dict[str, Any]] = {
    "gdrive-nas-heavy": {
        "label": "Migration massive NAS → Google Drive (gros fichiers)",
        "description": (
            "Chunks 128 Mo (exploite la RAM du NAS), fast-list, 4 transferts / 8 checkers, "
            "tpslimit 10, arrêt propre à la limite 750 Go/24 h, corbeille GDrive active."
        ),
        "options": {
            "transfers": 4,
            "checkers": 8,
            "fast_list": True,
            "tpslimit": 10,
            "tpslimit_burst": 10,
            "drive_chunk_size": "128M",
            "drive_upload_cutoff": "128M",
            "drive_pacer_min_sleep": "10ms",
            "drive_stop_on_upload_limit": True,
            "drive_use_trash": True,
            "retries": 5,
            "low_level_retries": 10,
        },
    },
    "gdrive-small-files": {
        "label": "Petits fichiers / Photos & Docs (NAS → Google Drive)",
        "description": (
            "Plus de parallélisme (8 transferts / 16 checkers), chunks 32 Mo, "
            "régulation API (tpslimit 10) et arrêt propre au quota 750 Go/24 h."
        ),
        "options": {
            "transfers": 8,
            "checkers": 16,
            "fast_list": True,
            "tpslimit": 10,
            "tpslimit_burst": 10,
            "drive_chunk_size": "32M",
            "drive_pacer_min_sleep": "10ms",
            "drive_stop_on_upload_limit": True,
            "drive_use_trash": True,
            "retries": 5,
            "low_level_retries": 10,
        },
    },
    "gdrive-24x7-quota": {
        "label": "Continu 24/7 sous plafond 750 Go/j (8,5 Mo/s)",
        "description": (
            "Lisse le débit à 8,5 Mo/s (~735 Go/24 h) pour ne jamais heurter le blocage "
            "quotidien Google Drive lors d'une migration multi-To sur plusieurs jours."
        ),
        "options": {
            "bwlimit": "8.5M",
            "transfers": 4,
            "checkers": 8,
            "fast_list": True,
            "tpslimit": 10,
            "tpslimit_burst": 10,
            "drive_chunk_size": "128M",
            "drive_upload_cutoff": "128M",
            "drive_pacer_min_sleep": "10ms",
            "drive_stop_on_upload_limit": True,
            "drive_use_trash": True,
            "retries": 5,
            "low_level_retries": 10,
        },
    },
    "gdrive-download-nas": {
        "label": "Rapatriement Google Drive → NAS ZFS",
        "description": (
            "Optimisé téléchargement vers ZFS : 8 transferts, multi-thread 4 flux par gros "
            "fichier, buffer 64 Mo, fast-list et arrêt propre à la limite de download."
        ),
        "options": {
            "transfers": 8,
            "checkers": 16,
            "multi_thread_streams": 4,
            "multi_thread_cutoff": "64M",
            "buffer_size": "64M",
            "fast_list": True,
            "tpslimit": 10,
            "tpslimit_burst": 10,
            "drive_stop_on_download_limit": True,
            "retries": 5,
            "low_level_retries": 10,
        },
    },
    "custom": {
        "label": "Personnalisé (tous paramètres manuels)",
        "description": "Aucun préréglage imposé : utilise uniquement les options définies dans la tâche.",
        "options": {},
    },
}

# Profils de montage FUSE fiables (poste Z640 NVMe / KDE Dolphin et NAS ZFS)
MOUNT_PROFILES: dict[str, dict[str, Any]] = {
    "gdrive-z640-desktop": {
        "label": "Bureau fiable Z640 (NVMe / Dolphin / LibreOffice)",
        "description": (
            "Cache VFS complet (20 Go / 7 j), écriture différée 5 s, lecture anticipée 128 Mo, "
            "rafraîchissement ChangeNotify 30 s, uid/gid utilisateur et démontage propre fusermount3."
        ),
        "options": {
            "vfs_cache_mode": "full",
            "vfs_cache_max_size": "20G",
            "vfs_cache_max_age": "168h",
            "vfs_write_back": "5s",
            "vfs_read_chunk_size": "64M",
            "vfs_read_chunk_size_limit": "1G",
            "vfs_read_ahead": "128M",
            "dir_cache_time": "12h",
            "poll_interval": "30s",
            "attr_timeout": "1s",
            "drive_pacer_min_sleep": "10ms",
            "allow_other": True,
            "read_only": False,
            "umask": "0022",
        },
    },
    "gdrive-nas-ro": {
        "label": "NAS Lecture seule (Média / Archives)",
        "description": (
            "Montage lecture seule avec cache VFS 50 Go, chunks 128 Mo, lecture anticipée 256 Mo "
            "et cache de répertoires 24 h."
        ),
        "options": {
            "vfs_cache_mode": "full",
            "vfs_cache_max_size": "50G",
            "vfs_cache_max_age": "168h",
            "vfs_read_chunk_size": "128M",
            "vfs_read_chunk_size_limit": "2G",
            "vfs_read_ahead": "256M",
            "dir_cache_time": "24h",
            "poll_interval": "1m",
            "attr_timeout": "5s",
            "drive_pacer_min_sleep": "10ms",
            "allow_other": True,
            "read_only": True,
            "umask": "0022",
        },
    },
    "custom": {
        "label": "Personnalisé",
        "description": "Paramètres de montage FUSE configurés manuellement.",
        "options": {},
    },
}

UMASK_RE = re.compile(r"^[0-7]{3,4}$")


def parse_google_oauth_json(raw_text: str) -> tuple[str, str]:
    """Extrait (client_id, client_secret) d'un JSON OAuth Google Cloud ou mygdoc.

    Prend en charge :
      - mygdoc (~/.config/mygdoc/oauth-credentials.json) : {"clientId", "clientSecret"}
      - format plat snake_case : {"client_id", "client_secret"}
      - export Google Cloud Console : {"installed": {"client_id", "client_secret"}} ou {"web": ...}
    """
    import json
    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        raise ValueError(f"JSON OAuth Google invalide : {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Le fichier OAuth Google doit contenir un objet JSON")
    if isinstance(data.get("clientId"), str) and isinstance(data.get("clientSecret"), str):
        cid, csec = data["clientId"].strip(), data["clientSecret"].strip()
    elif isinstance(data.get("client_id"), str) and isinstance(data.get("client_secret"), str):
        cid, csec = data["client_id"].strip(), data["client_secret"].strip()
    else:
        nested = data.get("installed") or data.get("web")
        if isinstance(nested, dict) and isinstance(nested.get("client_id"), str) and isinstance(nested.get("client_secret"), str):
            cid, csec = nested["client_id"].strip(), nested["client_secret"].strip()
        else:
            raise ValueError("Clés client_id/client_secret (ou clientId/clientSecret) introuvables dans le JSON")
    if not cid or not csec:
        raise ValueError("client_id et client_secret doivent être non vides")
    return cid, csec


def preview_client_id(client_id: str) -> str:
    """Aperçu non sensible du client_id pour affichage UI (jamais le secret)."""
    cid = (client_id or "").strip()
    if len(cid) <= 18:
        return cid
    return f"{cid[:10]}…{cid[-16:]}"


# ------------------------------------------------------------------ validation

def validate_remote_name(name: str) -> str:
    val = (name or "").strip()
    if not REMOTE_NAME_RE.fullmatch(val):
        raise ValueError(
            "Nom de remote invalide (attendu : lettres, chiffres, '.', '_', '-', max 64 car.)"
        )
    return val


def validate_job_id(job_id: str) -> str:
    val = (job_id or "").strip().lower()
    if not JOB_ID_RE.fullmatch(val):
        raise ValueError(
            "Identifiant invalide (attendu : minuscules, chiffres, '_', '-', max 48 car.)"
        )
    return val


def validate_path(path: str, label: str = "chemin") -> str:
    val = (path or "").strip()
    if not val:
        raise ValueError(f"Le {label} ne peut pas être vide")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in val):
        raise ValueError(f"Le {label} contient des caractères de contrôle interdits")
    if val.startswith("-"):
        raise ValueError(f"Le {label} ne doit pas commencer par '-'")
    return val


def validate_bwlimit(raw: str) -> str:
    val = (raw or "").strip()
    if not val:
        return ""
    tokens = val.split()
    for tok in tokens:
        if not (BWLIMIT_TOKEN_RE.fullmatch(tok) or BWLIMIT_SCHED_RE.fullmatch(tok)):
            raise ValueError(
                f"Expression --bwlimit invalide : {tok!r} (ex. '8.5M', 'off', '08:00,5M 23:00,off')"
            )
    return " ".join(tokens)


def validate_size(raw: str, label: str) -> str:
    val = (raw or "").strip()
    if not val:
        return ""
    if not SIZE_RE.fullmatch(val):
        raise ValueError(f"Taille invalide pour {label} : {val!r} (ex. '128M', '50G')")
    return val


def validate_duration(raw: str, label: str) -> str:
    val = (raw or "").strip()
    if not val:
        return ""
    if not DURATION_RE.fullmatch(val):
        raise ValueError(f"Durée invalide pour {label} : {val!r} (ex. '10ms', '24h', '7d')")
    return val


def validate_calendar(expr: str) -> str:
    val = (expr or "").strip()
    if not val or not SAFE_CALENDAR_RE.fullmatch(val):
        raise ValueError(f"Expression OnCalendar invalide : {val!r}")
    return val


def validate_filter_patterns(patterns: Sequence[str]) -> list[str]:
    out: list[str] = []
    for raw in patterns:
        pat = (raw or "").strip()
        if not pat:
            continue
        if any(ord(ch) < 32 or ch == "\x7f" for ch in pat):
            raise ValueError(f"Motif de filtre invalide : {pat!r}")
        out.append(pat)
    return out


# ------------------------------------------------------------------ rclone.conf

def is_secret_key(key: str) -> bool:
    low = key.strip().lower()
    if low in SECRET_KEYS:
        return True
    return any(tok in low for tok in ("secret", "password", "token", "private_key"))


def parse_rclone_conf(text: str, mask_secrets: bool = True) -> list[dict[str, Any]]:
    """Parse un fichier rclone.conf en liste de remotes (avec masquage des secrets)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # conserver la casse exacte des clés rclone
    parser.read_string(text or "")
    remotes: list[dict[str, Any]] = []
    for name in parser.sections():
        section = dict(parser.items(name))
        rtype = section.get("type", "unknown")
        opts: dict[str, str] = {}
        secret_fields: list[str] = []
        for k, v in section.items():
            if k == "type":
                continue
            if is_secret_key(k):
                secret_fields.append(k)
                opts[k] = MASKED_VALUE if (mask_secrets and v) else v
            else:
                opts[k] = v
        remotes.append({
            "name": name,
            "type": rtype,
            "options": opts,
            "has_secrets": secret_fields,
            "has_own_client_id": bool(section.get("client_id")) if rtype == "drive" else None,
            "has_token": bool(section.get("token")) if rtype == "drive" else None,
        })
    return remotes


def dump_rclone_conf(sections: Mapping[str, Mapping[str, str]]) -> str:
    """Sérialise proprement un dictionnaire {remote: {k: v}} au format rclone.conf."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    for name in sorted(sections):
        parser.add_section(name)
        for k, v in sections[name].items():
            if v is not None and str(v) != "":
                parser.set(name, k, str(v))
    buf = io.StringIO()
    parser.write(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ ZFS helpers

def parse_zfs_list(stdout: str) -> list[dict[str, str]]:
    """Parse `zfs list -H -o name,used,avail,mountpoint`."""
    datasets: list[dict[str, str]] = []
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, used, avail, mountpoint = parts[:4]
        if mountpoint in ("-", "none", "legacy"):
            continue
        datasets.append({
            "name": name,
            "used": used,
            "avail": avail,
            "mountpoint": mountpoint,
        })
    return datasets


def resolve_zfs_dataset(local_path: str, datasets: Sequence[Mapping[str, str]]) -> tuple[str, str, str] | None:
    """Pour un chemin local situé sur un dataset ZFS monté, rend (dataset, mountpoint, subpath).

    Choisit le point de montage le plus spécifique (plus long préfixe).
    Rend None si le chemin est un remote rclone (contient ':') ou hors ZFS.
    """
    if not local_path or ":" in local_path or not local_path.startswith("/"):
        return None
    norm = os.path.normpath(local_path)
    best: tuple[int, str, str, str] | None = None
    for ds in datasets:
        mp = os.path.normpath(ds.get("mountpoint", ""))
        if not mp or not mp.startswith("/"):
            continue
        if norm == mp or norm.startswith(mp.rstrip("/") + "/"):
            rel = "" if norm == mp else os.path.relpath(norm, mp)
            cand = (len(mp), ds["name"], mp, rel)
            if best is None or cand[0] > best[0]:
                best = cand
    if best is None:
        return None
    return best[1], best[2], best[3]


def snapshot_source_path(mountpoint: str, snap_name: str, rel_subpath: str) -> str:
    """Chemin lecture seule dans .zfs/snapshot/<snap_name>[/<rel_subpath>]."""
    base = os.path.join(mountpoint, ".zfs", "snapshot", snap_name)
    return os.path.join(base, rel_subpath) if rel_subpath else base


# ------------------------------------------------------------------ construction argv rclone

def merged_job_options(job: Mapping[str, Any]) -> dict[str, Any]:
    """Fusionne les options du profil Google Drive choisi avec les surcharges de la tâche."""
    profile_id = str(job.get("profile") or "custom")
    base = dict(GDRIVE_PROFILES.get(profile_id, GDRIVE_PROFILES["custom"])["options"])
    overrides = job.get("options") or {}
    for k, v in overrides.items():
        if v is None or v == "":
            continue
        base[k] = v
    return base


def build_job_argv(
    job: Mapping[str, Any],
    *,
    config_path: str,
    dry_run: bool = False,
    source_override: str | None = None,
    rc_addr: str | None = None,
    rc_user: str | None = None,
    rc_pass: str | None = None,
    backup_dir_suffix: str | None = None,
) -> list[str]:
    """Construit la commande rclone complète sous forme de liste argv (zéro shell)."""
    mode = str(job.get("mode") or "copy")
    if mode not in ALLOWED_MODES:
        raise ValueError(f"Mode rclone non pris en charge : {mode!r}")

    src = validate_path(source_override if source_override is not None else str(job.get("source", "")), "source")
    opts = merged_job_options(job)

    argv: list[str] = ["rclone", mode, "--config", config_path]

    if mode == "dedupe":
        dmode = str(opts.get("dedupe_mode") or "rename")
        if dmode not in ALLOWED_DEDUPE_MODES:
            raise ValueError(f"Mode dedupe invalide : {dmode!r}")
        argv += ["--dedupe-mode", dmode, src]
    else:
        dst = validate_path(str(job.get("destination", "")), "destination")
        argv += [src, dst]

    if dry_run or bool(job.get("dry_run")):
        argv.append("--dry-run")

    # Intégration API --rc locale (suivi temps réel + bwlimit à chaud)
    if rc_addr:
        argv += ["--rc", "--rc-addr", rc_addr]
        if rc_user and rc_pass:
            argv += ["--rc-user", rc_user, "--rc-pass", rc_pass]
        else:
            argv.append("--rc-no-auth")

    # Statistiques lisibles dans journalctl
    argv += ["--stats", "5s", "--stats-one-line", "-v"]

    # Paramètres entiers bornés
    int_flags = (
        ("transfers", "--transfers", 1, 64),
        ("checkers", "--checkers", 1, 128),
        ("tpslimit", "--tpslimit", 1, 100),
        ("tpslimit_burst", "--tpslimit-burst", 1, 200),
        ("retries", "--retries", 1, 50),
        ("low_level_retries", "--low-level-retries", 1, 100),
        ("multi_thread_streams", "--multi-thread-streams", 0, 32),
    )
    for key, flag, lo, hi in int_flags:
        if key in opts and opts[key] not in (None, ""):
            val = int(opts[key])
            if not (lo <= val <= hi):
                raise ValueError(f"Valeur hors bornes pour {key} : {val} (attendu {lo}..{hi})")
            argv += [flag, str(val)]

    # Paramètres de taille
    for key, flag in (
        ("drive_chunk_size", "--drive-chunk-size"),
        ("drive_upload_cutoff", "--drive-upload-cutoff"),
        ("multi_thread_cutoff", "--multi-thread-cutoff"),
        ("buffer_size", "--buffer-size"),
        ("max_size", "--max-size"),
        ("min_size", "--min-size"),
    ):
        if opts.get(key):
            argv += [flag, validate_size(str(opts[key]), key)]

    # Paramètres de durée
    for key, flag in (
        ("drive_pacer_min_sleep", "--drive-pacer-min-sleep"),
        ("min_age", "--min-age"),
        ("max_age", "--max-age"),
    ):
        if opts.get(key):
            argv += [flag, validate_duration(str(opts[key]), key)]

    # Bande passante
    if opts.get("bwlimit"):
        argv += ["--bwlimit", validate_bwlimit(str(opts["bwlimit"]))]

    # Drapeaux booléens
    bool_flags = (
        ("fast_list", "--fast-list"),
        ("checksum", "--checksum"),
        ("update", "--update"),
        ("ignore_existing", "--ignore-existing"),
        ("delete_empty_src_dirs", "--delete-empty-src-dirs"),
        ("drive_stop_on_upload_limit", "--drive-stop-on-upload-limit"),
        ("drive_stop_on_download_limit", "--drive-stop-on-download-limit"),
        ("drive_skip_gdocs", "--drive-skip-gdocs"),
        ("drive_shared_with_me", "--drive-shared-with-me"),
        ("resync", "--resync"),
    )
    for key, flag in bool_flags:
        if opts.get(key):
            argv.append(flag)

    if "drive_use_trash" in opts:
        argv.append("--drive-use-trash=true" if opts["drive_use_trash"] else "--drive-use-trash=false")

    # Corbeille horodatée anti-suppression accidentelle (sync / move)
    backup_dir = str(opts.get("backup_dir") or "").strip()
    if backup_dir and mode in ("sync", "move"):
        bdir = validate_path(backup_dir, "backup_dir")
        if backup_dir_suffix:
            bdir = bdir.rstrip("/") + "/" + backup_dir_suffix
        argv += ["--backup-dir", bdir]

    # Filtres --exclude / --include
    for pat in validate_filter_patterns(job.get("excludes") or []):
        argv += ["--exclude", pat]
    for pat in validate_filter_patterns(job.get("includes") or []):
        argv += ["--include", pat]

    return argv


def merged_mount_options(mount: Mapping[str, Any]) -> dict[str, Any]:
    """Fusionne les options d'un profil de montage FUSE avec les surcharges utilisateur."""
    profile_id = str(mount.get("profile") or "custom")
    base = dict(MOUNT_PROFILES.get(profile_id, MOUNT_PROFILES["custom"])["options"])
    for k, v in mount.items():
        if k in ("id", "name", "profile", "enabled"):
            continue
        if v is None or v == "":
            continue
        base[k] = v
    return base


def build_mount_argv(mount: Mapping[str, Any], *, config_path: str) -> list[str]:
    """Construit la commande `rclone mount` pour une unité systemd persistante."""
    opts = merged_mount_options(mount)
    remote_path = validate_path(str(opts.get("remote_path", "")), "remote_path")
    mountpoint = validate_path(str(opts.get("mountpoint", "")), "mountpoint")
    if not mountpoint.startswith("/"):
        raise ValueError("Le point de montage doit être un chemin absolu (ex. /mnt/rclone/gdrive)")

    vfs_mode = str(opts.get("vfs_cache_mode") or "full")
    if vfs_mode not in ALLOWED_VFS_CACHE_MODES:
        raise ValueError(f"Mode vfs-cache-mode invalide : {vfs_mode!r}")

    argv = [
        "rclone", "mount", remote_path, mountpoint,
        "--config", config_path,
        "--vfs-cache-mode", vfs_mode,
        "--dir-cache-time", validate_duration(str(opts.get("dir_cache_time") or "5m"), "dir_cache_time"),
        "--poll-interval", validate_duration(str(opts.get("poll_interval") or "1m"), "poll_interval"),
    ]
    if opts.get("cache_dir"):
        argv += ["--cache-dir", validate_path(str(opts["cache_dir"]), "cache_dir")]
    for key, flag in (
        ("vfs_cache_max_size", "--vfs-cache-max-size"),
        ("vfs_read_chunk_size", "--vfs-read-chunk-size"),
        ("vfs_read_chunk_size_limit", "--vfs-read-chunk-size-limit"),
        ("vfs_read_ahead", "--vfs-read-ahead"),
    ):
        if opts.get(key):
            argv += [flag, validate_size(str(opts[key]), key)]
    for key, flag in (
        ("vfs_cache_max_age", "--vfs-cache-max-age"),
        ("vfs_write_back", "--vfs-write-back"),
        ("attr_timeout", "--attr-timeout"),
        ("drive_pacer_min_sleep", "--drive-pacer-min-sleep"),
    ):
        if opts.get(key):
            argv += [flag, validate_duration(str(opts[key]), key)]
    if opts.get("bwlimit"):
        argv += ["--bwlimit", validate_bwlimit(str(opts["bwlimit"]))]
    for key, flag in (("uid", "--uid"), ("gid", "--gid")):
        if opts.get(key) not in (None, ""):
            val = int(opts[key])
            if not (0 <= val <= 65534):
                raise ValueError(f"Valeur {key} invalide : {val}")
            argv += [flag, str(val)]
    if opts.get("umask"):
        um = str(opts["umask"]).strip()
        if not UMASK_RE.fullmatch(um):
            raise ValueError(f"umask invalide : {um!r} (ex. '0022')")
        argv += ["--umask", um]
    if opts.get("allow_other", True):
        argv.append("--allow-other")
    if opts.get("read_only"):
        argv.append("--read-only")
    return argv


def build_serve_argv(serve: Mapping[str, Any], *, config_path: str) -> list[str]:
    """Construit la commande `rclone serve <proto>`."""
    proto = str(serve.get("protocol") or "webdav").lower()
    if proto not in ALLOWED_SERVE_PROTOCOLS:
        raise ValueError(f"Protocole serve invalide : {proto!r}")
    remote_path = validate_path(str(serve.get("remote_path", "")), "remote_path")
    addr = str(serve.get("addr") or "127.0.0.1:8080").strip()
    if not SAFE_ADDR_RE.fullmatch(addr):
        raise ValueError(f"Adresse d'écoute invalide : {addr!r}")

    argv = ["rclone", "serve", proto, remote_path, "--config", config_path, "--addr", addr]
    if serve.get("read_only"):
        argv.append("--read-only")
    if serve.get("user") and serve.get("pass") and proto != "dlna":
        argv += ["--user", str(serve["user"]), "--pass", str(serve["pass"])]
    return argv


def render_timer_unit(job_id: str, schedule: str, randomized_delay: str = "5m") -> str:
    """Génère le contenu d'une unité systemd .timer pour une tâche cockpit-rclone."""
    jid = validate_job_id(job_id)
    cal = validate_calendar(schedule)
    delay = validate_duration(randomized_delay, "randomized_delay") if randomized_delay else "0s"
    return (
        "[Unit]\n"
        f"Description=Planification cockpit-rclone pour la tâche {jid}\n\n"
        "[Timer]\n"
        f"OnCalendar={cal}\n"
        "Persistent=true\n"
        f"RandomizedDelaySec={delay}\n"
        f"Unit=cockpit-rclone-job@{jid}.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


# ------------------------------------------------------------------ Google Drive OAuth2 PKCE

GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_OAUTH_REDIRECT_URI = "http://127.0.0.1:53682/"

GDRIVE_SCOPE_URLS: dict[str, str] = {
    "drive": "https://www.googleapis.com/auth/drive",
    "drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
    "drive.file": "https://www.googleapis.com/auth/drive.file",
    "drive.appfolder": "https://www.googleapis.com/auth/drive.appdata",
    "drive.metadata.readonly": "https://www.googleapis.com/auth/drive.metadata.readonly",
}

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def validate_session_id(session_id: str) -> str:
    val = (session_id or "").strip()
    if not SESSION_ID_RE.fullmatch(val):
        raise ValueError("Identifiant de session OAuth invalide")
    return val


def resolve_gdrive_scope_url(scope: str) -> str:
    raw = (scope or "drive").strip()
    if raw.startswith("https://"):
        return raw
    return GDRIVE_SCOPE_URLS.get(raw, GDRIVE_SCOPE_URLS["drive"])


def generate_pkce_pair() -> tuple[str, str]:
    """Génère (code_verifier, code_challenge S256) conforme RFC 7636."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_gdrive_oauth_url(
    *,
    client_id: str,
    state: str,
    code_challenge: str,
    scope: str = "drive",
    redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
) -> str:
    """Construit l'URL officielle Google OAuth2 (page de connexion Google)."""
    cid = (client_id or "").strip()
    if not cid:
        raise ValueError("client_id Google OAuth manquant")
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": resolve_gdrive_scope_url(scope),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{GOOGLE_OAUTH_AUTH_URL}?{urllib.parse.urlencode(params)}"


def parse_oauth_callback_input(raw_input: str) -> tuple[str, str | None]:
    """Extrait (code, state_optionnel) depuis une URL de redirection complète ou un code brut."""
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("Collez l'URL de redirection (http://127.0.0.1:53682/?...) ou le code OAuth Google")
    if text.startswith("http://") or text.startswith("https://") or "code=" in text:
        parsed = urllib.parse.urlparse(text if "://" in text else f"http://localhost/?{text.lstrip('?')}")
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("error"):
            err = qs["error"][0]
            desc = (qs.get("error_description") or [""])[0]
            raise ValueError(f"Autorisation refusée par Google : {err}" + (f" ({desc})" if desc else ""))
        codes = qs.get("code")
        if not codes or not codes[0].strip():
            raise ValueError("Aucun paramètre 'code=' trouvé dans l'URL collée")
        states = qs.get("state")
        return codes[0].strip(), (states[0].strip() if states and states[0].strip() else None)
    if any(ch.isspace() for ch in text):
        raise ValueError("Code OAuth invalide (contient des espaces)")
    return text, None


def format_rclone_oauth_token(
    token_resp: Mapping[str, Any],
    *,
    now: datetime.datetime | None = None,
) -> str:
    """Convertit la réponse JSON de https://oauth2.googleapis.com/token au format token rclone."""
    access = str(token_resp.get("access_token") or "").strip()
    if not access:
        raise ValueError("Réponse Google OAuth sans access_token")
    token_type = str(token_resp.get("token_type") or "Bearer").strip() or "Bearer"
    refresh = str(token_resp.get("refresh_token") or "").strip()
    expires_in = int(token_resp.get("expires_in") or 3599)
    base_now = now or datetime.datetime.now(datetime.timezone.utc)
    expiry = (base_now + datetime.timedelta(seconds=max(60, expires_in))).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    out: dict[str, Any] = {
        "access_token": access,
        "token_type": token_type,
        "expiry": expiry,
    }
    if refresh:
        out["refresh_token"] = refresh
    return json.dumps(out, separators=(",", ":"))
