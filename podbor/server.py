"""HTTP-сервер без внешних зависимостей: веб-страница + JSON API.

    python -m podbor.server            # http://127.0.0.1:8000
    python -m podbor.server --port 9000 --host 0.0.0.0

Эндпоинты:
    GET  /                    веб-форма
    GET  /api/options         города, категории, форматы, языки из данных
    GET  /api/recommend?...   подбор (параметры в query string)
    POST /api/recommend       подбор (JSON в теле)
    GET  /api/scenarios       готовые демо-запросы под загруженные данные
    GET  /api/health          состояние и источник данных
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .data import WINDOW_END, WINDOW_START, load_vendors, norm
from . import ai
from .engine import FORMATS, LANGUAGES, recommend
from .scenarios import find_all

STATIC = Path(__file__).resolve().parent.parent / "static"
VENDORS, INFO = load_vendors()
SCENARIOS = find_all(VENDORS)


def options() -> dict:
    cats = {}
    for v in VENDORS:
        for c in v.categories:
            cats.setdefault(norm(c), {"name": c, "count": 0})["count"] += 1
    cities = sorted({v.city for v in VENDORS if v.city})
    return {
        "cities": cities,
        "categories": sorted(cats.values(), key=lambda x: (-x["count"], x["name"])),
        "formats": FORMATS, "languages": LANGUAGES,
        "window": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "data": INFO,
        "ai": ai.status(),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path in ("/favicon.svg", "/favicon.ico"):
            return self._send(200, (STATIC / "favicon.svg").read_bytes(), "image/svg+xml")
        if u.path == "/api/options":
            return self._json(options())
        if u.path == "/api/scenarios":
            return self._json(SCENARIOS)
        if u.path == "/api/health":
            return self._json({"status": "ok", "data": INFO})
        if u.path == "/api/recommend":
            params = {k: v[0] for k, v in parse_qs(u.query).items()}
            return self._json(recommend(VENDORS, params))
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/recommend", "/api/ai"):
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            params = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "тело запроса должно быть JSON"}, 400)
        if path == "/api/ai":
            opts = options()
            text = params.get("text", "") if isinstance(params, dict) else ""
            return self._json(ai.ask(VENDORS, str(text)[:1000], opts["cities"],
                                     [c["name"] for c in opts["categories"]]))
        self._json(recommend(VENDORS, params))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    a = p.parse_args()
    print(f"Данные: {INFO['file']} ({INFO['total']} профилей, синтетических {INFO['synthetic']})")
    st = ai.status()
    print(f"ИИ-агент: {'включён, модель ' + st['model'] if st['enabled'] else 'выключен (нет OPENAI_API_KEY)'}")
    print(f"Откройте http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
