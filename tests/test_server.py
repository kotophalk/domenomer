"""Офлайн-тесты сервера: лимитер, кэш, нормализация и HTTP-контракт с mock-апстримом.

Запуск: python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from unittest import mock

os.environ.setdefault("AHREFS_API_KEY", "test-key")
os.environ["QUEUE_MAX_WAIT"] = "2"
os.environ["PER_IP_CONCURRENCY"] = "2"
os.environ["TRUST_PROXY"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


# --- чистые функции ---

class NormalizeTargetTest(unittest.TestCase):
    def test_strips_scheme_path_www_and_lowercases(self):
        self.assertEqual(server.normalize_target("HTTPS://www.Example.com/path?q=1#x"), "example.com")

    def test_idna(self):
        self.assertEqual(server.normalize_target("доменомер.рф"), "xn--d1aca0abfedu.xn--p1ai")

    def test_userinfo_and_port(self):
        self.assertEqual(server.normalize_target("user@host.ru:8080"), "host.ru")

    def test_rejects_garbage(self):
        for bad in ["", "nodot", "a..b.com", "http://", "   ", "a b.com", "-" * 300 + ".com"]:
            self.assertIsNone(server.normalize_target(bad), bad)


class AhrefsErrorTextTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(server._ahrefs_error_text('["Error","Unauthorized"]'), "Error: Unauthorized")
        self.assertEqual(server._ahrefs_error_text('{"error":"Rate limited"}'), "Rate limited")
        self.assertEqual(server._ahrefs_error_text("plain text"), "plain text")
        self.assertEqual(server._ahrefs_error_text('{"foo":1}'), '{"foo":1}')


class ParseRetryAfterTest(unittest.TestCase):
    def test_seconds_and_junk(self):
        self.assertEqual(server.parse_retry_after("7", 5), 7.0)
        self.assertEqual(server.parse_retry_after("junk", 5), 5)
        self.assertEqual(server.parse_retry_after(None, 5), 5)

    def test_http_date(self):
        self.assertGreater(server.parse_retry_after("Thu, 01 Jan 2099 00:00:00 GMT", 5), 1000)


# --- лимитер ---

class UpstreamLimiterTest(unittest.TestCase):
    def test_burst_then_wait(self):
        lim = server.UpstreamLimiter(limit=3, period=0.6)
        for _ in range(3):
            self.assertEqual(lim.acquire(timeout=1), (True, 0.0))
        t0 = time.monotonic()
        ok, _ = lim.acquire(timeout=2)
        self.assertTrue(ok)
        # четвёртый ждал, пока первый старт не выйдет из окна
        self.assertGreaterEqual(time.monotonic() - t0, 0.5)

    def test_rejects_immediately_when_eta_exceeds_timeout(self):
        lim = server.UpstreamLimiter(limit=1, period=10)
        self.assertEqual(lim.acquire(timeout=1), (True, 0.0))
        t0 = time.monotonic()
        ok, eta = lim.acquire(timeout=0.5)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - t0, 0.2)  # не ждал впустую
        self.assertGreater(eta, 9)

    def test_pause_blocks_everyone(self):
        lim = server.UpstreamLimiter(limit=100, period=60)
        lim.pause(0.4)
        t0 = time.monotonic()
        ok, _ = lim.acquire(timeout=2)
        self.assertTrue(ok)
        self.assertGreaterEqual(time.monotonic() - t0, 0.35)

    def test_fifo_order(self):
        lim = server.UpstreamLimiter(limit=1, period=0.3)
        self.assertTrue(lim.acquire(timeout=1)[0])
        order = []
        lock = threading.Lock()

        def worker(n):
            ok, _ = lim.acquire(timeout=5)
            with lock:
                order.append((n, ok))

        threads = []
        for n in range(3):
            t = threading.Thread(target=worker, args=(n,))
            t.start()
            threads.append(t)
            time.sleep(0.05)  # чтобы очередь была детерминированной
        for t in threads:
            t.join()
        self.assertEqual(order, [(0, True), (1, True), (2, True)])

    def test_stats(self):
        lim = server.UpstreamLimiter(limit=5, period=60)
        lim.acquire(timeout=1)
        s = lim.stats()
        self.assertEqual(s["window_used"], 1)
        self.assertEqual(s["queue"], 0)


class TTLCacheTest(unittest.TestCase):
    def test_get_set_expire(self):
        c = server.TTLCache(ttl=0.2, maxsize=10)
        c.set("a", b"1")
        self.assertEqual(c.get("a"), b"1")
        time.sleep(0.25)
        self.assertIsNone(c.get("a"))

    def test_lru_eviction(self):
        c = server.TTLCache(ttl=60, maxsize=2)
        c.set("a", b"1")
        c.set("b", b"2")
        c.get("a")        # a — свежее
        c.set("c", b"3")  # вытесняется b
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.get("a"), b"1")
        self.assertEqual(c.get("c"), b"3")

    def test_disabled(self):
        c = server.TTLCache(ttl=0, maxsize=10)
        c.set("a", b"1")
        self.assertIsNone(c.get("a"))
        self.assertFalse(c.enabled)


class ConcurrencyGuardTest(unittest.TestCase):
    def test_limit_and_release(self):
        g = server.ConcurrencyGuard(2)
        self.assertTrue(g.acquire("ip"))
        self.assertTrue(g.acquire("ip"))
        self.assertFalse(g.acquire("ip"))
        self.assertTrue(g.acquire("other"))
        g.release("ip")
        self.assertTrue(g.acquire("ip"))


# --- HTTP-контракт ---

def _headers(**kw):
    m = Message()
    for k, v in kw.items():
        m[k.replace("_", "-")] = str(v)
    return m


class HttpTest(unittest.TestCase):
    """Поднимает настоящий сервер на свободном порту, апстрим подменён."""

    @classmethod
    def setUpClass(cls):
        cls.srv = server.make_server("127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        # Свежие лимитер/кэш/страж на каждый тест
        server.limiter = server.UpstreamLimiter(60)
        server.cache = server.TTLCache(3600, 1000)
        server.ip_guard = server.ConcurrencyGuard(2)
        self.upstream_calls = []

        def fake_upstream(target):
            self.upstream_calls.append(target)
            return self.upstream_response(target)

        self.upstream_response = lambda t: (200, json.dumps(
            {"domain_rating": {"domain_rating": 42.0, "license": "x"}}).encode(), _headers())
        patcher = mock.patch.object(server, "fetch_upstream", fake_upstream)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_static_index_and_assets(self):
        code, body, h = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"<title>", body)
        self.assertIn("text/html", h["Content-Type"])
        self.assertEqual(h["Cache-Control"], "no-cache")
        code, _, h = self.get("/app.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", h["Content-Type"])
        code, _, _ = self.get("/style.css")
        self.assertEqual(code, 200)

    def test_static_no_path_traversal(self):
        for p in ["/../server.py", "/..%2F..%2Fetc%2Fpasswd", "/%2e%2e/server.py", "/.env", "/nope.html"]:
            code, _, _ = self.get(p)
            self.assertEqual(code, 404, p)

    def test_index_without_metrika_id_has_no_counter(self):
        """По умолчанию (стенд, тесты, чужие копии) хиты в Метрику не шлются."""
        with mock.patch.object(server, "METRIKA_ID", ""):
            for p in ("/", "/index.html"):
                _, body, _ = self.get(p)
                self.assertNotIn(b"mc.yandex.ru", body, p)
                self.assertNotIn(b"__METRIKA_ID__", body, p)
                self.assertNotIn(b"metrika:start", body, p)
                self.assertNotIn(b'id="cookie-notice"', body, p)
                self.assertIn(b'href="/privacy"', body, p)  # ссылка в футере — всегда

    def test_privacy_page(self):
        code, body, h = self.get("/privacy")
        self.assertEqual(code, 200)
        self.assertIn("text/html", h["Content-Type"])
        self.assertIn("Политика конфиденциальности".encode(), body)
        self.assertIn(b"502917677947", body)
        self.assertEqual(self.get("/privacy.html")[0], 200)

    def test_index_with_metrika_id_renders_counter(self):
        with mock.patch.object(server, "METRIKA_ID", "12345678"):
            code, body, h = self.get("/")
        self.assertEqual(code, 200)
        self.assertEqual(int(h["Content-Length"]), len(body))
        self.assertIn(b"https://mc.yandex.ru/metrika/tag.js?id=12345678", body)
        self.assertIn(b'ym(12345678,"init"', body)
        self.assertIn(b"https://mc.yandex.ru/watch/12345678", body)
        self.assertIn(b"webvisor:false", body)
        self.assertNotIn(b"__METRIKA_ID__", body)
        self.assertIn(b'id="cookie-notice"', body)
        self.assertIn(b"nc_accepted=1", body)

    def test_healthz_and_limits_and_robots(self):
        code, body, _ = self.get("/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        code, body, _ = self.get("/api/limits")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["max_domains"], server.MAX_DOMAINS)
        code, body, _ = self.get("/robots.txt")
        self.assertEqual(code, 200)
        self.assertIn(b"Disallow: /api/", body)

    def test_healthz_503_without_key(self):
        with mock.patch.object(server, "API_KEY", ""):
            code, body, _ = self.get("/healthz")
            self.assertEqual(code, 503)
            code, body, _ = self.get("/api/dr?target=example.com")
            self.assertEqual(code, 503)
            self.assertIn("AHREFS_API_KEY", json.loads(body)["error"])

    def test_dr_ok_and_cache_hit(self):
        code, body, h = self.get("/api/dr?target=https://www.Example.com/x")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["domain_rating"]["domain_rating"], 42.0)
        self.assertEqual(h["X-Cache"], "MISS")
        code, body, h = self.get("/api/dr?target=example.com")
        self.assertEqual(code, 200)
        self.assertEqual(h["X-Cache"], "HIT")
        self.assertEqual(self.upstream_calls, ["example.com"])  # апстрим дёрнули один раз

    def test_dr_idna_target_sent_as_punycode(self):
        code, _, _ = self.get("/api/dr?target=" + urllib.parse.quote("доменомер.рф"))
        self.assertEqual(code, 200)
        self.assertEqual(self.upstream_calls, ["xn--d1aca0abfedu.xn--p1ai"])

    def test_dr_bad_target(self):
        for q in ["", "?target=", "?target=nodot", "?target=a..b.com"]:
            code, body, _ = self.get("/api/dr" + q)
            self.assertEqual(code, 400, q)
            self.assertIn("error", json.loads(body))
        self.assertEqual(self.upstream_calls, [])

    def test_upstream_401_passthrough_text(self):
        self.upstream_response = lambda t: (401, b'["Error","Unauthorized"]', _headers())
        code, body, _ = self.get("/api/dr?target=example.com")
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(body)["error"], "Error: Unauthorized")
        self.assertIsNone(server.cache.get("example.com"))  # ошибки не кэшируются

    def test_upstream_5xx_becomes_502(self):
        self.upstream_response = lambda t: (500, b"boom", _headers())
        code, body, _ = self.get("/api/dr?target=example.com")
        self.assertEqual(code, 502)

    def test_upstream_bad_body_becomes_502(self):
        self.upstream_response = lambda t: (200, b'{"unexpected": 1}', _headers())
        code, _, _ = self.get("/api/dr?target=example.com")
        self.assertEqual(code, 502)
        self.assertIsNone(server.cache.get("example.com"))

    def test_upstream_429_pauses_limiter_and_forwards_retry_after(self):
        self.upstream_response = lambda t: (429, b'["Error","Rate limit exceeded"]', _headers(Retry_After="3"))
        code, body, h = self.get("/api/dr?target=example.com")
        self.assertEqual(code, 429)
        self.assertEqual(h["Retry-After"], "3")
        self.assertIn("Rate limit exceeded", json.loads(body)["error"])
        self.assertGreater(server.limiter.stats()["paused_for"], 2)

    def test_queue_full_gives_429_with_retry_after(self):
        # Окно из 1 запроса в 10 с: второй не дождётся за QUEUE_MAX_WAIT=2 → 429 сразу
        server.limiter = server.UpstreamLimiter(limit=1, period=10)
        code, _, _ = self.get("/api/dr?target=one.com")
        self.assertEqual(code, 200)
        t0 = time.monotonic()
        code, body, h = self.get("/api/dr?target=two.com")
        self.assertEqual(code, 429)
        self.assertLess(time.monotonic() - t0, 1)
        self.assertGreaterEqual(int(h["Retry-After"]), 9)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.upstream_calls, ["one.com"])

    def test_per_ip_concurrency_uses_forwarded_for(self):
        # Медленный апстрим держит слоты занятыми
        gate = threading.Event()

        def slow(t):
            if t.startswith("a"):
                gate.wait(3)
            return (200, json.dumps({"domain_rating": {"domain_rating": 1}}).encode(), _headers())
        self.upstream_response = slow

        results = {}

        def call(name, ip):
            results[name] = self.get(f"/api/dr?target={name}.com", {"X-Forwarded-For": f"{ip}, 10.0.0.1"})[0]

        threads = [threading.Thread(target=call, args=(f"a{i}", "1.1.1.1")) for i in range(2)]
        for t in threads:
            t.start()
        time.sleep(0.3)  # два запроса с 1.1.1.1 висят на апстриме
        self.assertEqual(self.get("/api/dr?target=a9.com", {"X-Forwarded-For": "1.1.1.1"})[0], 429)
        # другой IP — не ограничен
        code = self.get("/api/dr?target=b1.com", {"X-Forwarded-For": "2.2.2.2"})[0]
        self.assertEqual(code, 200)
        gate.set()
        for t in threads:
            t.join()
        self.assertEqual(set(results.values()), {200})


if __name__ == "__main__":
    unittest.main()
