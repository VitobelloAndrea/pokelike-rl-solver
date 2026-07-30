"""A tiny stdlib-only local HTTP server: serves the static web UI
(`pokelike/webui/static/`) and a small JSON API wrapping a single
`engine.Engine` instance. Stdlib-only deliberately -- this environment has
no `pip`-installed packages beyond the standard library (see
`docs/handover.md`'s environment gotchas), so no Flask/FastAPI dependency.

Single global `Engine` instance -- this is a local, single-player tool (run
it, open a browser tab, play one run), not a multi-user service. No auth,
no HTTPS, meant for `localhost` only.

Run with:
    python -m pokelike.webui.server [--port 8000]
then open http://localhost:8000/ in a browser.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from pokelike import engine
from pokelike.webui.state_json import ActionDecodeError, decode_action, encode_state

_STATIC_DIR = Path(__file__).parent / "static"

_engine = engine.Engine()


def _json_bytes(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


class _BadRequest(ValueError):
    """Raised by the scalar coercion helpers below; `do_POST` turns it into
    an HTTP 400, matching `ActionDecodeError`'s treatment in `/api/action`.
    """


def _to_bool(value, field: str) -> bool:
    """CODEX.md issue 47: `bool(x)` on a JSON value is Python TRUTHINESS,
    not a boolean parse -- `bool("false")` is `True`. A `reset` payload
    with `{"nuzlocke_mode": "false"}` (a plausible client mistake, e.g. a
    stringified form field) would silently turn Nuzlocke mode ON. Only
    accept an actual JSON boolean (or an absent/`None` field, handled by
    the caller's own default).
    """
    if isinstance(value, bool):
        return value
    raise _BadRequest(f"{field!r} must be a boolean, got {value!r}")


def _to_int(value, field: str) -> int:
    if isinstance(value, bool):  # bool is an int subclass -- reject explicitly
        raise _BadRequest(f"{field!r} must be an integer, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _BadRequest(f"{field!r} must be an integer, got {value!r}") from None


class Handler(BaseHTTPRequestHandler):
    server_version = "PokelikeWebUI/1"

    # -----------------------------------------------------------------
    # Small response helpers
    # -----------------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_state(self) -> None:
        if _engine.state is None:
            self._send_json(409, {"error": "no active run; POST /api/reset first"})
            return
        self._send_json(200, encode_state(_engine.state))

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # -----------------------------------------------------------------
    # Static file serving (index.html, main.css, app.js, images)
    # -----------------------------------------------------------------

    def _serve_static(self, url_path: str) -> None:
        rel = url_path.lstrip("/") or "index.html"
        candidate = (_STATIC_DIR / rel).resolve()
        # Path-traversal guard -- reject anything that escapes _STATIC_DIR.
        if _STATIC_DIR.resolve() not in candidate.parents and candidate != _STATIC_DIR.resolve():
            self.send_error(403, "Forbidden")
            return
        if not candidate.is_file():
            self.send_error(404, "Not Found")
            return
        content_type, _ = mimetypes.guess_type(str(candidate))
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -----------------------------------------------------------------
    # Routing
    # -----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's naming convention
        path = urlparse(self.path).path
        if path == "/api/state":
            self._send_state()
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "malformed JSON body"})
            return

        if path == "/api/reset":
            try:
                seed = body.get("seed")
                passives = body.get("passives") or []
                kwargs = dict(
                    nuzlocke_mode=_to_bool(body.get("nuzlocke_mode", False), "nuzlocke_mode"),
                    gen2_mode=_to_bool(body.get("gen2_mode", False), "gen2_mode"),
                    gen3_mode=_to_bool(body.get("gen3_mode", False), "gen3_mode"),
                    gen4_mode=_to_bool(body.get("gen4_mode", False), "gen4_mode"),
                    shiny_charm=_to_bool(body.get("shiny_charm", False), "shiny_charm"),
                    seed=None if seed is None else _to_int(seed, "seed"),
                    passives=[engine.battle.Trait(id=t) for t in passives],
                )
            except _BadRequest as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                state = _engine.reset(**kwargs)
            except ValueError as exc:
                # e.g. more than one of gen2/gen3/gen4_mode set at once.
                self._send_json(409, {"error": str(exc)})
                return
            self._send_json(200, encode_state(state))
            return

        if path == "/api/action":
            if _engine.state is None:
                self._send_json(409, {"error": "no active run; POST /api/reset first"})
                return
            try:
                action = decode_action(body)
            except ActionDecodeError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                state = _engine.step(action)
            except ValueError as exc:
                # A well-formed but illegal action (wrong phase, index out
                # of range, inaccessible node, ...) -- engine.py's own
                # ValueError, not a decode error. 409 Conflict: the request
                # was understood but not valid given current state.
                self._send_json(409, {"error": str(exc)})
                return
            self._send_json(200, encode_state(state))
            return

        self.send_error(404, "Not Found")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 -- stdlib's own signature
        pass  # quiet by default; flip this on for debugging


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Pokelike web UI: http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
