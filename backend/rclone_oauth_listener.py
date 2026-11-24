#!/usr/bin/env python3
"""rclone_oauth_listener — serveur loopback temporaire (127.0.0.1:53682) pour OAuth2 Google Drive.

Utilisé par `cockpit-rclone` :
  - écoute uniquement sur 127.0.0.1 (jamais exposé sur le réseau) pendant 5 min max ;
  - reçoit le callback Google `/?state=...&code=...` lorsque le navigateur est sur la
    même machine (ex. Z640) ou via un tunnel SSH `-L 53682:127.0.0.1:53682` ;
  - échange le code auprès de https://oauth2.googleapis.com/token avec PKCE S256 ;
  - écrit le token JSON dans rclone.conf en mode 0600 (atomique) puis s'arrête proprement ;
  - expose aussi `exchange_and_store_oauth_code()` réutilisé par `rclone-ctl gdrive-oauth-finish`
    pour finaliser une connexion depuis un NAS distant sans tunnel SSH.
"""
from __future__ import annotations

import configparser
import html
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import rclone_core as core  # noqa: E402


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


def _read_sections(cfg_path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if cfg_path.is_file():
        parser.read_string(cfg_path.read_text(encoding="utf-8"))
    return {sec: dict(parser.items(sec)) for sec in parser.sections()}


def exchange_oauth_code_with_google(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: str,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Échange un code d'autorisation Google contre access_token + refresh_token."""
    form = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }).encode("utf-8")
    req = urllib.request.Request(
        core.GOOGLE_OAUTH_TOKEN_URL,
        data=form,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
            err_json = json.loads(body)
            desc = err_json.get("error_description") or err_json.get("error") or body
        except Exception:
            desc = body or str(exc)
        raise RuntimeError(f"Échec de l'échange OAuth Google (HTTP {exc.code}) : {desc}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Impossible de joindre oauth2.googleapis.com : {exc.reason}") from exc


def exchange_and_store_oauth_code(
    session_path: Path,
    code: str,
    state_received: str | None = None,
    *,
    exchange_fn=exchange_oauth_code_with_google,
) -> dict[str, Any]:
    """Finalise une session OAuth : vérifie state, obtient le token et met à jour rclone.conf."""
    if not session_path.is_file():
        raise ValueError("Session OAuth introuvable ou déjà expirée. Relancez « Ouvrir la connexion Google ».")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    if session.get("status") == "completed":
        return {"ok": True, "remote": session.get("remote"), "already_completed": True}
    if time.time() > float(session.get("expires_at") or 0):
        session["status"] = "expired"
        session["error"] = "Session OAuth expirée (délai de 5 min dépassé)."
        _clear_session_secrets(session)
        _atomic_write(session_path, json.dumps(session), 0o600)
        raise ValueError(session["error"])

    expected_state = str(session.get("state") or "")
    if state_received is not None and expected_state and state_received != expected_state:
        raise ValueError("Paramètre state OAuth incohérent (session différente ou requête périmée).")

    token_resp = exchange_fn(
        code=code,
        client_id=str(session["client_id"]),
        client_secret=str(session["client_secret"]),
        redirect_uri=str(session["redirect_uri"]),
        code_verifier=str(session["code_verifier"]),
    )
    token_json = core.format_rclone_oauth_token(token_resp)

    cfg_path = Path(str(session["config_path"]))
    remote_name = core.validate_remote_name(str(session["remote"]))
    sections = _read_sections(cfg_path)
    sec = dict(sections.get(remote_name, {"type": "drive"}))
    sec["type"] = "drive"
    sec["client_id"] = str(session["client_id"])
    sec["client_secret"] = str(session["client_secret"])
    if session.get("scope"):
        sec["scope"] = str(session["scope"])
    if session.get("team_drive"):
        sec["team_drive"] = str(session["team_drive"])
    if session.get("root_folder_id"):
        sec["root_folder_id"] = str(session["root_folder_id"])
    sec["token"] = token_json
    sections[remote_name] = sec
    _atomic_write(cfg_path, core.dump_rclone_conf(sections), 0o600)

    session["status"] = "completed"
    session["completed_at"] = time.time()
    session["error"] = ""
    _clear_session_secrets(session)
    _atomic_write(session_path, json.dumps(session), 0o600)
    return {"ok": True, "remote": remote_name, "has_refresh_token": bool(token_resp.get("refresh_token"))}


def _clear_session_secrets(session: dict[str, Any]) -> None:
    for k in ("client_secret", "code_verifier"):
        session.pop(k, None)


def _html_page(title: str, heading: str, message: str, ok: bool) -> bytes:
    color = "#1f7a3e" if ok else "#b91c1c"
    body = (
        "<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:60px auto;padding:24px;"
        "background:#f8fafc;color:#0f172a}.card{background:#fff;border:1px solid #cbd5e1;"
        f"border-top:4px solid {color};border-radius:8px;padding:24px}}h1{{margin-top:0;color:{color};font-size:20px}}</style>"
        f"</head><body><div class='card'><h1>{html.escape(heading)}</h1>"
        f"<p>{html.escape(message)}</p></div></body></html>"
    )
    return body.encode("utf-8")


class _ReusableHTTPServer(http.server.HTTPServer):
    allow_reuse_address = True


def run_listener(session_path: Path, port: int = 53682, timeout_sec: float = 300.0) -> int:
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if qs.get("error"):
                err = qs["error"][0]
                desc = (qs.get("error_description") or [""])[0]
                msg = f"Google a refusé l'autorisation : {err}" + (f" ({desc})" if desc else "")
                self._mark_error(msg)
                self._reply(400, "Autorisation refusée", "Autorisation Google annulée", msg, False)
                done.set()
                return
            code = (qs.get("code") or [""])[0].strip()
            state_rx = (qs.get("state") or [None])[0]
            if not code:
                self._reply(
                    400,
                    "Callback incomplet",
                    "Paramètre code manquant",
                    "Cette adresse attend la redirection automatique depuis accounts.google.com.",
                    False,
                )
                return
            try:
                res = exchange_and_store_oauth_code(session_path, code, state_rx)
                self._reply(
                    200,
                    "Connexion Google réussie",
                    f"Remote « {res['remote']}: » connecté à Google Drive",
                    "Le jeton OAuth a été enregistré dans rclone.conf. Vous pouvez fermer cet onglet et retourner dans Cockpit.",
                    True,
                )
            except Exception as exc:
                self._mark_error(str(exc))
                self._reply(500, "Erreur OAuth", "Impossible de finaliser la connexion Google", str(exc), False)
            done.set()

        def _mark_error(self, msg: str) -> None:
            try:
                if session_path.is_file():
                    s = json.loads(session_path.read_text(encoding="utf-8"))
                    s["status"] = "error"
                    s["error"] = msg
                    _clear_session_secrets(s)
                    _atomic_write(session_path, json.dumps(s), 0o600)
            except Exception:
                pass

        def _reply(self, status: int, title: str, heading: str, message: str, ok: bool) -> None:
            payload = _html_page(title, heading, message, ok)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    try:
        server = _ReusableHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return 2

    server.timeout = 0.5
    deadline = time.time() + timeout_sec
    try:
        while not done.is_set() and time.time() < deadline:
            server.handle_request()
            if not session_path.is_file():
                break
            try:
                s = json.loads(session_path.read_text(encoding="utf-8"))
                if s.get("status") in ("completed", "error", "expired"):
                    break
            except Exception:
                break
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return 1
    session_path = Path(args[0])
    port = int(args[1]) if len(args) > 1 else 53682
    return run_listener(session_path, port=port)


if __name__ == "__main__":
    sys.exit(main())
