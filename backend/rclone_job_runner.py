#!/usr/bin/env python3
"""rclone-job-runner — exécuteur systemd pour cockpit-rclone (jobs, mounts, serves).

Fonctionnalités :
  - lit la définition de la tâche/montage/serve dans /etc/cockpit-rclone/state.json ;
  - pour un job avec `zfs_snapshot: true` sur une source ZFS locale : crée un snapshot
    atomique `dataset@cockpit-rclone-<id>-<ts>`, exécute rclone sur `.zfs/snapshot/...`,
    et détruit le snapshot dans un bloc `finally` même en cas d'erreur ou SIGTERM ;
  - lance chaque job avec une API `--rc` locale authentifiée (`127.0.0.1:<port_libre>`,
    mot de passe aléatoire par exécution écrit en 0600 dans `/run/cockpit-rclone/`) ;
  - transmet proprement SIGTERM/SIGINT à rclone pour un arrêt propre.
"""
from __future__ import annotations

import datetime
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import rclone_core as core  # noqa: E402

ETC_DIR = Path(os.environ.get("COCKPIT_RCLONE_ETC", "/etc/cockpit-rclone"))
RUN_DIR = Path(os.environ.get("COCKPIT_RCLONE_RUN", "/run/cockpit-rclone"))
DEFAULT_RCLONE_CONF = os.environ.get("RCLONE_CONFIG", "/root/.config/rclone/rclone.conf")


def _load_state() -> dict[str, Any]:
    path = ETC_DIR / "state.json"
    if not path.exists():
        return {"jobs": {}, "mounts": {}, "serves": {}, "config_path": DEFAULT_RCLONE_CONF}
    return json.loads(path.read_text(encoding="utf-8"))


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_runtime(kind: str, item_id: str, payload: dict[str, Any]) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)
    path = RUN_DIR / f"{kind}-{item_id}.json"
    tmp = RUN_DIR / f".{kind}-{item_id}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    return path


def _list_zfs_datasets() -> list[dict[str, str]]:
    try:
        res = subprocess.run(
            ["zfs", "list", "-H", "-o", "name,used,avail,mountpoint"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return []
    return core.parse_zfs_list(res.stdout) if res.returncode == 0 else []


def _create_zfs_snapshot(job_id: str, source: str) -> tuple[str, str] | None:
    """Crée un snapshot ZFS pour `source` ; rend (full_snap_id, snapshot_source_path) ou None."""
    datasets = _list_zfs_datasets()
    resolved = core.resolve_zfs_dataset(source, datasets)
    if resolved is None:
        return None
    dataset, mountpoint, rel_subpath = resolved
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    snap_name = f"cockpit-rclone-{job_id}-{ts}"
    full_snap = f"{dataset}@{snap_name}"
    res = subprocess.run(["zfs", "snapshot", full_snap], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Échec création snapshot ZFS {full_snap} : {res.stderr.strip()}")
    return full_snap, core.snapshot_source_path(mountpoint, snap_name, rel_subpath)


def _destroy_zfs_snapshot(full_snap: str) -> None:
    res = subprocess.run(["zfs", "destroy", full_snap], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        print(f"cockpit-rclone: avertissement destruction snapshot {full_snap} : {res.stderr.strip()}",
              file=sys.stderr)


def _run_subprocess(
    argv: list[str],
    *,
    user: int | None = None,
    group: int | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Lance rclone ; avec user/group, perd les droits root avant exec (groupes annexes vidés)."""
    if user is not None:
        proc = subprocess.Popen(argv, user=user, group=group, extra_groups=[], env=env, umask=0o077)
    else:
        proc = subprocess.Popen(argv, env=env)

    def _forward(signum: int, _frame: Any) -> None:
        if proc.poll() is None:
            proc.send_signal(signum)

    old_term = signal.signal(signal.SIGTERM, _forward)
    old_int = signal.signal(signal.SIGINT, _forward)
    try:
        return proc.wait()
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def run_job(job_id: str, dry_run: bool = False) -> int:
    jid = core.validate_job_id(job_id)
    state = _load_state()
    job = (state.get("jobs") or {}).get(jid)
    if not job:
        print(f"cockpit-rclone: tâche introuvable : {jid}", file=sys.stderr)
        return 2

    config_path = str(state.get("config_path") or DEFAULT_RCLONE_CONF)
    rc_port = _pick_free_port()
    rc_addr = f"127.0.0.1:{rc_port}"
    rc_user = "cockpit"
    rc_pass = secrets.token_urlsafe(24)

    created_snap: str | None = None
    source_override: str | None = None
    runtime_path: Path | None = None

    try:
        if job.get("zfs_snapshot") and job.get("mode") in ("copy", "sync", "check"):
            snap = _create_zfs_snapshot(jid, str(job.get("source", "")))
            if snap:
                created_snap, source_override = snap
                print(f"cockpit-rclone: snapshot ZFS atomique créé : {created_snap} -> {source_override}",
                      flush=True)

        ts_suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        argv = core.build_job_argv(
            job,
            config_path=config_path,
            dry_run=dry_run,
            source_override=source_override,
            rc_addr=rc_addr,
            rc_user=rc_user,
            rc_pass=rc_pass,
            backup_dir_suffix=ts_suffix,
        )
        runtime_path = _write_runtime("job", jid, {
            "id": jid,
            "pid": os.getpid(),
            "rc_addr": rc_addr,
            "rc_user": rc_user,
            "rc_pass": rc_pass,
            "started_at": time.time(),
            "dry_run": bool(dry_run or job.get("dry_run")),
            "zfs_snapshot": created_snap,
            "mode": job.get("mode"),
            "source": source_override or job.get("source"),
            "destination": job.get("destination"),
        })
        return _run_subprocess(argv)
    finally:
        if runtime_path is not None:
            runtime_path.unlink(missing_ok=True)
        if created_snap is not None:
            _destroy_zfs_snapshot(created_snap)


def _unmount_stale(mountpoint: str) -> None:
    for tool in ("fusermount3", "fusermount"):
        try:
            subprocess.run([tool, "-uz", mountpoint], capture_output=True, text=True, check=False)
            return
        except OSError:
            continue


def prep_mount(mount_id: str) -> int:
    mid = core.validate_job_id(mount_id)
    state = _load_state()
    mount = (state.get("mounts") or {}).get(mid)
    if not mount:
        print(f"cockpit-rclone: montage introuvable : {mid}", file=sys.stderr)
        return 2
    mp = core.validate_path(str(mount["mountpoint"]), "mountpoint")
    _unmount_stale(mp)
    Path(mp).mkdir(parents=True, exist_ok=True)
    if mount.get("cache_dir"):
        Path(core.validate_path(str(mount["cache_dir"]), "cache_dir")).mkdir(parents=True, exist_ok=True)
    return 0


def stop_mount(mount_id: str) -> int:
    mid = core.validate_job_id(mount_id)
    state = _load_state()
    mount = (state.get("mounts") or {}).get(mid)
    if not mount:
        return 0
    _unmount_stale(str(mount["mountpoint"]))
    return 0


def run_mount(mount_id: str) -> int:
    mid = core.validate_job_id(mount_id)
    state = _load_state()
    mount = (state.get("mounts") or {}).get(mid)
    if not mount:
        print(f"cockpit-rclone: montage introuvable : {mid}", file=sys.stderr)
        return 2
    config_path = str(state.get("config_path") or DEFAULT_RCLONE_CONF)
    Path(str(mount["mountpoint"])).mkdir(parents=True, exist_ok=True)
    if mount.get("cache_dir"):
        Path(str(mount["cache_dir"])).mkdir(parents=True, exist_ok=True)
    argv = core.build_mount_argv(mount, config_path=config_path)
    return _run_subprocess(argv)


def _prepare_serve_runtime(sid: str, config_path: str, run_as: str) -> tuple[str, int, int]:
    """Prépare /run/cockpit-rclone/serve-<id>/ (0700, propriétaire = utilisateur d'exécution)
    avec une copie 0600 de rclone.conf : rclone serve tourne SANS les droits root et ne peut
    donc pas lire /etc/cockpit-rclone ni /root. Rend (chemin de la copie, uid, gid).
    Limite connue : un jeton OAuth rafraîchi par serve n'est pas réécrit dans le vrai
    rclone.conf (la copie est refaite à chaque démarrage)."""
    import pwd

    try:
        pw = pwd.getpwnam(run_as)
    except KeyError:
        raise RuntimeError(f"Utilisateur d'exécution introuvable : {run_as} (créé par le paquet : cockpit-rclone)")
    if pw.pw_uid == 0:
        raise RuntimeError("rclone serve ne tourne jamais en root")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUN_DIR, 0o711)  # traversable, non listable
    sdir = RUN_DIR / f"serve-{sid}"
    sdir.mkdir(mode=0o700, exist_ok=True)
    os.chown(sdir, pw.pw_uid, pw.pw_gid)
    os.chmod(sdir, 0o700)
    conf = sdir / "rclone.conf"
    tmp = sdir / f".rclone.conf.{os.getpid()}"
    src = Path(config_path)
    data = src.read_bytes() if src.exists() else b""
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chown(tmp, pw.pw_uid, pw.pw_gid)
    os.replace(tmp, conf)
    return str(conf), pw.pw_uid, pw.pw_gid


def run_serve(serve_id: str) -> int:
    sid = core.validate_job_id(serve_id)
    state = _load_state()
    serve = (state.get("serves") or {}).get(sid)
    if not serve:
        print(f"cockpit-rclone: serveur introuvable : {sid}", file=sys.stderr)
        return 2
    run_as = core.validate_run_as(str(serve.get("run_as") or ""))
    roots = core.serve_allowed_roots(_list_zfs_datasets(), state.get("serve_allowed_roots") or [])
    config_path = str(state.get("config_path") or DEFAULT_RCLONE_CONF)
    try:
        # Vérifie AVANT de préparer quoi que ce soit (adresse, authentification, dossier servi)
        core.build_serve_argv(serve, config_path=config_path, allowed_roots=roots)
        conf_copy, uid, gid = _prepare_serve_runtime(sid, config_path, run_as)
        argv = core.build_serve_argv(serve, config_path=conf_copy, allowed_roots=roots)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"cockpit-rclone: serveur {sid} refusé : {exc}", file=sys.stderr)
        return 2
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(RUN_DIR / f"serve-{sid}"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        **core.serve_env(serve),
    }
    return _run_subprocess(argv, user=uid, group=gid, env=env)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 2:
        print("Usage: rclone-job-runner.py <job|job-dry|mount|mount-prep|mount-stop|serve> <id>", file=sys.stderr)
        return 2
    kind, item_id = args[0], args[1]
    if kind == "job":
        return run_job(item_id, dry_run=False)
    if kind == "job-dry":
        return run_job(item_id, dry_run=True)
    if kind == "mount":
        return run_mount(item_id)
    if kind == "mount-prep":
        return prep_mount(item_id)
    if kind == "mount-stop":
        return stop_mount(item_id)
    if kind == "serve":
        return run_serve(item_id)
    print(f"cockpit-rclone: type inconnu : {kind}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
