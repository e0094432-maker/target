import json
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
        # gemini-2.5-flash is tried only while Google still lists it, and then last
        app._ai.update(model=None, no_search_until=0, listed_at=app.time.time(), listed=["gemini-2.5-flash", "gemini-9-flash-b", "gemini-9-flash-c"])
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
        self.assertEqual([m for m, s in calls[:3]], ["gemini-9-flash-b", "gemini-9-flash-c", "gemini-2.5-flash"])
        # an answer without search says so
        self.assertEqual((first.engine, first.search, second.search), ("gemini", False, False))
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
            bad = post(p, "/api/marketer-publish", {"key": "secret", "text": "", "images": []})[0]
            # midday: a short text post, no pictures, nothing foreign gets through as a picture
            app.ask = lambda prompt, system=None: ('{"text": "Мало кто замечает, но клиент уже спросил нейросеть.", "tags": ["малыйбизнес"]}', [], "test-model")
            _, noon, _ = post(p, "/api/marketer-plan", {"key": "secret", "slot": 1})
            before = len(calls)
            _, pub_t, _ = post(p, "/api/marketer-publish", {"key": "secret", "text": noon["text"], "images": ["https://evil.example/x.png"]})
            text_calls = [c[2] for c in calls[before:]]
        finally:
            app._threads, app.ask, app.time.sleep = real_t, real_a, real_s
            del os.environ["KORIN_ADMIN_KEY"], os.environ["THREADS_TOKEN"]
        self.assertEqual((code, len(pl["slides"]), len(pl["slides"][1]["points"])), (200, 2, 3))
        self.assertEqual(pl["brand"], "korin")
        # the second brand has its own account: without its token nothing can be planned or posted
        os.environ.update(KORIN_ADMIN_KEY="secret", THREADS_TOKEN="tok")
        code_k, no, _ = post(p, "/api/marketer-plan", {"key": "secret", "brand": "kenes"})
        self.assertEqual(code_k, 400)
        self.assertIn("Kenes AI", no["error"])
        del os.environ["KORIN_ADMIN_KEY"], os.environ["THREADS_TOKEN"]
        self.assertEqual((code2, pub["link"], bad), (200, "https://threads.net/p/9", 400))
        self.assertEqual([c[2] for c in calls[:4]], ["IMAGE", "IMAGE", "IMAGE", "CAROUSEL"])
        self.assertEqual(calls[3][3], "1,2,3")
        self.assertEqual((noon["format"], noon["slides"], text_calls[0]), ("text", [], "TEXT"))
        self.assertIn("#малыйбизнес", noon["text"])

    def test_marketer_auto_mode_posts_each_slot_once_a_day(self):
        # scheduled runs come hours late, twice or at night: the account's own posts today (Almaty, UTC+5) decide what is due
        import datetime
        at = lambda s: datetime.datetime.fromisoformat(s).timestamp()
        almaty = lambda s: at(s + "+05:00")
        utc = lambda s: datetime.datetime.fromtimestamp(almaty(s), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
        os.environ.update(KORIN_ADMIN_KEY="secret", THREADS_TOKEN="tok")
        state, reads, writes, asked = {"posts": [], "fail": False}, [], [], []

        def fake(method, path, params):
            if method == "GET" and path == "/me/threads":
                reads.append((params["fields"], params["limit"], params["access_token"]))
                if state["fail"]:
                    raise app.BadRequest("Threads не ответил: timed out")
                return {"data": [{"id": str(i), **(t if isinstance(t, dict) else {"timestamp": t, "media_type": "TEXT_POST"})} for i, t in enumerate(state["posts"])]}
            writes.append(path)
            return {"id": "1"}

        def model(prompt, system=None):
            asked.append(prompt)
            return ('{"text": "Клиент уже спросил нейросеть.", "tags": ["малыйбизнес"], "slides": [{"tag": "вопрос", "title": "Вас советует нейросеть?"}, {"tag": "шаги", "title": "Что проверить", "points": ["Цены"]}]}', [], "test-model")

        def plan(now, posts=(), **body):
            state["posts"], when = list(posts), now
            app.time.time = lambda: when
            return app.marketer_plan({"key": "secret", **body})

        real_t, real_a, real_clock = app._threads, app.ask, app.time.time
        app._threads, app.ask = fake, model
        try:
            # night in Almaty: a skip with HTTP 200, nothing is read, asked or written
            app.time.time = lambda: almaty("2026-11-23T02:47")
            app._hits.clear()
            code, night, _ = post(self.api.server_port, "/api/marketer-plan", {"key": "secret", "auto": True})
            late = plan(almaty("2026-11-23T22:00"), auto=True)
            self.assertEqual((code, night["ok"], reads, asked), (200, False, [], []))
            self.assertIn("ночью", night["skip"])
            self.assertIn("ночью", late["skip"])
            # a post with pictures takes a while, so none is started just before 22:00
            self.assertIn("21:45", plan(almaty("2026-11-23T21:50"), auto=True)["skip"])
            self.assertNotIn("alarm", night)
            self.assertIn("10:30", plan(almaty("2026-11-23T10:00"), auto=True)["skip"])
            # both of today's slots are out already
            full = plan(almaty("2026-11-23T15:00"), [utc("2026-11-23T14:05"), utc("2026-11-23T10:35")], auto=True)
            self.assertIn("уже вышло 2 из 2", full["skip"])
            self.assertNotIn("alarm", full)
            # the morning post is out, now it is midday: a text post for slot 1
            noon = plan(almaty("2026-11-23T14:20"), [utc("2026-11-23T10:31"), utc("2026-11-22T19:05")], auto=True)
            self.assertEqual((noon["ok"], noon["slot"], noon["posted_today"], noon["format"]), (True, 1, 1, "text"))
            # nothing yet today: the morning carousel, on another topic than midday
            morning = plan(almaty("2026-11-23T10:40"), [utc("2026-11-22T19:05")], auto=True)
            self.assertEqual((morning["slot"], morning["posted_today"], morning["format"]), (0, 0, "carousel"))
            self.assertNotEqual(morning["topic"], noon["topic"])
            # the last post went out an hour ago: wait even though a slot is due
            soon = plan(almaty("2026-11-23T19:30"), [utc("2026-11-23T18:30"), utc("2026-11-23T10:31")], auto=True)
            self.assertIn("60 мин", soon["skip"])
            # yesterday's real late runs: 01:49 Almaty is already today but outside the posting window, so it takes no slot
            # and today's carousel still goes out; every Threads time format is read
            real = plan(almaty("2026-11-23T15:00"), ["2026-11-22T20:49:36+0000", "2026-11-22T17:06:00Z", "2026-11-22T13:34:00+00:00"], auto=True)
            self.assertEqual((real["slot"], real["posted_today"], real["format"]), (0, 0, "carousel"))
            # our repost of someone else's thread is not our post: neither a slot nor a reason to wait
            repost = plan(almaty("2026-11-23T10:37"), [{"timestamp": utc("2026-11-23T10:20"), "media_type": "REPOST_FACADE"}], auto=True)
            self.assertEqual((repost["slot"], repost["posted_today"]), (0, 0))
            asked_before = len(asked)
            # a failed or unreadable read never turns into a blind post
            # and it is an alarm, so the run fails and someone sees it (an expired token looks like this)
            state["fail"] = True
            broken = plan(almaty("2026-11-23T14:20"), auto=True)
            self.assertEqual((broken["ok"], broken.get("alarm")), (False, True))
            self.assertIn("вслепую", broken["skip"])
            state["fail"] = False
            unreadable = plan(almaty("2026-11-23T14:20"), ["вчера"], auto=True)
            self.assertEqual(unreadable.get("alarm"), True)
            self.assertIn("вслепую", unreadable["skip"])
            self.assertEqual(len(asked), asked_before)
            # a person with an explicit slot is not stopped, even at night, and the account is not read
            reads_before = len(reads)
            forced = plan(almaty("2026-11-23T02:47"), slot=1)
            self.assertEqual((forced["ok"], forced["slot"], forced["format"], len(reads)), (True, 1, "text", reads_before))
            self.assertNotIn("posted_today", forced)
            # topics follow the Almaty day: 20:30 UTC is already 01:30 of the next day there
            evening_utc = plan(at("2026-11-22T20:30+00:00"), slot=0)["topic"]
            self.assertEqual(evening_utc, plan(almaty("2026-11-23T12:00"), slot=0)["topic"])
            self.assertNotEqual(evening_utc, plan(almaty("2026-11-22T12:00"), slot=0)["topic"])
        finally:
            app._threads, app.ask, app.time.time = real_t, real_a, real_clock
            del os.environ["KORIN_ADMIN_KEY"], os.environ["THREADS_TOKEN"]
        self.assertEqual(writes, [])
        self.assertEqual(set(reads), {("id,timestamp,media_type", "10", "tok")})

    def test_github_token_is_checked_with_a_real_rsa_signature(self):
        import subprocess, tempfile, base64 as b64
        enc = lambda raw: b64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["openssl", "genrsa", "-out", f"{d}/k.pem", "2048"], check=True, capture_output=True)
            mod = subprocess.run(["openssl", "rsa", "-in", f"{d}/k.pem", "-noout", "-modulus"], check=True, capture_output=True, text=True).stdout.strip().split("=")[1]
            keys = {"k1": {"kid": "k1", "kty": "RSA", "n": enc(bytes.fromhex(mod)), "e": enc(b"\x01\x00\x01")}}

            def token(claims):
                signed = enc(json.dumps({"alg": "RS256", "kid": "k1"}).encode()) + "." + enc(json.dumps(claims).encode())
                open(f"{d}/m", "w").write(signed)
                sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", f"{d}/k.pem", f"{d}/m"], check=True, capture_output=True).stdout
                return signed + "." + enc(sig)

            good = {"iss": app.GITHUB_ISSUER, "aud": "korin-api", "repository": "e0094432-maker/target", "exp": app.time.time() + 300}
            t = token(good)
            self.assertEqual(app.verify_rs256(t, keys)["repository"], "e0094432-maker/target")
            head, body, sig = t.split(".")
            forged = head + "." + enc(json.dumps({**good, "repository": "evil/repo"}).encode()) + "." + sig
            self.assertIsNone(app.verify_rs256(forged, keys))
            self.assertIsNone(app.verify_rs256("a.b.c", keys))
            app._jwks.update(at=app.time.time(), keys=keys)
            try:
                self.assertTrue(app.from_our_workflow(t))
                self.assertFalse(app.from_our_workflow(token({**good, "repository": "evil/repo"})))
                self.assertFalse(app.from_our_workflow(token({**good, "exp": app.time.time() - 5})))
                self.assertFalse(app.from_our_workflow(token({**good, "aud": "other"})))
            finally:
                app._jwks.update(at=0, keys={})

    def test_marketer_answers_new_comments_once_and_skips_the_rest(self):
        p = self.api.server_port
        os.environ.update(KORIN_ADMIN_KEY="secret", THREADS_TOKEN="tok")
        sent = []
        talk = [
            {"id": "c1", "text": "Проблема не в нейросетях, а в запросах.", "username": "artur", "replied_to": {"id": "p1"}},
            {"id": "c2", "text": "Уже отвечено раньше", "username": "dana", "replied_to": {"id": "p1"}},
            {"id": "r2", "text": "Спасибо!", "username": "korin.kz", "replied_to": {"id": "c2"}},
            {"id": "c3", "text": "ты дурак", "username": "troll", "replied_to": {"id": "p1"}},
            {"id": "c4", "text": "разговор двух чужих людей", "username": "x", "replied_to": {"id": "c3"}},
        ]

        def fake(method, path, params):
            if path == "/me":
                return {"username": "korin.kz"}
            if path == "/me/threads" and method == "GET":
                return {"data": [{"id": "p1", "text": "Наш пост"}]}
            if path.endswith("/conversation"):
                return {"data": talk}
            if path == "/me/threads":
                sent.append((params.get("reply_to_id"), params.get("text")))
            return {"id": "n1"}

        def model(prompt, system=None):
            return ('{"answer": false, "reply": ""}' if "ты дурак" in prompt else '{"answer": true, "reply": "Согласны. Но клиент спросит как умеет."}', [], "test-model")

        real_t, app._threads = app._threads, fake
        real_a, app.ask = app.ask, model
        real_s, app.time.sleep = app.time.sleep, lambda s: None
        try:
            code, data, _ = post(p, "/api/marketer-replies", {"key": "secret"})
        finally:
            app._threads, app.ask, app.time.sleep = real_t, real_a, real_s
            del os.environ["KORIN_ADMIN_KEY"], os.environ["THREADS_TOKEN"]
        self.assertEqual((code, data["skipped"]), (200, 1))
        self.assertEqual(sent, [("c1", "Согласны. Но клиент спросит как умеет.")])

    def test_rate_limit(self):
        app._hits.clear()
        self.assertTrue(all(app.allowed("1.2.3.4", limit=3) for _ in range(3)))
        self.assertFalse(app.allowed("1.2.3.4", limit=3))
        self.assertTrue(app.allowed("5.6.7.8", limit=3))


if __name__ == "__main__":
    unittest.main()
