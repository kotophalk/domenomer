#!/usr/bin/env python3
"""Доменомер — сервер: статика + прокси к бесплатному Ahrefs DR API. Только stdlib.

Публичный режим: ключ Ahrefs один на всех посетителей, поэтому лимит апстрима
(60 запросов в минуту на ключ) соблюдается здесь, глобально, — скользящим окном
с FIFO-очередью. Успешные ответы кэшируются, чтобы популярные домены не
расходовали лимит. Ключ никогда не уходит в браузер.
"""

import collections
import email.utils
import http.server
import json
import logging
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
ENV_FILE = os.path.join(BASE_DIR, ".env")


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


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --- Конфиг (переменные окружения; локально можно через .env) ---
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = _env_int("PORT", 3000)
API_KEY = os.environ.get("AHREFS_API_KEY", "").strip()
AHREFS_API = os.environ.get("AHREFS_API_URL", "https://api.ahrefs.com/v3/public/domain-rating-free")
UPSTREAM_TIMEOUT = _env_float("UPSTREAM_TIMEOUT", 15)
# Лимит Ahrefs на ключ: 60 запросов в минуту, при превышении — 429.
UPSTREAM_RATE_PER_MIN = _env_int("UPSTREAM_RATE_PER_MIN", 60)
# Сколько секунд запрос ждёт слот в очереди к Ahrefs; дольше — 429 с Retry-After.
QUEUE_MAX_WAIT = _env_float("QUEUE_MAX_WAIT", 30)
# Одновременных запросов к /api/dr с одного IP (фронтенд держит 4).
PER_IP_CONCURRENCY = _env_int("PER_IP_CONCURRENCY", 6)
# Доменов за один прогон — сообщается фронтенду через /api/limits.
MAX_DOMAINS = _env_int("MAX_DOMAINS", 200)
# Кэш успешных ответов: секунд и записей (0 — выключить).
CACHE_TTL = _env_int("CACHE_TTL", 86400)
CACHE_MAX = _env_int("CACHE_MAX", 50000)
# 1 — брать IP клиента из X-Forwarded-For / X-Real-IP (за reverse proxy).
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
# Origin'ы, которым разрешён API из браузера ("*" или список через запятую). Пусто — CORS выключен.
CORS_ALLOW_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "").strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}

log = logging.getLogger("domenomer")


# --- Вспомогательное ---

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


_TARGET_RE = re.compile(r"^[a-z0-9.-]{1,253}$")


def normalize_target(raw):
    """Домен → ключ кэша и параметр для Ahrefs (punycode). None — некорректный ввод."""
    t = (raw or "").strip().lower()
    t = re.sub(r"^[a-z][a-z0-9+.-]*://", "", t)   # схема
    t = t.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    t = t.split("@")[-1].split(":", 1)[0]          # user@ и :порт
    if t.startswith("www."):
        t = t[4:]
    t = t.strip(".")
    if not t or "." not in t:
        return None
    try:
        t = t.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if not _TARGET_RE.match(t) or ".." in t:
        return None
    return t


def parse_retry_after(value, default):
    """Retry-After в секундах (число или HTTP-дата); при мусоре — default."""
    if not value:
        return default
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return default


class UpstreamLimiter:
    """Скользящее окно: не более `limit` стартов за `period` секунд, очередь FIFO.

    Ahrefs считает лимит на ключ, ключ один на всех — поэтому лимитер один на процесс.
    `pause()` (после 429 от Ahrefs) останавливает выдачу слотов всем.
    """

    def __init__(self, limit, period=60.0):
        self.limit = max(1, limit)
        self.period = period
        self._starts = collections.deque()   # monotonic-время последних стартов
        self._queue = collections.deque()    # ожидающие, в порядке прихода
        self._paused_until = 0.0
        self._cond = threading.Condition()

    def _trim(self, now):
        while self._starts and self._starts[0] <= now - self.period:
            self._starts.popleft()

    def _eta(self, now, ahead):
        """Через сколько секунд получит слот тот, перед кем в очереди `ahead` человек."""
        need = ahead + 1 - (self.limit - len(self._starts))
        if need <= 0:
            t = now
        elif need <= len(self._starts):
            t = self._starts[need - 1] + self.period
        else:
            t = self._starts[-1] + self.period + (need - len(self._starts)) * self.period / self.limit
        return max(t, self._paused_until) - now

    def acquire(self, timeout):
        """Ждёт слот не дольше `timeout` с. Возвращает (True, 0) или (False, через_сколько_повторить)."""
        with self._cond:
            now = time.monotonic()
            self._trim(now)
            eta = self._eta(now, len(self._queue))
            if eta > timeout:
                return False, eta
            me = object()
            self._queue.append(me)
            deadline = now + timeout
            try:
                while True:
                    now = time.monotonic()
                    self._trim(now)
                    if self._queue[0] is me:
                        eta = self._eta(now, 0)
                        if eta <= 0:
                            self._starts.append(now)
                            return True, 0.0
                        if now + eta > deadline:
                            return False, eta
                        self._cond.wait(min(eta, deadline - now))
                    else:
                        if now >= deadline:
                            return False, self._eta(now, self._queue.index(me))
                        self._cond.wait(deadline - now)
            finally:
                self._queue.remove(me)
                self._cond.notify_all()

    def pause(self, seconds):
        with self._cond:
            self._paused_until = max(self._paused_until, time.monotonic() + seconds)
            self._cond.notify_all()

    def stats(self):
        with self._cond:
            now = time.monotonic()
            self._trim(now)
            return {
                "window_used": len(self._starts),
                "queue": len(self._queue),
                "paused_for": max(0.0, round(self._paused_until - now, 1)),
            }


class TTLCache:
    """LRU-кэш с TTL. ttl<=0 или maxsize<=0 — выключен."""

    def __init__(self, ttl, maxsize):
        self.ttl = ttl
        self.maxsize = maxsize
        self._d = collections.OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return self.ttl > 0 and self.maxsize > 0

    def get(self, key):
        if not self.enabled:
            return None
        with self._lock:
            item = self._d.get(key)
            if item is None:
                return None
            value, expires = item
            if expires <= time.monotonic():
                del self._d[key]
                return None
            self._d.move_to_end(key)
            return value

    def set(self, key, value):
        if not self.enabled:
            return
        with self._lock:
            self._d[key] = (value, time.monotonic() + self.ttl)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def __len__(self):
        with self._lock:
            return len(self._d)


class ConcurrencyGuard:
    """Не более `limit` одновременных запросов на ключ (IP)."""

    def __init__(self, limit):
        self.limit = limit
        self._counts = collections.Counter()
        self._lock = threading.Lock()

    def acquire(self, key):
        with self._lock:
            if self.limit > 0 and self._counts[key] >= self.limit:
                return False
            self._counts[key] += 1
            return True

    def release(self, key):
        with self._lock:
            self._counts[key] -= 1
            if self._counts[key] <= 0:
                del self._counts[key]


limiter = UpstreamLimiter(UPSTREAM_RATE_PER_MIN)
cache = TTLCache(CACHE_TTL, CACHE_MAX)
ip_guard = ConcurrencyGuard(PER_IP_CONCURRENCY)


def fetch_upstream(target):
    """Запрос к Ahrefs. Возвращает (status, body_bytes, headers)."""
    url = f"{AHREFS_API}?target={urllib.parse.quote(target)}&output=json"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Domenomer/1.0 (+https://domenomer.delosvod.ru)",
        "Accept": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Domenomer/1.0"
    sys_version = ""

    # --- маршрутизация ---

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/dr":
            self._api_dr(parsed)
        elif path == "/api/limits":
            self._json(200, {
                "max_domains": MAX_DOMAINS,
                "upstream_rate_per_min": UPSTREAM_RATE_PER_MIN,
                "cache_ttl": CACHE_TTL if cache.enabled else 0,
            })
        elif path == "/healthz":
            if API_KEY:
                self._json(200, {"status": "ok", "limiter": limiter.stats(), "cache_size": len(cache)})
            else:
                self._json(503, {"status": "no_api_key"})
        elif path == "/robots.txt":
            self._raw(200, b"User-agent: *\nAllow: /\nDisallow: /api/\n", "text/plain; charset=utf-8")
        else:
            self._static(path)

    # --- API ---

    def _client_ip(self):
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
            xri = self.headers.get("X-Real-IP")
            if xri:
                return xri.strip()
        return self.client_address[0]

    def _api_dr(self, parsed):
        t0 = time.monotonic()
        ip = self._client_ip()
        qs = urllib.parse.parse_qs(parsed.query)
        raw = qs.get("target", [""])[0]
        target = normalize_target(raw)

        if not target:
            self._json(400, {"error": "Некорректный домен"})
            return
        if not API_KEY:
            self._json(503, {"error": "Сервис не настроен: не задан AHREFS_API_KEY"})
            return

        cached = cache.get(target)
        if cached is not None:
            self._raw(200, cached, "application/json", {"X-Cache": "HIT"})
            log.info("%s %s 200 cache=HIT %dms", ip, target, (time.monotonic() - t0) * 1000)
            return

        if not ip_guard.acquire(ip):
            self._json(429, {"error": "Слишком много одновременных запросов, повторите позже"},
                       {"Retry-After": "2"})
            log.info("%s %s 429 per-ip", ip, target)
            return
        try:
            ok, eta = limiter.acquire(QUEUE_MAX_WAIT)
            if not ok:
                retry = max(1, math.ceil(eta))
                self._json(429, {"error": "Очередь к Ahrefs заполнена, повторите позже"},
                           {"Retry-After": str(retry)})
                log.info("%s %s 429 queue retry=%ss", ip, target, retry)
                return

            try:
                status, body, headers = fetch_upstream(target)
            except Exception as e:  # таймаут, DNS, сеть
                self._json(502, {"error": f"Ahrefs недоступен: {e}"})
                log.warning("%s %s 502 upstream error: %s", ip, target, e)
                return

            waited = (time.monotonic() - t0) * 1000
            if status == 200:
                # Проверяем форму ответа, прежде чем кэшировать
                try:
                    dr = json.loads(body)["domain_rating"]["domain_rating"]
                except (ValueError, KeyError, TypeError):
                    self._json(502, {"error": "Неожиданный ответ Ahrefs"})
                    log.warning("%s %s 502 bad upstream body: %r", ip, target, body[:200])
                    return
                cache.set(target, body)
                self._raw(200, body, "application/json", {"X-Cache": "MISS"})
                log.info("%s %s 200 dr=%s cache=MISS %dms", ip, target, dr, waited)
                return

            text = _ahrefs_error_text(body.decode("utf-8", errors="replace"))[:200]
            if status == 429:
                # Лимит на ключ исчерпан — тормозим всех, не только этого клиента
                pause = parse_retry_after(headers.get("Retry-After"), 5.0)
                limiter.pause(pause)
                self._json(429, {"error": f"Лимит Ahrefs: {text}"}, {"Retry-After": str(max(1, math.ceil(pause)))})
                log.warning("%s %s 429 upstream, pause %.0fs", ip, target, pause)
                return
            if 500 <= status < 600:
                self._json(502, {"error": f"Ошибка Ahrefs ({status}): {text}"})
            else:
                self._json(status, {"error": text})
            log.info("%s %s %s upstream: %s", ip, target, status, text)
        finally:
            ip_guard.release(ip)

    # --- статика ---

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        rel = os.path.normpath(urllib.parse.unquote(path).lstrip("/"))
        full = os.path.join(STATIC_DIR, rel)
        ext = os.path.splitext(full)[1].lower()
        # normpath уже схлопнул "..": проверяем, что не вышли за пределы static/
        if (not full.startswith(STATIC_DIR + os.sep)
                or ext not in MIME
                or not os.path.isfile(full)):
            self._raw(404, b"Not found", "text/plain; charset=utf-8")
            return
        with open(full, "rb") as f:
            data = f.read()
        # HTML без кэша, чтобы деплой подхватывался сразу; ассеты — ненадолго
        cache_control = "no-cache" if ext == ".html" else "public, max-age=300"
        self._raw(200, data, MIME[ext], {"Cache-Control": cache_control})

    # --- ответы ---

    def _json(self, code, obj, extra_headers=None):
        self._raw(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                  "application/json; charset=utf-8", extra_headers)

    def _raw(self, code, body, content_type, extra_headers=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if CORS_ALLOW_ORIGIN and self.path.startswith("/api/"):
                origin = self.headers.get("Origin", "")
                allowed = CORS_ALLOW_ORIGIN == "*" or origin in [o.strip() for o in CORS_ALLOW_ORIGIN.split(",")]
                if allowed:
                    self.send_header("Access-Control-Allow-Origin", "*" if CORS_ALLOW_ORIGIN == "*" else origin)
                    self.send_header("Vary", "Origin")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # клиент ушёл, не ждя ответа

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)


def make_server(host=HOST, port=PORT):
    # Threading — иначе запросы выстраиваются в очередь на сервере, а не в лимитере
    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def main():
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    server = make_server()
    log.info("Доменомер запущен: http://%s:%s (лимит %s/мин, очередь до %.0fs, кэш %ss)",
             HOST, PORT, UPSTREAM_RATE_PER_MIN, QUEUE_MAX_WAIT, CACHE_TTL if cache.enabled else "выкл")
    if API_KEY:
        log.info("AHREFS_API_KEY: ...%s", API_KEY[-4:])
    else:
        log.warning("AHREFS_API_KEY не задан — /api/dr отвечает 503. Скопируйте .env.example в .env и впишите ключ.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Остановлен.")


if __name__ == "__main__":
    main()
