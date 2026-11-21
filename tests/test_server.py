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
        self.assertFalse(checks["sitemap"])
        self.assertTrue(bots["bingbot"])
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

    def test_quick_check_returns_answers_word_for_word(self):
        real, app.ask = app.ask, lambda prompt, system=None: ("Советую школу Alfa и Hello Kids.", [], "test-model")
        app._qcache.clear()
        try:
            code, data, _ = post(self.api.server_port, "/api/quick", {"company": "Hello Kids", "niche": "курсы английского", "city": "Алматы"})
        finally:
            app.ask = real
        self.assertEqual((code, data["asked"], data["named"]), (200, 3, 3))
        self.assertIn("Alfa", data["results"][0]["answer"])
        self.assertEqual(post(self.api.server_port, "/api/quick", {"company": "A"})[0], 400)

    def test_quick_check_pays_once_per_question_not_per_company(self):
        calls = []

        def fake(prompt, system=None):
            calls.append(prompt)
            return "Советую Alfa.", [], "test-model"

        real, app.ask = app.ask, fake
        app._qcache.clear()
        try:
            body = {"niche": "мебель на заказ", "city": "Астана", "i": 0}
            _, first, _ = post(self.api.server_port, "/api/quick", {**body, "company": "Alfa"})
            _, second, _ = post(self.api.server_port, "/api/quick", {**body, "company": "Beta"})
        finally:
            app.ask = real
        self.assertEqual(len(calls), 1)
        self.assertEqual((first["result"]["named"], first["result"]["cached"]), (True, False))
        self.assertEqual((second["result"]["named"], second["result"]["cached"]), (False, True))

    def test_competitors_are_parsed_from_a_free_form_answer(self):
        answer = "1. **English Hub** — крупная сеть\n- Lingua Plus (центр)\n• Hello Kids\n3) «Bright School»: отзывы\n\nНЕТ"
        self.assertEqual(app.clean_names(answer, "Hello Kids"), ["English Hub", "Lingua Plus", "Bright School"])
        real, app.ask = app.ask, lambda prompt, system=None: (answer, [], "test-model")
        try:
            code, data, _ = post(self.api.server_port, "/api/suggest", {"company": "Hello Kids", "niche": "курсы", "city": "Алматы"})
        finally:
            app.ask = real
        self.assertEqual((code, len(data["rivals"])), (200, 3))

    def test_check_goes_one_question_at_a_time(self):
        real, app.ask = app.ask, lambda prompt, system=None: ("Советую Beta, у них отзывы на 2gis.kz", ["https://2gis.kz/x"], "test-model")
        app._qcache.clear()
        try:
            code, data, _ = post(self.api.server_port, "/api/check-one", {"company": "Alfa", "rivals": ["Beta"], "city": "Алматы", "question": "кого посоветуешь?"})
        finally:
            app.ask = real
        self.assertEqual((code, data["row"], data["sources"]), (200, [False, True], ["2gis.kz"]))

    def test_model_falls_back_without_search_and_remembers(self):
        calls = []

        def fake(prompt, system=None, search=None, model=None, retries=3, timeout=180):
            calls.append((model, search))
            if search:
                raise app.providers.ProviderError("HTTP 429: quota")
            return "ок", [], model

        real, app.providers.gemini = app.providers.gemini, fake
        os.environ["GEMINI_API_KEY"] = "k"
        app._ai.update(model=None, no_search_until=0)
        try:
            first = app.ask("q")
            second = app.ask("q")
        finally:
            app.providers.gemini = real
            del os.environ["GEMINI_API_KEY"]
            app._ai.update(model=None, no_search_until=0)
        self.assertEqual(first[0], "ок")
        self.assertEqual(calls[-1], (first[2], False))
        self.assertEqual(len(calls), 5)  # three tries with search, one without, then straight to no-search
        self.assertTrue(app.out_of_quota("HTTP 429: quota"))

    def test_name_typed_as_one_word_is_still_found(self):
        cs = [{"id": 0, "name": "vegalegal", "aliases": []}, {"id": 1, "name": "GK and Partners", "aliases": []}]
        self.assertEqual(app.named_ids("Советую Vega Legal и GK and Partners.", cs), {0, 1})
        self.assertEqual(app.named_ids("Советую REVERA.", cs), set())

    def test_breakdown_reads_the_site_and_returns_structured_steps(self):
        seen = {}

        def fake(prompt, system=None):
            seen["p"] = prompt
            return 'Вот разбор:\n```json\n{"verdict": "Мало отзывов", "reasons": [{"rival": "Beta", "why": "отзывы", "quote": "у Beta много отзывов"}], "gaps": [{"title": "Собрать отзывы", "why": "у Beta 100", "how": "написать 5 клиентам", "effort": "день"}]}\n```', [], "test-model"

        real, app.ask = app.ask, fake
        try:
            code, data, _ = post(self.api.server_port, "/api/analyze", {"company": "Alfa", "niche": "курсы", "city": "Алматы", "rivals": ["Beta"], "site": f"http://127.0.0.1:{self.site.server_port}/", "sources": ["2gis.kz"], "answers": [{"q": "кого посоветуешь?", "a": "у Beta много отзывов"}]})
        finally:
            app.ask = real
        self.assertEqual((code, data["analysis"]["gaps"][0]["effort"], data["analysis"]["reasons"][0]["rival"]), (200, "день", "Beta"))
        self.assertIn("САЙТ КЛИЕНТА", seen["p"])
        self.assertTrue(data["steps"][1]["ok"])
        self.assertEqual(post(self.api.server_port, "/api/analyze", {"company": "Alfa"})[0], 400)
        self.assertIsNone(app.parse_json("нет данных"))

    def test_social_plan_and_reply(self):
        plan = '{"posts": [{"kind": "вопрос", "hook": "Вас советует нейросеть?", "threads": "Спросите нейросеть про свою нишу.", "x": "Спросите нейросеть.", "day": "пн"}, {"kind": "совет", "hook": "", "threads": "", "x": ""}]}'
        real, app.ask = app.ask, lambda prompt, system=None: (plan if "posts" in prompt else '{"fit": false, "why": "личная тема", "reply": "не важно"}', [], "test-model")
        try:
            code, data, _ = post(self.api.server_port, "/api/social-plan", {"brand": "Kenes AI", "about": "реклама в нейросетях"})
            code2, rep, _ = post(self.api.server_port, "/api/social-reply", {"brand": "Kenes AI", "about": "реклама", "thread": "Сегодня у меня умер кот, очень грустно."})
        finally:
            app.ask = real
        self.assertEqual((code, len(data["posts"]), data["posts"][0]["day"]), (200, 1, "пн"))
        self.assertEqual((code2, rep["fit"], rep["reply"]), (200, False, ""))
        self.assertEqual(post(self.api.server_port, "/api/social-plan", {"brand": "A"})[0], 400)

    def test_threads_needs_the_owner_code_and_publishes_in_two_steps(self):
        p = self.api.server_port
        os.environ.pop("KORIN_ADMIN_KEY", None)
        self.assertEqual(post(p, "/api/threads-post", {"key": "", "text": "привет"})[0], 400)
        os.environ.update(KORIN_ADMIN_KEY="secret", THREADS_TOKEN="tok")
        calls = []

        def fake(method, path, params):
            calls.append((method, path, params.get("reply_to_id")))
            return {"id": "777", "permalink": "https://threads.net/t/1", "data": [{"id": "5", "text": "ветка", "username": "u", "permalink": "l", "timestamp": "t"}]}

        real, app._threads = app._threads, fake
        try:
            self.assertEqual(post(p, "/api/threads-post", {"key": "wrong", "text": "привет"})[0], 400)
            code, data, _ = post(p, "/api/threads-post", {"key": "secret", "text": "привет", "reply_to": "123"})
            code2, found, _ = post(p, "/api/threads-search", {"key": "secret", "q": "малый бизнес"})
        finally:
            app._threads = real
            del os.environ["KORIN_ADMIN_KEY"], os.environ["THREADS_TOKEN"]
        self.assertEqual((code, data["link"]), (200, "https://threads.net/t/1"))
        self.assertEqual(calls[:2], [("POST", "/me/threads", "123"), ("POST", "/me/threads_publish", None)])
        self.assertEqual((code2, found["found"][0]["id"]), (200, "5"))

    def test_marketer_plans_a_post_and_publishes_a_carousel(self):
        p = self.api.server_port
        os.environ.update(KORIN_ADMIN_KEY="secret", THREADS_TOKEN="tok")
        calls = []

        def fake(method, path, params):
            calls.append((method, path, params.get("media_type"), params.get("children")))
            return {"id": str(len(calls)), "permalink": "https://threads.net/p/9"}

        plan = '{"text": "Спросите нейросеть про свою нишу.", "slides": [{"tag": "вопрос", "title": "Вас советует нейросеть?"}, {"tag": "шаги", "title": "Что проверить", "points": ["Цены на сайте", "Отзывы", "Адрес"]}, {"title": "лишний"}]}'
        real_t, app._threads = app._threads, fake
        real_a, app.ask = app.ask, lambda prompt, system=None: (plan, [], "test-model")
        real_s, app.time.sleep = app.time.sleep, lambda s: None
        try:
            self.assertEqual(post(p, "/api/marketer-plan", {"key": "no"})[0], 400)
            code, pl, _ = post(p, "/api/marketer-plan", {"key": "secret"})
            imgs = [f"https://kenesai.asia/korin/media/2026-11-21-ab12-{i}.png" for i in (1, 2, 3)]
            code2, pub, _ = post(p, "/api/marketer-publish", {"key": "secret", "text": pl["text"], "images": imgs + ["https://evil.example/x.png"]})
            bad = post(p, "/api/marketer-publish", {"key": "secret", "text": "t", "images": ["https://evil.example/x.png"]})[0]
        finally:
            app._threads, app.ask, app.time.sleep = real_t, real_a, real_s
            del os.environ["KORIN_ADMIN_KEY"], os.environ["THREADS_TOKEN"]
        self.assertEqual((code, len(pl["slides"]), len(pl["slides"][1]["points"])), (200, 2, 3))
        self.assertEqual((code2, pub["link"], bad), (200, "https://threads.net/p/9", 400))
        self.assertEqual([c[2] for c in calls[:4]], ["IMAGE", "IMAGE", "IMAGE", "CAROUSEL"])
        self.assertEqual(calls[3][3], "1,2,3")

    def test_rate_limit(self):
        app._hits.clear()
        self.assertTrue(all(app.allowed("1.2.3.4", limit=3) for _ in range(3)))
        self.assertFalse(app.allowed("1.2.3.4", limit=3))
        self.assertTrue(app.allowed("5.6.7.8", limit=3))


if __name__ == "__main__":
    unittest.main()
