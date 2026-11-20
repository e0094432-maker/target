import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from server import app

PAGE = """<html><head><title>Hello Kids</title><meta name="description" content="Курсы"></head>
<body><script>var hidden = 'цена';</script><h1>Курсы английского в Алматы</h1>
<p>{filler}</p><p>Стоимость от 30 000 ₸. Телефон +7 707 123 45 67, пр. Абая, 60.</p></body></html>""".format(filler="Учим детей. " * 60)


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body, code = b"", 404
        if self.path == "/robots.txt":
            body, code = b"User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n", 200
        elif self.path == "/":
            body, code = PAGE.encode(), 200
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def post(port, path, payload, origin=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


class SafeUrlTest(unittest.TestCase):
    def test_refuses_private_and_odd_addresses(self):
        for bad in ["", "ftp://example.kz", "http://127.0.0.1", "http://localhost", "http://10.0.0.5", "http://169.254.169.254/latest", "https://user:pw@example.kz", "https://example.kz:8443"]:
            with self.assertRaises(app.BadRequest, msg=bad):
                app.safe_url(bad)

    def test_adds_scheme(self):
        os.environ["KORIN_SKIP_DNS_CHECK"] = "1"
        try:
            self.assertEqual(app.safe_url("example.kz/page?x=1"), "https://example.kz")
        finally:
            del os.environ["KORIN_SKIP_DNS_CHECK"]


class PageTest(unittest.TestCase):
    def test_checks(self):
        got = {i: ok for i, _, ok, _ in app.analyse_page(200, PAGE, 0.4)}
        self.assertEqual(got, {"open": True, "text": True, "title": True, "price": True, "contact": True, "schema": False, "speed": True})

    def test_script_text_is_not_visible(self):
        self.assertNotIn("hidden", app.visible_text(PAGE))
        empty = {i: ok for i, _, ok, _ in app.analyse_page(200, "<html><script>цена 5 ₸</script></html>", 5)}
        self.assertEqual((empty["text"], empty["price"], empty["speed"]), (False, False, False))


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site, cls.api = serve(Site), serve(app.Handler)
        cls._safe, cls._key = app.safe_url, os.environ.pop("OPENAI_API_KEY", None)
        # the fixture site lives on localhost, which the real check refuses
        app.safe_url = lambda raw: raw.rstrip("/")
        cls._opener = app._opener
        app._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        app.safe_url, app._opener = cls._safe, cls._opener
        if cls._key:
            os.environ["OPENAI_API_KEY"] = cls._key
        cls.site.shutdown()
        cls.api.shutdown()

    def test_audit_end_to_end(self):
        code, data, headers = post(self.api.server_port, "/api/site-audit", {"url": f"http://127.0.0.1:{self.site.server_port}"}, origin="https://kenesai.asia")
        self.assertEqual(code, 200)
        bots = {b["bot"]: b["allowed"] for b in data["bots"]}
        self.assertEqual((bots["GPTBot"], bots["OAI-SearchBot"], bots["ClaudeBot"]), (False, True, True))
        checks = {c["id"]: c["ok"] for c in data["checks"]}
        self.assertTrue(checks["robots"] and checks["open"] and checks["price"])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://kenesai.asia")

    def test_foreign_origin_gets_no_cors_header(self):
        _, _, headers = post(self.api.server_port, "/api/site-audit", {"url": f"http://127.0.0.1:{self.site.server_port}"}, origin="https://evil.example")
        self.assertIsNone(headers["Access-Control-Allow-Origin"])

    def test_errors(self):
        p = self.api.server_port
        self.assertEqual(post(p, "/api/nope", {})[0], 404)
        self.assertEqual(post(p, "/api/agent", {"task": "hack"})[0], 400)
        self.assertEqual(post(p, "/api/check", {"company": "A"})[0], 400)
        code, data, _ = post(p, "/api/agent", {"task": "faq", "context": {"company": "A"}})
        self.assertEqual(code, 503)
        self.assertIn("ИИ не подключён", data["error"])

    def test_connector_is_checked_by_reading_the_page(self):
        site = f"http://127.0.0.1:{self.site.server_port}/"
        code, data, _ = post(self.api.server_port, "/api/connect", {"url": site})
        self.assertEqual((code, data["ok"], data["status"]), (200, True, 200))
        self.assertGreater(data["chars"], 100)
        code, data, _ = post(self.api.server_port, "/api/connect", {"url": site + "missing"})
        self.assertEqual((code, data["ok"]), (200, False))

    def test_agent_reads_connected_pages_before_writing(self):
        seen = {}

        def fake(prompt, system=None):
            seen["prompt"] = prompt
            return "готово", [], "test-model"

        real, app.ask = app.ask, fake
        try:
            site = f"http://127.0.0.1:{self.site.server_port}/"
            code, data, _ = post(self.api.server_port, "/api/agent", {"task": "chat", "question": "что улучшить?", "context": {"company": "A", "links": {"Сайт": site, "2ГИС": site + "missing"}}})
        finally:
            app.ask = real
        self.assertEqual((code, data["text"]), (200, "готово"))
        self.assertEqual([s["ok"] for s in data["steps"]], [True, False, True])
        self.assertIn("Прочитанные страницы", seen["prompt"])
        self.assertIn("что улучшить?", seen["prompt"])

    def test_rate_limit(self):
        app._hits.clear()
        self.assertTrue(all(app.allowed("1.2.3.4", limit=3) for _ in range(3)))
        self.assertFalse(app.allowed("1.2.3.4", limit=3))
        self.assertTrue(app.allowed("5.6.7.8", limit=3))


if __name__ == "__main__":
    unittest.main()
