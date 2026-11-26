"""Client connectors: Telegram channel and WordPress site. urllib is stubbed — nothing real is sent."""
import base64
import contextlib
import io
import json
import socket
import unittest
import urllib.error

from server import app
from tests.test_server import post, serve

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawA"
WP_PASS = "abcd EFGH ijkl MNOP qrst uvwx"
WP_PASS_RAW = WP_PASS.replace(" ", "")


class Resp(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(url, code, payload, headers=None):
    return urllib.error.HTTPError(url, code, "err", headers or {}, io.BytesIO(json.dumps(payload).encode()))


class FakeOpener:
    """Records every request; `answer(req)` returns a payload dict or raises."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def open(self, req, timeout=None):
        self.calls.append(req)
        got = self.answer(req)
        return got if isinstance(got, Resp) else Resp(got)


def tg_answer(member_status="administrator", can_post=True, bad_token=False):
    def answer(req):
        method = req.full_url.rsplit("/", 1)[1]
        params = dict(x.split("=", 1) for x in req.data.decode().split("&") if "=" in x)
        if bad_token:
            raise http_error(req.full_url, 401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        if method == "getMe":
            return {"ok": True, "result": {"id": 42, "is_bot": True, "username": "akku_bot"}}
        if method == "getChat":
            return {"ok": True, "result": {"id": -1001234567890, "title": "Аққу English", "type": "channel", "username": "akku_channel"}}
        if method == "getChatMember":
            return {"ok": True, "result": {"status": member_status, "can_post_messages": can_post}}
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 77, "chat": {"id": -1001234567890, "title": "Аққу English", "username": "akku_channel"}, "text": params.get("text")}}
        return {"ok": False, "description": "unknown"}
    return answer


class ConnectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = serve(app.Handler)

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()

    def setUp(self):
        self._opener, self._resolve = app._conn_opener, app._resolve
        app._resolve = lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        app._hits.clear()

    def tearDown(self):
        app._conn_opener, app._resolve = self._opener, self._resolve
        app._hits.clear()

    def call(self, path, payload):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, data, headers = post(self.api.server_port, path, payload, origin="https://kenesai.asia")
        return code, data, headers, out.getvalue()

    def assertNoSecret(self, *texts):
        for t in texts:
            t = t if isinstance(t, str) else json.dumps(t, ensure_ascii=False)
            for s in (TOKEN, TOKEN.split(":")[1], WP_PASS, WP_PASS_RAW, base64.b64encode(f"admin:{WP_PASS_RAW}".encode()).decode()):
                self.assertNotIn(s, t)

    # ---------- Telegram ----------
    def test_telegram_test_checks_token_channel_and_admin_rights(self):
        app._conn_opener = fake = FakeOpener(tg_answer())
        code, data, headers, log = self.call("/api/connect/telegram/test", {"token": TOKEN, "chat": "https://t.me/akku_channel"})
        self.assertEqual(code, 200, data)
        self.assertEqual((data["title"], data["chat"], data["bot"], data["can_post"]), ("Аққу English", "@akku_channel", "@akku_bot", True))
        self.assertEqual([r.full_url.rsplit("/", 1)[1] for r in fake.calls], ["getMe", "getChat", "getChatMember"])
        self.assertTrue(all(r.full_url.startswith("https://api.telegram.org/bot") for r in fake.calls))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://kenesai.asia")
        self.assertNoSecret(data, log)

    def test_telegram_publish_sends_the_approved_text(self):
        app._conn_opener = fake = FakeOpener(tg_answer())
        code, data, _, log = self.call("/api/publish/telegram", {"token": TOKEN, "chat": "@akku_channel", "text": "Новый набор в группы."})
        self.assertEqual(code, 200, data)
        self.assertEqual((data["message_id"], data["link"]), (77, "https://t.me/akku_channel/77"))
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("text=%D0%9D%D0%BE%D0%B2%D1%8B%D0%B9", fake.calls[0].data.decode())
        self.assertNoSecret(data, log)

    def test_telegram_bad_token_is_refused_without_echoing_it(self):
        app._conn_opener = FakeOpener(tg_answer(bad_token=True))
        code, data, _, log = self.call("/api/connect/telegram/test", {"token": TOKEN, "chat": "@akku_channel"})
        self.assertEqual(code, 400)
        self.assertIn("не принял токен", data["error"])
        self.assertNoSecret(data, log)
        # malformed tokens never reach Telegram (no path tricks like ../)
        app._conn_opener = fake = FakeOpener(tg_answer())
        for bad in ("", "abc", "123:../../getUpdates", TOKEN + "/x", "1" * 200):
            code, data, _, _ = self.call("/api/connect/telegram/test", {"token": bad, "chat": "@akku_channel"})
            self.assertEqual(code, 400, bad)
        for bad in ("", "@a", "@bad name", "https://evil.example/x", "@" + "a" * 40):
            self.assertEqual(self.call("/api/connect/telegram/test", {"token": TOKEN, "chat": bad})[0], 400, bad)
        self.assertEqual(fake.calls, [])

    def test_telegram_bot_without_post_rights_is_not_connected(self):
        app._conn_opener = FakeOpener(tg_answer(member_status="member"))
        code, data, _, _ = self.call("/api/connect/telegram/test", {"token": TOKEN, "chat": "@akku_channel"})
        self.assertEqual(code, 400)
        self.assertIn("Публикация сообщений", data["error"])
        app._conn_opener = FakeOpener(tg_answer(can_post=False))
        self.assertEqual(self.call("/api/connect/telegram/test", {"token": TOKEN, "chat": "@akku_channel"})[0], 400)

    def test_telegram_text_limits(self):
        app._conn_opener = fake = FakeOpener(tg_answer())
        self.assertEqual(self.call("/api/publish/telegram", {"token": TOKEN, "chat": "@akku_channel", "text": "  "})[0], 400)
        self.assertEqual(self.call("/api/publish/telegram", {"token": TOKEN, "chat": "@akku_channel", "text": "я" * 4097})[0], 400)
        self.assertEqual(fake.calls, [])

    def test_unexpected_failure_is_logged_masked(self):
        def boom(req):
            raise RuntimeError(f"socket broke on {req.full_url}")
        app._conn_opener = FakeOpener(boom)
        code, data, _, log = self.call("/api/publish/telegram", {"token": TOKEN, "chat": "@akku_channel", "text": "Привет"})
        self.assertEqual(code, 500)
        self.assertIn("unexpected error", log)
        self.assertIn("***", log)
        self.assertNoSecret(data, log)

    def test_connectors_use_the_cheap_bucket(self):
        self.assertEqual({app.bucket_of(p) for p in ("/api/connect/telegram/test", "/api/publish/telegram", "/api/connect/wordpress/test", "/api/publish/wordpress")}, {"cheap"})

    # ---------- WordPress ----------
    def wp_answer(self, status=200, payload=None, first_404=False):
        def answer(req):
            if first_404 and "/wp-json/" in req.full_url:
                raise http_error(req.full_url, 404, {"code": "rest_no_route"})
            if status != 200:
                raise http_error(req.full_url, status, payload or {})
            if req.get_method() == "GET":
                return {"id": 1, "name": "Айгерим", "capabilities": {"publish_pages": True}}
            body = json.loads(req.data)
            return {"id": 15, "status": body["status"], "link": "https://akku.kz/?page_id=15", "title": {"raw": body["title"]}}
        return answer

    def test_wordpress_test_reads_the_user_with_basic_auth(self):
        app._conn_opener = fake = FakeOpener(self.wp_answer())
        code, data, _, log = self.call("/api/connect/wordpress/test", {"site": "akku.kz/wp-admin/", "user": "admin", "app_password": WP_PASS})
        self.assertEqual(code, 200, data)
        self.assertEqual((data["site"], data["name"], data["can_publish"]), ("https://akku.kz", "Айгерим", True))
        req = fake.calls[0]
        self.assertEqual(req.full_url, "https://akku.kz/wp-json/wp/v2/users/me?context=edit")
        self.assertEqual(req.get_header("Authorization"), "Basic " + base64.b64encode(f"admin:{WP_PASS_RAW}".encode()).decode())
        self.assertNoSecret(data, log)

    def test_wordpress_falls_back_to_rest_route(self):
        app._conn_opener = fake = FakeOpener(self.wp_answer(first_404=True))
        code, data, _, _ = self.call("/api/connect/wordpress/test", {"site": "https://akku.kz", "user": "admin", "app_password": WP_PASS})
        self.assertEqual(code, 200, data)
        self.assertEqual(fake.calls[1].full_url, "https://akku.kz/?rest_route=/wp/v2/users/me&context=edit")

    def test_wordpress_publish_creates_a_draft_page_by_default(self):
        app._conn_opener = fake = FakeOpener(self.wp_answer())
        text = "Вопросы и ответы\n\nСколько человек в группе?\nДо 8 учеников.\n\n<script>alert(1)</script>"
        code, data, _, log = self.call("/api/publish/wordpress", {"site": "https://akku.kz", "user": "admin", "app_password": WP_PASS, "title": "Вопросы и ответы", "content": text})
        self.assertEqual(code, 200, data)
        self.assertEqual((data["id"], data["status"], data["edit_link"]), (15, "draft", "https://akku.kz/wp-admin/post.php?post=15&action=edit"))
        req = fake.calls[0]
        sent = json.loads(req.data)
        self.assertEqual((req.get_method(), req.full_url, sent["status"]), ("POST", "https://akku.kz/wp-json/wp/v2/pages", "draft"))
        self.assertIn("<h3>Сколько человек в группе?</h3>", sent["content"])
        self.assertNotIn("<script>", sent["content"])
        self.assertNoSecret(data, log)
        self.assertEqual(self.call("/api/publish/wordpress", {"site": "https://akku.kz", "user": "admin", "app_password": WP_PASS, "title": "T", "content": "abc", "status": "private"})[0], 400)

    def test_wordpress_wrong_password(self):
        app._conn_opener = FakeOpener(self.wp_answer(401, {"code": "incorrect_password", "message": f"bad password {WP_PASS_RAW}"}))
        code, data, _, log = self.call("/api/connect/wordpress/test", {"site": "https://akku.kz", "user": "admin", "app_password": WP_PASS})
        self.assertEqual(code, 400)
        self.assertIn("не принял логин", data["error"])
        self.assertNoSecret(data, log)
        for bad in ("short", "x" * 25, "abcd efgh ijkl mnop qrst uvw!"):
            self.assertEqual(self.call("/api/connect/wordpress/test", {"site": "https://akku.kz", "user": "admin", "app_password": bad})[0], 400)

    def test_wordpress_redirect_is_not_followed(self):
        app._conn_opener = FakeOpener(self.wp_answer(301, {}))
        app._conn_opener.answer = lambda req: (_ for _ in ()).throw(http_error(req.full_url, 301, {}, {"Location": "https://www.akku.kz/wp-json/wp/v2/users/me"}))
        code, data, _, _ = self.call("/api/connect/wordpress/test", {"site": "https://akku.kz", "user": "admin", "app_password": WP_PASS})
        self.assertEqual(code, 400)
        self.assertIn("https://www.akku.kz", data["error"])
        self.assertEqual(len(app._conn_opener.calls), 1)
        # the opener used in production really refuses redirects
        self.assertIsNone(app._NoRedirect().redirect_request(None, None, 301, "", {}, "https://evil.example/"))

    def test_wordpress_private_and_plain_http_addresses_are_blocked(self):
        app._conn_opener = fake = FakeOpener(self.wp_answer())
        for site in ("http://akku.kz", "https://localhost", "https://127.0.0.1", "https://10.0.0.5", "https://169.254.169.254", "https://[::1]", "https://192.168.1.1/wp", "https://akku.kz:8443", "https://user:pw@akku.kz", "ftp://akku.kz", "https://intranet.local"):
            code, data, _, _ = self.call("/api/connect/wordpress/test", {"site": site, "user": "admin", "app_password": WP_PASS})
            self.assertEqual(code, 400, site)
        # a public-looking name that resolves inward (DNS rebinding to the metadata service) is blocked too
        app._resolve = lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]
        code, data, _, _ = self.call("/api/connect/wordpress/test", {"site": "https://akku.kz", "user": "admin", "app_password": WP_PASS})
        self.assertEqual(code, 400)
        self.assertIn("нельзя подключить", data["error"])
        self.assertEqual(fake.calls, [])

    def test_mask(self):
        self.assertEqual(app.mask(f"x {TOKEN} y", TOKEN), "x *** y")
        self.assertNotIn(TOKEN, app.mask(f"url https://api.telegram.org/bot{TOKEN}/getMe"))
        self.assertNotIn(WP_PASS_RAW, app.mask(f"pw {WP_PASS_RAW}", WP_PASS))


if __name__ == "__main__":
    unittest.main()
