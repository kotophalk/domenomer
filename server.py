#!/usr/bin/env python3
"""Minimal proxy server for Ahrefs DR API (CORS bypass + API key). Zero dependencies."""

import http.server
import urllib.request
import urllib.parse
import urllib.error
import json
import os

PORT = 3000
AHREFS_API = "https://api.ahrefs.com/v3/public/domain-rating-free"
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(STATIC_DIR, ".env")


def load_env_file(path):
    """Читает KEY=VALUE из .env, не перезаписывая уже заданные переменные окружения."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file(ENV_FILE)
API_KEY = os.environ.get("AHREFS_API_KEY", "").strip()


def _ahrefs_error_text(body):
    """Превращает тело ошибки Ahrefs в строку.

    Встречаются форматы: ["Error", "Unauthorized"], {"error": "..."}, произвольный текст.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if isinstance(data, dict):
        data = data.get("error", body)
    if isinstance(data, list):
        return ": ".join(str(x) for x in data)
    return str(data)

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/dr":
            self._proxy(parsed)
        else:
            self._static(parsed)

    def _proxy(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get("target", [None])[0]

        if not target:
            self._json_response(400, {"error": "missing target"})
            return

        if not API_KEY:
            self._json_response(500, {"error": "Не задан AHREFS_API_KEY (см. .env.example)"})
            return

        api_url = f"{AHREFS_API}?target={urllib.parse.quote(target)}&output=json"

        try:
            req = urllib.request.Request(api_url, headers={
                "User-Agent": "BulkDRChecker/1.0",
                "Accept": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            extra = {}
            if e.headers.get("Retry-After"):
                extra["Retry-After"] = e.headers["Retry-After"]  # фронтенд использует при 429
            self._json_response(e.code, {"error": _ahrefs_error_text(body)[:200]}, extra)
        except Exception as e:
            self._json_response(502, {"error": str(e)})

    def _static(self, parsed):
        file_path = parsed.path
        if file_path == "/":
            file_path = "/index.html"

        file_path = os.path.join(STATIC_DIR, file_path.lstrip("/"))
        ext = os.path.splitext(file_path)[1]

        if ext not in MIME or not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        with open(file_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", MIME[ext])
        self.end_headers()
        self.wfile.write(data)

    def _json_response(self, code, obj, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def log_message(self, fmt, *args):
        # Compact logging
        print(f"  {args[0]}")


if __name__ == "__main__":
    # Threading — иначе параллельные запросы фронтенда выстраиваются в очередь на сервере
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n  ⚡ DR Checker запущен: http://localhost:{PORT}")
    if API_KEY:
        print(f"  🔑 AHREFS_API_KEY: ...{API_KEY[-4:]}\n")
    else:
        print("  ⚠️  AHREFS_API_KEY не задан — проверка DR работать не будет.")
        print("     Скопируйте .env.example в .env и впишите ключ Ahrefs APIv3.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Остановлен.")
