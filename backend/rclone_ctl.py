#!/usr/bin/env python3
"""rclone-ctl — interface CLI JSON appelée par l'extension Cockpit via cockpit.spawn.

Principes :
  - toutes les données sensibles (tokens OAuth, mots de passe, secrets) arrivent sur stdin
    en JSON et ne passent jamais dans argv ;
  - aucune commande shell (subprocess.run avec liste d'arguments uniquement) ;
  - écritures atomiques en mode 0600 pour rclone.conf et state.json ;
  - secrets toujours masqués dans les réponses renvoyées à l'interface navigateur.
"""
from __future__ import annotations

import base64
import configparser
import glob
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import rclone_core as core  # noqa: E402
import rclone_oauth_listener as oauth_listener  # noqa: E402

ETC_DIR = Path(os.environ.get("COCKPIT_RCLONE_ETC", "/etc/cockpit-rclone"))
RUN_DIR = Path(os.environ.get("COCKPIT_RCLONE_RUN", "/run/cockpit-rclone"))
SYSTEMD_DIR = Path(os.environ.get("COCKPIT_RCLONE_SYSTEMD", "/etc/systemd/system"))
DEFAULT_RCLONE_CONF = os.environ.get("RCLONE_CONFIG", "/root/.config/rclone/rclone.conf")
FUSE_CONF = Path(os.environ.get("COCKPIT_RCLONE_FUSE_CONF", "/etc/fuse.conf"))


def _read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Entrée JSON attendue sous forme d'objet")
    return data


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _load_state() -> dict[str, Any]:
    path = ETC_DIR / "state.json"
    if not path.exists():
        return {"jobs": {}, "mounts": {}, "serves": {}, "config_path": DEFAULT_RCLONE_CONF}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("jobs", {})
    data.setdefault("mounts", {})
    data.setdefault("serves", {})
    data.setdefault("config_path", DEFAULT_RCLONE_CONF)
    return data


def _save_state(state: dict[str, Any]) -> None:
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(ETC_DIR, 0o700)
    _atomic_write(ETC_DIR / "state.json", json.dumps(state, indent=2, sort_keys=True) + "\n", 0o600)


def _config_path(state: dict[str, Any]) -> Path:
    return Path(str(state.get("config_path") or DEFAULT_RCLONE_CONF))


def _run(cmd: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _rclone_obscure(secret: str) -> str:
    res = _run(["rclone", "obscure", secret], timeout=10.0)
    if res.returncode != 0:
        raise RuntimeError(f"rclone obscure a échoué : {res.stderr.strip()}")
    return res.stdout.strip()


def _detect_google_oauth_files() -> list[dict[str, str]]:
    """Cherche les fichiers d'identifiants OAuth Google existants sans exposer le secret."""
    candidates: list[Path] = [
        ETC_DIR / "google-oauth.json",
        Path("/root/.config/mygdoc/oauth-credentials.json"),
    ]
    for p in sorted(glob.glob("/home/*/.config/mygdoc/oauth-credentials.json")):
        candidates.append(Path(p))
    extra = os.environ.get("COCKPIT_RCLONE_OAUTH_FILES", "")
    for p in extra.split(":"):
        if p.strip():
            candidates.append(Path(p.strip()))

    seen: set[str] = set()
    found: list[dict[str, str]] = []
    for path in candidates:
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        try:
            cid, _ = core.parse_google_oauth_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found.append({
            "path": key,
            "client_id_preview": core.preview_client_id(cid),
            "source": "mygdoc" if "mygdoc" in key else "cockpit-rclone",
        })
    return found


def _fuse_allow_other_enabled() -> bool:
    if not FUSE_CONF.is_file():
        return False
    try:
        for line in FUSE_CONF.read_text(encoding="utf-8").splitlines():
            if line.strip() == "user_allow_other":
                return True
    except OSError:
        return False
    return False


def _local_users() -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    for entry in pwd.getpwall():
        if entry.pw_uid == 0 or (1000 <= entry.pw_uid < 60000 and entry.pw_shell not in ("/usr/sbin/nologin", "/bin/false")):
            users.append({
                "name": entry.pw_name,
                "uid": entry.pw_uid,
                "gid": entry.pw_gid,
                "home": entry.pw_dir,
            })
    return sorted(users, key=lambda u: u["uid"])


def _systemd_unit_status(unit: str) -> dict[str, str]:
    if not shutil.which("systemctl"):
        return {"active": "unknown", "sub": "unknown", "enabled": "unknown"}
    res = _run(["systemctl", "show", unit, "--property=ActiveState,SubState,UnitFileState,ExecMainExitTimestamp,NextElapseUSecRealtime"], timeout=5.0)
    info: dict[str, str] = {}
    for line in res.stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            info[k.strip()] = v.strip()
    return {
        "active": info.get("ActiveState", "inactive"),
        "sub": info.get("SubState", "dead"),
        "enabled": info.get("UnitFileState", "disabled"),
        "last_exit": info.get("ExecMainExitTimestamp", ""),
        "next_elapse": info.get("NextElapseUSecRealtime", ""),
    }


def _rc_call(runtime: dict[str, Any], endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"http://{runtime['rc_addr']}/{endpoint.lstrip('/')}"
    body = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if runtime.get("rc_user") and runtime.get("rc_pass"):
        creds = base64.b64encode(f"{runtime['rc_user']}:{runtime['rc_pass']}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {creds}")
    with urllib.request.urlopen(req, timeout=3.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _read_job_runtime(job_id: str) -> dict[str, Any] | None:
    path = RUN_DIR / f"job-{job_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _live_job_stats(job_id: str) -> dict[str, Any] | None:
    rt = _read_job_runtime(job_id)
    if not rt:
        return None
    try:
        stats = _rc_call(rt, "core/stats")
        bw = _rc_call(rt, "core/bwlimit")
        return {
            "running": True,
            "dry_run": rt.get("dry_run", False),
            "zfs_snapshot": rt.get("zfs_snapshot"),
            "started_at": rt.get("started_at"),
            "bytes": stats.get("bytes", 0),
            "totalBytes": stats.get("totalBytes", 0),
            "speed": stats.get("speed", 0),
            "eta": stats.get("eta"),
            "transfers": stats.get("transfers", 0),
            "totalTransfers": stats.get("totalTransfers", 0),
            "checks": stats.get("checks", 0),
            "totalChecks": stats.get("totalChecks", 0),
            "errors": stats.get("errors", 0),
            "lastError": stats.get("lastError", ""),
            "transferring": [
                {
                    "name": t.get("name", ""),
                    "size": t.get("size", 0),
                    "bytes": t.get("bytes", 0),
                    "percentage": t.get("percentage", 0),
                    "speed": t.get("speed", 0),
                    "eta": t.get("eta"),
                }
                for t in (stats.get("transferring") or [])
            ],
            "bwlimit": bw.get("rate", "off"),
        }
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _zfs_datasets() -> list[dict[str, str]]:
    if not shutil.which("zfs"):
        return []
    res = _run(["zfs", "list", "-H", "-o", "name,used,avail,mountpoint"], timeout=10.0)
    return core.parse_zfs_list(res.stdout) if res.returncode == 0 else []


def _zfs_orphan_snapshots() -> list[str]:
    if not shutil.which("zfs"):
        return []
    res = _run(["zfs", "list", "-H", "-t", "snapshot", "-o", "name"], timeout=10.0)
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if "@cockpit-rclone-" in line]


# ------------------------------------------------------------------ actions

def cmd_overview() -> dict[str, Any]:
    state = _load_state()
    cfg_path = _config_path(state)
    rclone_bin = shutil.which("rclone")
    rclone_ver = ""
    if rclone_bin:
        res = _run(["rclone", "version"], timeout=5.0)
        if res.returncode == 0 and res.stdout:
            rclone_ver = res.stdout.splitlines()[0].strip()

    conf_text = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""
    remotes = core.parse_rclone_conf(conf_text, mask_secrets=True)

    jobs_out: list[dict[str, Any]] = []
    for jid, job in sorted(state["jobs"].items()):
        svc = _systemd_unit_status(f"cockpit-rclone-job@{jid}.service")
        timer_unit = SYSTEMD_DIR / f"cockpit-rclone-job@{jid}.timer"
        timer = _systemd_unit_status(f"cockpit-rclone-job@{jid}.timer") if timer_unit.exists() else None
        live = _live_job_stats(jid)
        jobs_out.append({
            **job,
            "id": jid,
            "service": svc,
            "timer": timer,
            "live": live,
        })

    mounts_out: list[dict[str, Any]] = []
    for mid, m in sorted(state["mounts"].items()):
        svc = _systemd_unit_status(f"cockpit-rclone-mount@{mid}.service")
        mounts_out.append({**m, "id": mid, "service": svc})

    serves_out: list[dict[str, Any]] = []
    for sid, s in sorted(state["serves"].items()):
        svc = _systemd_unit_status(f"cockpit-rclone-serve@{sid}.service")
        masked = dict(s)
        if masked.get("pass"):
            masked["pass"] = core.MASKED_VALUE
        serves_out.append({**masked, "id": sid, "service": svc})

    return {
        "system": {
            "rclone_installed": bool(rclone_bin),
            "rclone_version": rclone_ver,
            "fuse3_installed": bool(shutil.which("fusermount3") or shutil.which("fusermount")),
            "fuse_allow_other": _fuse_allow_other_enabled(),
            "zfs_available": bool(shutil.which("zfs")),
            "config_path": str(cfg_path),
            "config_exists": cfg_path.is_file(),
            "users": _local_users(),
            "orphan_snapshots": _zfs_orphan_snapshots(),
        },
        "oauth_detected": _detect_google_oauth_files(),
        "zfs_datasets": _zfs_datasets(),
        "remotes": remotes,
        "jobs": jobs_out,
        "mounts": mounts_out,
        "serves": serves_out,
        "profiles": core.GDRIVE_PROFILES,
        "mount_profiles": core.MOUNT_PROFILES,
    }


def _read_raw_conf_sections(cfg_path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if cfg_path.is_file():
        parser.read_string(cfg_path.read_text(encoding="utf-8"))
    return {sec: dict(parser.items(sec)) for sec in parser.sections()}


def cmd_remote_save(payload: dict[str, Any]) -> dict[str, Any]:
    state = _load_state()
    cfg_path = _config_path(state)
    name = core.validate_remote_name(str(payload.get("name", "")))
    rtype = str(payload.get("type", "")).strip().lower()
    if not rtype or not core.REMOTE_NAME_RE.fullmatch(rtype):
        raise ValueError("Type de backend rclone invalide")

    sections = _read_raw_conf_sections(cfg_path)
    existing = dict(sections.get(name, {}))
    opts = dict(payload.get("options") or {})

    # Import serveur-side d'un fichier OAuth Google détecté (ex. mygdoc) sans transiter par le navigateur
    oauth_path = str(payload.get("use_oauth_file") or "").strip()
    if rtype == "drive" and oauth_path:
        allowed_paths = {item["path"] for item in _detect_google_oauth_files()}
        if oauth_path not in allowed_paths:
            raise ValueError("Chemin OAuth non autorisé (doit être un fichier détecté)")
        cid, csec = core.parse_google_oauth_json(Path(oauth_path).read_text(encoding="utf-8"))
        opts["client_id"] = cid
        opts["client_secret"] = csec

    # Import JSON OAuth collé directement
    if rtype == "drive" and payload.get("oauth_json"):
        cid, csec = core.parse_google_oauth_json(str(payload["oauth_json"]))
        opts["client_id"] = cid
        opts["client_secret"] = csec

    # Chiffrement automatique des mots de passe pour le backend crypt
    if rtype == "crypt":
        for pkey in ("password", "password2"):
            val = str(opts.get(pkey) or "")
            if val and val != core.MASKED_VALUE and payload.get("obscure_passwords", True):
                opts[pkey] = _rclone_obscure(val)

    new_section: dict[str, str] = {"type": rtype}
    for k, v in opts.items():
        key = str(k).strip()
        if not key or key == "type":
            continue
        val = "" if v is None else str(v).strip()
        if val == core.MASKED_VALUE:
            if key in existing:
                new_section[key] = existing[key]
            continue
        if val == "" and core.is_secret_key(key) and key in existing and payload.get("preserve_empty_secrets", True):
            new_section[key] = existing[key]
            continue
        if val != "":
            new_section[key] = val

    sections[name] = new_section
    _atomic_write(cfg_path, core.dump_rclone_conf(sections), 0o600)
    return {"ok": True, "name": name, "type": rtype}


def cmd_remote_delete(name: str) -> dict[str, Any]:
    rname = core.validate_remote_name(name)
    state = _load_state()
    cfg_path = _config_path(state)
    sections = _read_raw_conf_sections(cfg_path)
    if rname not in sections:
        raise ValueError(f"Remote introuvable : {rname}")
    del sections[rname]
    _atomic_write(cfg_path, core.dump_rclone_conf(sections), 0o600)
    return {"ok": True, "deleted": rname}


def cmd_remote_about(name: str) -> dict[str, Any]:
    rname = core.validate_remote_name(name)
    state = _load_state()
    res = _run(["rclone", "about", f"{rname}:", "--config", str(_config_path(state)), "--json"], timeout=30.0)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"rclone about a échoué (code {res.returncode})")
    return {"ok": True, "name": rname, "about": json.loads(res.stdout)}


def cmd_remote_cleanup(name: str) -> dict[str, Any]:
    rname = core.validate_remote_name(name)
    state = _load_state()
    res = _run(["rclone", "cleanup", f"{rname}:", "--config", str(_config_path(state))], timeout=120.0)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"rclone cleanup a échoué (code {res.returncode})")
    return {"ok": True, "name": rname, "output": res.stderr.strip() or "Corbeille vidée."}


def cmd_gdrive_auth_hint(payload: dict[str, Any]) -> dict[str, Any]:
    """Renvoie la commande sûre à exécuter sur un poste avec navigateur (ex. Z640) pour obtenir le token OAuth."""
    oauth_path = str(payload.get("oauth_path") or "").strip()
    if oauth_path:
        allowed = {item["path"] for item in _detect_google_oauth_files()}
        if oauth_path not in allowed:
            raise ValueError("Chemin OAuth non autorisé")
        # Commande bash lisant le JSON localement sans jamais afficher le secret dans Cockpit
        cmd = (
            f'python3 -c \'import json,subprocess; d=json.load(open({oauth_path!r})); '
            'i=d.get("installed") or d.get("web") or d; '
            'cid=i.get("client_id") or i.get("clientId"); '
            'sec=i.get("client_secret") or i.get("clientSecret"); '
            'subprocess.run(["rclone","authorize","drive",cid,sec], check=True)\''
        )
    else:
        cmd = 'rclone authorize "drive" "<CLIENT_ID>" "<CLIENT_SECRET>"'
    return {"ok": True, "command": cmd}


def _resolve_gdrive_client_creds(
    payload: dict[str, Any],
    existing: dict[str, str],
) -> tuple[str, str]:
    """Résout (client_id, client_secret) sans jamais exposer le secret au navigateur."""
    oauth_path = str(payload.get("use_oauth_file") or "").strip()
    if oauth_path:
        allowed = {item["path"] for item in _detect_google_oauth_files()}
        if oauth_path not in allowed:
            raise ValueError("Chemin OAuth non autorisé (doit être un fichier détecté)")
        return core.parse_google_oauth_json(Path(oauth_path).read_text(encoding="utf-8"))
    if payload.get("oauth_json"):
        return core.parse_google_oauth_json(str(payload["oauth_json"]))

    cid = str(payload.get("client_id") or existing.get("client_id") or "").strip()
    csec_in = str(payload.get("client_secret") or "").strip()
    if csec_in == core.MASKED_VALUE:
        csec_in = ""
    csec = csec_in or str(existing.get("client_secret") or "").strip()

    if cid and csec:
        return cid, csec

    # Repli automatique sur la clé Google déjà présente sur la machine (ex. mygdoc)
    detected = _detect_google_oauth_files()
    if detected:
        return core.parse_google_oauth_json(Path(detected[0]["path"]).read_text(encoding="utf-8"))

    raise ValueError(
        "Aucun client_id / client_secret Google OAuth trouvé. "
        "Sélectionnez votre fichier OAuth Google détecté ou saisissez client_id + client_secret."
    )


def _oauth_session_path(session_id: str) -> Path:
    sid = core.validate_session_id(session_id)
    return RUN_DIR / f"oauth-{sid}.json"


def cmd_gdrive_oauth_start(payload: dict[str, Any]) -> dict[str, Any]:
    """Démarre une session OAuth2 Google (PKCE) et renvoie l'URL directe de connexion Google."""
    state = _load_state()
    cfg_path = _config_path(state)
    remote_name = core.validate_remote_name(str(payload.get("name", "")))
    sections = _read_raw_conf_sections(cfg_path)
    existing = dict(sections.get(remote_name, {}))

    cid, csec = _resolve_gdrive_client_creds(payload, existing)
    scope = str(payload.get("scope") or existing.get("scope") or "drive").strip() or "drive"
    team_drive = str(payload.get("team_drive") or existing.get("team_drive") or "").strip()
    root_folder_id = str(payload.get("root_folder_id") or existing.get("root_folder_id") or "").strip()

    # Sauvegarde préalable des paramètres non-token dans rclone.conf (client_id/secret/scope)
    sec = dict(existing)
    sec["type"] = "drive"
    sec["client_id"] = cid
    sec["client_secret"] = csec
    sec["scope"] = scope
    if team_drive:
        sec["team_drive"] = team_drive
    if root_folder_id:
        sec["root_folder_id"] = root_folder_id
    sections[remote_name] = sec
    _atomic_write(cfg_path, core.dump_rclone_conf(sections), 0o600)

    port = int(os.environ.get("COCKPIT_RCLONE_OAUTH_PORT", "53682"))
    redirect_uri = f"http://127.0.0.1:{port}/"
    session_id = secrets.token_urlsafe(18)
    oauth_state = secrets.token_urlsafe(24)
    verifier, challenge = core.generate_pkce_pair()

    auth_url = core.build_gdrive_oauth_url(
        client_id=cid,
        state=oauth_state,
        code_challenge=challenge,
        scope=scope,
        redirect_uri=redirect_uri,
    )

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)
    session_path = _oauth_session_path(session_id)
    session_data = {
        "session_id": session_id,
        "remote": remote_name,
        "config_path": str(cfg_path),
        "client_id": cid,
        "client_secret": csec,
        "scope": scope,
        "team_drive": team_drive,
        "root_folder_id": root_folder_id,
        "state": oauth_state,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "status": "pending",
        "error": "",
        "created_at": time.time(),
        "expires_at": time.time() + 300.0,
    }
    _atomic_write(session_path, json.dumps(session_data), 0o600)

    listener_started = False
    if os.environ.get("COCKPIT_RCLONE_NO_OAUTH_LISTENER") != "1":
        listener_script = HERE / "rclone_oauth_listener.py"
        if listener_script.is_file():
            try:
                subprocess.Popen(
                    [sys.executable, str(listener_script), str(session_path), str(port)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
                listener_started = True
            except OSError:
                listener_started = False

    return {
        "ok": True,
        "session_id": session_id,
        "remote": remote_name,
        "auth_url": auth_url,
        "redirect_uri": redirect_uri,
        "listener_started": listener_started,
        "client_id_preview": core.preview_client_id(cid),
    }


def cmd_gdrive_oauth_status(session_id: str) -> dict[str, Any]:
    """Renvoie l'état d'une session OAuth sans jamais exposer aucun secret."""
    session_path = _oauth_session_path(session_id)
    if not session_path.is_file():
        return {"ok": True, "status": "expired", "error": "Session OAuth introuvable ou expirée."}
    data = json.loads(session_path.read_text(encoding="utf-8"))
    status = str(data.get("status") or "pending")
    if status == "pending" and time.time() > float(data.get("expires_at") or 0):
        status = "expired"
    return {
        "ok": True,
        "session_id": data.get("session_id"),
        "remote": data.get("remote"),
        "status": status,
        "error": str(data.get("error") or ""),
    }


def cmd_gdrive_oauth_finish(payload: dict[str, Any]) -> dict[str, Any]:
    """Finalise une connexion OAuth à partir de l'URL de redirection collée ou du code brut."""
    session_id = core.validate_session_id(str(payload.get("session_id", "")))
    callback_input = str(payload.get("callback_input") or payload.get("code") or "")
    code, state_rx = core.parse_oauth_callback_input(callback_input)
    session_path = _oauth_session_path(session_id)
    # Code brut collé à la main par un administrateur connecté à Cockpit : state peut manquer.
    # Si l'URL complète est collée, son state est vérifié.
    return oauth_listener.exchange_and_store_oauth_code(session_path, code, state_rx, allow_missing_state=True)


def cmd_browse(payload: dict[str, Any]) -> dict[str, Any]:
    target = core.validate_path(str(payload.get("target", "")), "target")
    state = _load_state()
    res = _run(["rclone", "lsjson", target, "--config", str(_config_path(state)), "--no-mimetype"], timeout=45.0)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"rclone lsjson a échoué (code {res.returncode})")
    items = json.loads(res.stdout or "[]")
    items.sort(key=lambda x: (not bool(x.get("IsDir")), str(x.get("Name", "")).lower()))
    return {"ok": True, "target": target, "items": items}


def cmd_size(payload: dict[str, Any]) -> dict[str, Any]:
    target = core.validate_path(str(payload.get("target", "")), "target")
    state = _load_state()
    res = _run(["rclone", "size", target, "--config", str(_config_path(state)), "--json"], timeout=120.0)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"rclone size a échoué (code {res.returncode})")
    return {"ok": True, "target": target, "size": json.loads(res.stdout)}


def cmd_mkdir(payload: dict[str, Any]) -> dict[str, Any]:
    target = core.validate_path(str(payload.get("target", "")), "target")
    state = _load_state()
    res = _run(["rclone", "mkdir", target, "--config", str(_config_path(state))], timeout=30.0)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"rclone mkdir a échoué (code {res.returncode})")
    return {"ok": True, "target": target}


def cmd_delete_path(payload: dict[str, Any]) -> dict[str, Any]:
    target = core.validate_path(str(payload.get("target", "")), "target")
    is_dir = bool(payload.get("is_dir"))
    state = _load_state()
    sub = "purge" if is_dir else "deletefile"
    res = _run(["rclone", sub, target, "--config", str(_config_path(state))], timeout=60.0)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"rclone {sub} a échoué (code {res.returncode})")
    return {"ok": True, "deleted": target}


def cmd_job_save(payload: dict[str, Any]) -> dict[str, Any]:
    jid = core.validate_job_id(str(payload.get("id", "")))
    state = _load_state()
    job = {
        "id": jid,
        "name": str(payload.get("name") or jid).strip(),
        "mode": str(payload.get("mode") or "copy").strip(),
        "profile": str(payload.get("profile") or "gdrive-nas-heavy").strip(),
        "source": core.validate_path(str(payload.get("source", "")), "source"),
        "destination": (
            "" if payload.get("mode") == "dedupe"
            else core.validate_path(str(payload.get("destination", "")), "destination")
        ),
        "zfs_snapshot": bool(payload.get("zfs_snapshot", False)),
        "dry_run": bool(payload.get("dry_run", False)),
        "excludes": core.validate_filter_patterns(payload.get("excludes") or []),
        "includes": core.validate_filter_patterns(payload.get("includes") or []),
        "options": dict(payload.get("options") or {}),
    }
    # Valide la commande générée avant de sauvegarder
    preview_argv = core.build_job_argv(job, config_path=str(_config_path(state)))
    state["jobs"][jid] = job
    _save_state(state)
    return {"ok": True, "job": job, "argv_preview": preview_argv}


def cmd_job_delete(job_id: str) -> dict[str, Any]:
    jid = core.validate_job_id(job_id)
    state = _load_state()
    if jid not in state["jobs"]:
        raise ValueError(f"Tâche introuvable : {jid}")
    if shutil.which("systemctl"):
        _run(["systemctl", "stop", f"cockpit-rclone-job@{jid}.service"], timeout=15.0)
        _run(["systemctl", "disable", "--now", f"cockpit-rclone-job@{jid}.timer"], timeout=15.0)
    timer_path = SYSTEMD_DIR / f"cockpit-rclone-job@{jid}.timer"
    if timer_path.exists():
        timer_path.unlink()
        if shutil.which("systemctl"):
            _run(["systemctl", "daemon-reload"], timeout=10.0)
    del state["jobs"][jid]
    _save_state(state)
    return {"ok": True, "deleted": jid}


def cmd_job_start(job_id: str, dry_run: bool = False) -> dict[str, Any]:
    jid = core.validate_job_id(job_id)
    state = _load_state()
    if jid not in state["jobs"]:
        raise ValueError(f"Tâche introuvable : {jid}")
    unit = f"cockpit-rclone-job-dry@{jid}.service" if dry_run else f"cockpit-rclone-job@{jid}.service"
    res = _run(["systemctl", "start", "--no-block", unit], timeout=10.0)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Impossible de démarrer {unit}")
    return {"ok": True, "unit": unit}


def cmd_job_stop(job_id: str) -> dict[str, Any]:
    jid = core.validate_job_id(job_id)
    rt = _read_job_runtime(jid)
    if rt:
        try:
            _rc_call(rt, "core/quit")
        except (urllib.error.URLError, OSError, ValueError):
            pass
    for unit in (f"cockpit-rclone-job@{jid}.service", f"cockpit-rclone-job-dry@{jid}.service"):
        if shutil.which("systemctl"):
            _run(["systemctl", "stop", unit], timeout=15.0)
    return {"ok": True, "stopped": jid}


def cmd_job_bwlimit(payload: dict[str, Any]) -> dict[str, Any]:
    jid = core.validate_job_id(str(payload.get("id", "")))
    rate = core.validate_bwlimit(str(payload.get("rate", ""))) or "off"
    rt = _read_job_runtime(jid)
    if not rt:
        raise RuntimeError(f"La tâche {jid} n'est pas en cours d'exécution")
    resp = _rc_call(rt, "core/bwlimit", {"rate": rate})
    return {"ok": True, "id": jid, "bwlimit": resp}


def cmd_timer_set(payload: dict[str, Any]) -> dict[str, Any]:
    jid = core.validate_job_id(str(payload.get("id", "")))
    state = _load_state()
    if jid not in state["jobs"]:
        raise ValueError(f"Tâche introuvable : {jid}")
    schedule = core.validate_calendar(str(payload.get("schedule", "")))
    delay = str(payload.get("randomized_delay") or "5m")
    enabled = bool(payload.get("enabled", True))

    if shutil.which("systemd-analyze"):
        chk = _run(["systemd-analyze", "calendar", schedule], timeout=5.0)
        if chk.returncode != 0:
            raise ValueError(f"Calendrier systemd invalide : {chk.stderr.strip() or chk.stdout.strip()}")

    timer_text = core.render_timer_unit(jid, schedule, delay)
    _atomic_write(SYSTEMD_DIR / f"cockpit-rclone-job@{jid}.timer", timer_text, 0o644)
    state["jobs"][jid]["schedule"] = schedule
    state["jobs"][jid]["randomized_delay"] = delay
    _save_state(state)

    if shutil.which("systemctl"):
        _run(["systemctl", "daemon-reload"], timeout=10.0)
        action = ["enable", "--now"] if enabled else ["disable", "--now"]
        res = _run(["systemctl", *action, f"cockpit-rclone-job@{jid}.timer"], timeout=10.0)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "Échec configuration du timer systemd")
    return {"ok": True, "id": jid, "schedule": schedule, "enabled": enabled}


def cmd_timer_remove(job_id: str) -> dict[str, Any]:
    jid = core.validate_job_id(job_id)
    if shutil.which("systemctl"):
        _run(["systemctl", "disable", "--now", f"cockpit-rclone-job@{jid}.timer"], timeout=10.0)
    p = SYSTEMD_DIR / f"cockpit-rclone-job@{jid}.timer"
    if p.exists():
        p.unlink()
    if shutil.which("systemctl"):
        _run(["systemctl", "daemon-reload"], timeout=10.0)
    state = _load_state()
    if jid in state["jobs"]:
        state["jobs"][jid].pop("schedule", None)
        _save_state(state)
    return {"ok": True, "removed": jid}


def cmd_mount_save(payload: dict[str, Any]) -> dict[str, Any]:
    mid = core.validate_job_id(str(payload.get("id", "")))
    state = _load_state()
    mount = {
        "id": mid,
        "name": str(payload.get("name") or mid).strip(),
        "profile": str(payload.get("profile") or "gdrive-z640-desktop").strip(),
        "remote_path": core.validate_path(str(payload.get("remote_path", "")), "remote_path"),
        "mountpoint": core.validate_path(str(payload.get("mountpoint", "")), "mountpoint"),
        "cache_dir": str(payload.get("cache_dir") or "/var/cache/cockpit-rclone").strip(),
        "vfs_cache_mode": str(payload.get("vfs_cache_mode") or "").strip(),
        "vfs_cache_max_size": str(payload.get("vfs_cache_max_size") or "").strip(),
        "vfs_cache_max_age": str(payload.get("vfs_cache_max_age") or "").strip(),
        "vfs_write_back": str(payload.get("vfs_write_back") or "").strip(),
        "vfs_read_chunk_size": str(payload.get("vfs_read_chunk_size") or "").strip(),
        "vfs_read_ahead": str(payload.get("vfs_read_ahead") or "").strip(),
        "dir_cache_time": str(payload.get("dir_cache_time") or "").strip(),
        "poll_interval": str(payload.get("poll_interval") or "").strip(),
        "bwlimit": str(payload.get("bwlimit") or "").strip(),
        "uid": payload.get("uid"),
        "gid": payload.get("gid"),
        "umask": str(payload.get("umask") or "").strip(),
        "allow_other": bool(payload.get("allow_other", True)),
        "read_only": bool(payload.get("read_only", False)),
    }
    argv_preview = core.build_mount_argv(mount, config_path=str(_config_path(state)))
    state["mounts"][mid] = mount
    _save_state(state)
    return {"ok": True, "mount": mount, "argv_preview": argv_preview}


def cmd_mount_delete(mount_id: str) -> dict[str, Any]:
    mid = core.validate_job_id(mount_id)
    state = _load_state()
    if mid not in state["mounts"]:
        raise ValueError(f"Montage introuvable : {mid}")
    if shutil.which("systemctl"):
        _run(["systemctl", "disable", "--now", f"cockpit-rclone-mount@{mid}.service"], timeout=20.0)
    del state["mounts"][mid]
    _save_state(state)
    return {"ok": True, "deleted": mid}


def cmd_service_action(unit_prefix: str, item_id: str, action: str) -> dict[str, Any]:
    iid = core.validate_job_id(item_id)
    if action not in ("start", "stop", "restart", "enable", "disable"):
        raise ValueError(f"Action systemd invalide : {action!r}")
    unit = f"{unit_prefix}@{iid}.service"
    cmd = ["systemctl", action, unit]
    if action in ("enable", "disable"):
        cmd = ["systemctl", action, "--now", unit]
    res = _run(cmd, timeout=30.0)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"systemctl {action} {unit} a échoué")
    return {"ok": True, "unit": unit, "status": _systemd_unit_status(unit)}


def cmd_serve_save(payload: dict[str, Any]) -> dict[str, Any]:
    sid = core.validate_job_id(str(payload.get("id", "")))
    state = _load_state()
    existing = state["serves"].get(sid, {})
    raw_pass = str(payload.get("pass") or "")
    if raw_pass == core.MASKED_VALUE:
        raw_pass = str(existing.get("pass") or "")
    serve = {
        "id": sid,
        "protocol": str(payload.get("protocol") or "webdav").lower(),
        "remote_path": core.validate_path(str(payload.get("remote_path", "")), "remote_path"),
        "addr": str(payload.get("addr") or "127.0.0.1:8080").strip(),
        "user": str(payload.get("user") or "").strip(),
        "pass": raw_pass,
        "read_only": bool(payload.get("read_only", True)),
        "run_as": core.validate_run_as(str(payload.get("run_as") or "")),
        "dlna_lan_ack": bool(payload.get("dlna_lan_ack", False)),
    }
    roots = core.serve_allowed_roots(_zfs_datasets(), state.get("serve_allowed_roots") or [])
    core.build_serve_argv(serve, config_path=str(_config_path(state)), allowed_roots=roots)
    state["serves"][sid] = serve
    _save_state(state)
    masked = dict(serve)
    if masked.get("pass"):
        masked["pass"] = core.MASKED_VALUE
    return {"ok": True, "serve": masked}


def cmd_serve_delete(serve_id: str) -> dict[str, Any]:
    sid = core.validate_job_id(serve_id)
    state = _load_state()
    if sid not in state["serves"]:
        raise ValueError(f"Serveur introuvable : {sid}")
    if shutil.which("systemctl"):
        _run(["systemctl", "disable", "--now", f"cockpit-rclone-serve@{sid}.service"], timeout=15.0)
    del state["serves"][sid]
    _save_state(state)
    return {"ok": True, "deleted": sid}


LOG_UNIT_RE = re.compile(r"^cockpit-rclone-(?:job|job-dry|mount|serve)@[a-z0-9][a-z0-9_-]{0,47}\.service$")


def cmd_logs(unit: str, lines: int = 120) -> dict[str, Any]:
    if not LOG_UNIT_RE.fullmatch(unit or ""):
        raise ValueError("Unité non autorisée pour lecture des journaux")
    n = max(10, min(1000, int(lines)))
    if not shutil.which("journalctl"):
        return {"ok": True, "unit": unit, "logs": "(journalctl indisponible)"}
    res = _run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"], timeout=10.0)
    return {"ok": True, "unit": unit, "logs": res.stdout}


def cmd_zfs_snapshot_cleanup() -> dict[str, Any]:
    snaps = _zfs_orphan_snapshots()
    destroyed: list[str] = []
    errors: list[str] = []
    for snap in snaps:
        res = _run(["zfs", "destroy", snap], timeout=15.0)
        if res.returncode == 0:
            destroyed.append(snap)
        else:
            errors.append(f"{snap}: {res.stderr.strip()}")
    return {"ok": not errors, "destroyed": destroyed, "errors": errors}


def dispatch(argv: list[str]) -> dict[str, Any]:
    if not argv:
        raise ValueError("Sous-commande requise")
    cmd = argv[0]
    if cmd == "overview":
        return cmd_overview()
    if cmd == "remote-save":
        return cmd_remote_save(_read_stdin_json())
    if cmd == "remote-delete":
        return cmd_remote_delete(argv[1])
    if cmd == "remote-about":
        return cmd_remote_about(argv[1])
    if cmd == "remote-cleanup":
        return cmd_remote_cleanup(argv[1])
    if cmd == "gdrive-auth-hint":
        return cmd_gdrive_auth_hint(_read_stdin_json())
    if cmd == "gdrive-oauth-start":
        return cmd_gdrive_oauth_start(_read_stdin_json())
    if cmd == "gdrive-oauth-status":
        return cmd_gdrive_oauth_status(argv[1])
    if cmd == "gdrive-oauth-finish":
        return cmd_gdrive_oauth_finish(_read_stdin_json())
    if cmd == "browse":
        return cmd_browse(_read_stdin_json())
    if cmd == "size":
        return cmd_size(_read_stdin_json())
    if cmd == "mkdir":
        return cmd_mkdir(_read_stdin_json())
    if cmd == "delete-path":
        return cmd_delete_path(_read_stdin_json())
    if cmd == "job-save":
        return cmd_job_save(_read_stdin_json())
    if cmd == "job-delete":
        return cmd_job_delete(argv[1])
    if cmd == "job-start":
        return cmd_job_start(argv[1], dry_run="--dry-run" in argv[2:])
    if cmd == "job-stop":
        return cmd_job_stop(argv[1])
    if cmd == "job-bwlimit":
        return cmd_job_bwlimit(_read_stdin_json())
    if cmd == "timer-set":
        return cmd_timer_set(_read_stdin_json())
    if cmd == "timer-remove":
        return cmd_timer_remove(argv[1])
    if cmd == "mount-save":
        return cmd_mount_save(_read_stdin_json())
    if cmd == "mount-delete":
        return cmd_mount_delete(argv[1])
    if cmd == "mount-action":
        return cmd_service_action("cockpit-rclone-mount", argv[1], argv[2])
    if cmd == "serve-save":
        return cmd_serve_save(_read_stdin_json())
    if cmd == "serve-delete":
        return cmd_serve_delete(argv[1])
    if cmd == "serve-action":
        return cmd_service_action("cockpit-rclone-serve", argv[1], argv[2])
    if cmd == "logs":
        lines = int(argv[2]) if len(argv) > 2 else 120
        return cmd_logs(argv[1], lines)
    if cmd == "zfs-snapshot-cleanup":
        return cmd_zfs_snapshot_cleanup()
    raise ValueError(f"Sous-commande inconnue : {cmd}")


def main(argv: list[str] | None = None) -> int:
    try:
        res = dispatch(argv if argv is not None else sys.argv[1:])
        print(json.dumps(res, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
