"""Servidor del dashboard con corridas a demanda.

Sirve los reportes estáticos (como el http.server que reemplaza) y expone:
- POST /run?token=..&provider=..&minutes=..  -> dispara una corrida en background
  (provider: oechsle | plazavea | marketplace | todo | sellers)
- GET  /run/status                            -> estado de la corrida en curso

Seguridad: requiere el token de la env RUN_TOKEN (si no está seteada, el
endpoint queda deshabilitado). Solo una corrida a demanda a la vez.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_ALLOWED = ("oechsle", "plazavea", "marketplace", "todo", "sellers")


def _bin() -> str:
    """Ruta del ejecutable `homologador` (junto al Python en uso, ej. venv/bin)."""
    import sys
    cand = os.path.join(os.path.dirname(sys.executable), "homologador")
    return cand if os.path.exists(cand) else "homologador"
_state: dict = {"running": None, "started_at": None, "finished": None}
_lock = threading.Lock()


def _commands(provider: str, minutes: int, counts_only: bool = False) -> list[list[str]]:
    m = str(minutes)
    extra = ["--counts-only"] if counts_only else []
    if provider == "sellers":
        return [[_bin(), "sellers"]]
    if provider == "todo":
        sub = str(max(5, minutes // 3))
        return [
            [_bin(), "run", "--max-runtime", m, *extra],
            [_bin(), "--provider", "plazavea", "run", "--max-runtime", sub, *extra],
            [_bin(), "--provider", "marketplace", "run", "--max-runtime", sub, *extra],
            [_bin(), "sellers"],
        ]
    if provider == "oechsle":
        return [[_bin(), "run", "--max-runtime", m, *extra]]
    return [[_bin(), "--provider", provider, "run", "--max-runtime", m, *extra]]


def _launch(provider: str, minutes: int, counts_only: bool = False) -> None:
    def worker():
        try:
            for cmd in _commands(provider, minutes, counts_only):
                print(f"[webserver] ejecutando: {' '.join(cmd)}", flush=True)
                subprocess.run(cmd, check=False)
        finally:
            with _lock:
                _state["running"] = None
                _state["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            print("[webserver] corrida a demanda finalizada", flush=True)

    threading.Thread(target=worker, daemon=True).start()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # menos ruido en logs
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/run/status":
            with _lock:
                self._json(200, dict(_state))
            return
        super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/run":
            self._json(404, {"error": "no existe"})
            return
        q = parse_qs(u.query)
        expected = os.environ.get("RUN_TOKEN", "")
        if not expected:
            self._json(403, {"error": "corridas a demanda deshabilitadas (falta RUN_TOKEN)"})
            return
        if (q.get("token") or [""])[0] != expected:
            self._json(401, {"error": "token inválido"})
            return
        provider = (q.get("provider") or ["oechsle"])[0]
        if provider not in _ALLOWED:
            self._json(400, {"error": f"provider inválido (opciones: {', '.join(_ALLOWED)})"})
            return
        try:
            minutes = max(1, min(120, int((q.get("minutes") or ["15"])[0])))
        except ValueError:
            minutes = 15
        counts_only = (q.get("counts_only") or ["0"])[0] in ("1", "true")
        with _lock:
            if _state["running"]:
                self._json(409, {"error": f"ya hay una corrida en curso ({_state['running']})",
                                 "running": _state["running"]})
                return
            _state["running"] = provider + (" (conteo)" if counts_only else "")
            _state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _launch(provider, minutes, counts_only)
        self._json(200, {"ok": True, "provider": provider, "minutes": minutes,
                          "counts_only": counts_only})


def main(port: int, directory: str) -> None:
    handler = partial(Handler, directory=directory)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"[webserver] dashboard en :{port} (dir {directory}) · "
          f"corridas a demanda {'ON' if os.environ.get('RUN_TOKEN') else 'OFF (sin RUN_TOKEN)'}",
          flush=True)
    server.serve_forever()
