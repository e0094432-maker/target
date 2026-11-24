"""Model choice, honest measurement, the writing router and declined comments. No real AI is called: HTTP is stubbed."""
import copy
import json
import os
import pickle
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from aiintel import providers
from server import app

ENV = ("GEMINI_API_KEY", "OPENAI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACKS", "GEMINI_PREFER", "GEMINI_SEARCH",
       "GROQ_API_KEY", "GROQ_MODEL", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_MODEL", "CLOUDFLARE_MAX_TOKENS",
       "KORIN_WRITE_ORDER", "KORIN_WRITE_ORDER_CLIENT", "KORIN_WRITE_BUDGET", "KORIN_WRITE_COOLDOWN", "KORIN_REPLY_MAX_AGE_HOURS",
       "KORIN_ADMIN_KEY", "THREADS_TOKEN", "THREADS_TOKEN_KENES")
CF_ID = "0123456789abcdef0123456789abcdef"
GROQ_OK = {"choices": [{"message": {"role": "assistant", "content": "текст от groq"}}], "model": "openai/gpt-oss-120b"}
CF_OK = {"result": {"response": "текст от cloudflare"}, "success": True, "errors": [], "messages": []}


def gemini_reply(text="Советую Alfa и Beta.", grounded=True, model="gemini-3.5-flash-lite"):
    cand = {"content": {"parts": [{"text": text}]}}
    if grounded:
        cand["groundingMetadata"] = {"webSearchQueries": ["кого посоветуешь"], "groundingChunks": [{"web": {"uri": "https://vertexaisearch.cloud.google.com/x", "title": "2gis.kz"}}]}
    return {"candidates": [cand], "modelVersion": model}


class FakeHTTP:
    """Stands in for providers._post. plan: host -> list of replies (dict) or errors; the last one repeats."""

    def __init__(self, **plan):
        self.plan, self.calls = plan, []

    def __call__(self, url, headers, payload, timeout=180, retries=3):
        host = "groq" if "api.groq.com" in url else "cloudflare" if "api.cloudflare.com" in url else "gemini" if "generativelanguage" in url else "openai" if "api.openai.com" in url else "other"
        self.calls.append({"host": host, "url": url, "headers": headers, "payload": payload, "retries": retries})
        queue = self.plan.get(host) or [providers.ProviderError("HTTP 500: nothing planned")]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def hosts(self):
        return [c["host"] for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in ENV if k in os.environ}
        self.patches = []
        self.reset()
        self.patch(app.time, "sleep", lambda s: None)

    def tearDown(self):
        for obj, name, value in reversed(self.patches):
            setattr(obj, name, value)
        for k in ENV:
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.reset()

    def reset(self):
        app._ai.clear()
        app._ai.update(model=None, no_search_until=0)
        app._writers.update(day="", used={}, cool={})
        app._declined.clear()
        app._qcache.clear()
        app._quick.update(day="", n=0)

    def patch(self, obj, name, value):
        self.patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def listed(self, *names):
        app._ai.update(listed_at=app.time.time(), listed=list(names))

    def http(self, **plan):
        fake = FakeHTTP(**plan)
        self.patch(providers, "_post", fake)
        return fake


# ---------- 1. Gemini model choice ----------
def _m(name, methods=("generateContent", "countTokens")):
    return {"name": "models/" + name, "supportedGenerationMethods": list(methods)}


class ModelChoiceTest(Base):
    def setUp(self):
        super().setUp()
        os.environ["GEMINI_API_KEY"] = "k"
        self.pages = []

    def google_lists(self, pages):
        def fake(page):
            self.pages.append(page)
            if isinstance(pages, Exception):
                raise pages
            return pages[page]
        self.patch(app, "_gemini_list", fake)

    def test_new_models_first_and_old_flash_after_them(self):
        self.google_lists({
            "": {"models": [_m("gemini-2.5-flash"), _m("gemini-2.0-flash"), _m("gemini-2.5-pro"), _m("gemini-2.5-flash-preview-tts"),
                            _m("text-embedding-004", ["embedContent"]), _m("gemma-3-27b-it"), _m("gemini-4-hydrogen")], "nextPageToken": "p2"},
            "p2": {"models": [_m("gemini-3.5-flash-lite"), _m("gemini-3-flash-preview")]},
        })
        order = app.gemini_models()
        self.assertEqual(order, ["gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.0-flash"])
        self.assertEqual(self.pages, ["", "p2"])  # every page is read
        app.gemini_models()
        self.assertEqual(self.pages, ["", "p2"])  # and the list is kept for an hour

    def test_a_dearer_family_is_used_only_when_asked_for(self):
        # gemini-4-hydrogen has no «flash» in its name and costs ~3-5x more: it comes only through GEMINI_PREFER
        self.google_lists({"": {"models": [_m("gemini-2.5-flash"), _m("gemini-4-hydrogen")]}})
        self.assertEqual(app.gemini_models(), ["gemini-2.5-flash"])
        os.environ["GEMINI_PREFER"] = "gemini-3.5-flash-lite,gemini-4-hydrogen"
        self.assertEqual(app.gemini_models(), ["gemini-4-hydrogen", "gemini-2.5-flash"])

    def test_many_new_models_never_push_out_the_old_flash_or_pick_paid_ones(self):
        # the names the research reports for November 2026
        names = ["gemini-4-argon", "gemini-4-carbon", "gemini-4-neon", "gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.7-flash",
                 "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash"]
        self.google_lists({"": {"models": [_m(n) for n in names]}})
        order = app.gemini_models()
        self.assertEqual(len(order), 5)
        self.assertEqual((order[0], order[-1]), ("gemini-3.5-flash-lite", "gemini-2.5-flash"))
        self.assertFalse({"gemini-4-argon", "gemini-4-carbon", "gemini-4-neon", "gemini-4-hydrogen"} & set(order))
        self.assertTrue(all("flash" in m for m in order))
        # without the preferred name the old flash still keeps its place
        app._ai.clear()
        app._ai.update(model=None, no_search_until=0)
        self.google_lists({"": {"models": [_m(n) for n in names if n != "gemini-3.5-flash-lite"]}})
        order = app.gemini_models()
        self.assertEqual(order[-1], "gemini-2.5-flash")
        self.assertFalse({"gemini-4-argon", "gemini-4-carbon", "gemini-4-neon"} & set(order))

    def test_a_pinned_model_does_not_push_out_the_old_flash(self):
        os.environ.update(GEMINI_MODEL="gemini-custom", GEMINI_FALLBACKS="gemini-other")
        self.google_lists(urllib.error.URLError("no route"))
        order = app.gemini_models()
        self.assertEqual((order[:2], order[-1], len(order)), (["gemini-custom", "gemini-other"], "gemini-2.5-flash", 5))

    def test_after_the_shutdown_the_old_model_is_not_tried(self):
        self.google_lists({"": {"models": [_m("gemini-3.5-flash-lite"), _m("gemini-flash-latest"), _m("gemini-3-flash")]}})
        # a moving alias comes after the numbered models
        self.assertEqual(app.gemini_models(), ["gemini-3.5-flash-lite", "gemini-3-flash", "gemini-flash-latest"])

    def test_before_new_models_appear_the_old_one_stays_first(self):
        # an older or cheaper 2.x model never pushes gemini-2.5-flash aside while it works
        self.google_lists({"": {"models": [_m("gemini-2.0-flash"), _m("gemini-2.5-flash-lite"), _m("gemini-2.5-flash")]}})
        self.assertEqual(app.gemini_models()[0], "gemini-2.5-flash")

    def test_env_still_wins(self):
        os.environ.update(GEMINI_MODEL="gemini-custom", GEMINI_FALLBACKS="gemini-2.5-flash")
        self.google_lists({"": {"models": [_m("gemini-3.5-flash-lite"), _m("gemini-2.5-flash")]}})
        self.assertEqual(app.gemini_models()[:3], ["gemini-custom", "gemini-2.5-flash", "gemini-3.5-flash-lite"])

    def test_preference_list_can_be_changed(self):
        os.environ["GEMINI_PREFER"] = "gemini-4-hydrogen,gemini-3.5-flash-lite"
        self.google_lists({"": {"models": [_m("gemini-3.5-flash-lite"), _m("gemini-4-hydrogen")]}})
        self.assertEqual(app.gemini_models(), ["gemini-4-hydrogen", "gemini-3.5-flash-lite"])

    def test_unreadable_list_falls_back_to_the_names_then_the_old_model(self):
        self.google_lists(urllib.error.URLError("no route"))
        self.assertEqual(app.gemini_models(), ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash"])
        self.assertEqual(self.pages, [""])
        app.gemini_models()
        self.assertEqual(self.pages, [""])  # not asked again at once...
        now = app.time.time()
        self.patch(app.time, "time", lambda: now + 301)
        app.gemini_models()
        self.assertEqual(self.pages, ["", ""])  # ...but after five minutes, not an hour

    def test_ask_skips_a_missing_model_and_remembers_the_one_that_answered(self):
        self.google_lists(urllib.error.URLError("no route"))
        calls = []

        def gemini(prompt, system=None, search=None, model=None, retries=3, timeout=180):
            calls.append(model)
            if model != "gemini-flash-lite-latest":
                raise providers.ProviderError("HTTP 404: model not found")
            return providers.Answer("ответ", ["https://2gis.kz"], model, engine="gemini", search=True)

        self.patch(providers, "gemini", gemini)
        first = app.ask("q")
        self.assertEqual((first[2], first.search, calls), ("gemini-flash-lite-latest", True, ["gemini-3.5-flash-lite", "gemini-flash-lite-latest"]))
        app.ask("q")
        self.assertEqual(calls[-1], "gemini-flash-lite-latest")
        self.assertEqual(len(calls), 3)

    def test_a_broken_connection_while_listing_does_not_fail_the_request(self):
        self.google_lists(ConnectionResetError("reset by peer"))
        before = app.time.time()
        self.assertEqual(app.gemini_models()[0], "gemini-3.5-flash-lite")  # the names as they are, no exception
        self.assertLess(app._ai["listed_at"], before - 3000)  # asked again in five minutes, not an hour

    def test_one_busy_moment_does_not_move_us_to_another_model_for_good(self):
        self.listed("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash")
        busy, calls = [True], []

        def gemini(prompt, system=None, search=None, model=None, retries=3, timeout=180):
            calls.append(model)
            if model == "gemini-3.5-flash-lite" and busy:
                busy.pop()
                raise providers.ProviderError("HTTP 429: RESOURCE_EXHAUSTED")
            return providers.Answer("ответ", ["https://2gis.kz"], model, engine="gemini", search=True)

        self.patch(providers, "gemini", gemini)
        self.assertEqual(app.ask("q")[2], "gemini-3.8-flash")
        self.assertEqual(app.ask("q")[2], "gemini-3.8-flash")  # it answered, so it goes first for a while...
        now = app.time.time()
        self.patch(app.time, "time", lambda: now + 3601)
        self.assertEqual(app.ask("q")[2], "gemini-3.5-flash-lite")  # ...an hour, then the cheap one again
        self.assertEqual(calls, ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"])

    def test_a_model_that_did_not_search_does_not_jump_the_queue(self):
        self.listed("gemini-3.5-flash-lite", "gemini-2.5-flash")
        calls = []

        def gemini(prompt, system=None, search=None, model=None, retries=3, timeout=180):
            calls.append((model, search))
            if model == "gemini-3.5-flash-lite" and search:
                raise providers.ProviderError("HTTP 429: grounding quota")
            grounded = search and model == "gemini-2.5-flash"
            return providers.Answer("ответ", [], model, engine="gemini", search=grounded)

        self.patch(providers, "gemini", gemini)
        got = app.ask("q")
        self.assertEqual((got[2], got.search), ("gemini-2.5-flash", True))
        self.assertEqual(app._ai["model"], "gemini-2.5-flash")
        app._ai.update(model=None)
        calls.clear()
        # the model answers with search asked for but did not search: it is not promoted
        self.listed("gemini-3.5-flash-lite", "gemini-3.8-flash")

        def lazy(prompt, system=None, search=None, model=None, retries=3, timeout=180):
            calls.append((model, search))
            return providers.Answer("ответ", [], model, engine="gemini", search=False)

        self.patch(providers, "gemini", lazy)
        self.assertFalse(app.ask("q").search)
        self.assertIsNone(app._ai["model"])
        self.assertEqual(calls, [("gemini-3.5-flash-lite", True), ("gemini-3.8-flash", True)])  # one more try, then stop

    def test_a_new_model_that_skips_search_hands_over_to_the_old_flash(self):
        self.listed("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash")
        calls = []

        def gemini(prompt, system=None, search=None, model=None, retries=3, timeout=180):
            calls.append((model, search))
            return providers.Answer("ответ", [], model, engine="gemini", search=bool(search and model == "gemini-2.5-flash"))

        self.patch(providers, "gemini", gemini)
        got = app.ask("q")
        self.assertEqual((got[2], got.search), ("gemini-2.5-flash", True))
        self.assertEqual(calls, [("gemini-3.5-flash-lite", True), ("gemini-2.5-flash", True)])  # straight to the old flash
        self.assertEqual(app._ai["model"], "gemini-2.5-flash")
        calls.clear()
        app.ask("q")
        self.assertEqual(calls, [("gemini-2.5-flash", True)])  # it searched, so it goes first for a while

    def test_writing_without_search_is_not_treated_as_a_skip(self):
        self.listed("gemini-3.5-flash-lite", "gemini-2.5-flash")
        calls = []

        def gemini(prompt, system=None, search=None, model=None, retries=3, timeout=180):
            calls.append((model, search))
            return providers.Answer("текст", [], model, engine="gemini", search=False)

        self.patch(providers, "gemini", gemini)
        got = app.ask("q", web=False)
        self.assertEqual((got[0], got.search, calls), ("текст", False, [("gemini-3.5-flash-lite", False)]))


# ---------- 2. honest measurement ----------
OLD_CHECK_KEYS = {"row", "answer", "sources", "model", "cached"}
OLD_QUICK_KEYS = {"question", "answer", "named", "sources", "model", "cached"}
CHECK = {"company": "Alfa", "rivals": ["Beta"], "city": "Алматы", "question": "кого посоветуешь?"}


class HonestMeasurementTest(Base):
    def test_answer_can_be_copied_and_pickled(self):
        a = providers.Answer("т", ["https://2gis.kz"], "m", engine="gemini", search=True)
        for b in (copy.copy(a), copy.deepcopy(a), pickle.loads(pickle.dumps(a))):
            self.assertEqual((tuple(b), b.engine, b.search), (("т", ["https://2gis.kz"], "m"), "gemini", True))

    def test_the_cli_default_is_not_the_retiring_model(self):
        os.environ["GEMINI_API_KEY"] = "k"
        fake = self.http(gemini=[gemini_reply()])
        providers.gemini("q")  # how the aiintel CLI calls it: no model
        self.assertNotIn("gemini-2.5-flash", fake.calls[0]["url"])
        os.environ["GEMINI_MODEL"] = "gemini-x"
        providers.gemini("q")
        self.assertIn("/models/gemini-x:", fake.calls[1]["url"])

    def test_grounding_is_read_from_googles_answer(self):
        os.environ["GEMINI_API_KEY"] = "k"
        self.http(gemini=[gemini_reply(grounded=False), gemini_reply(grounded=True)])
        no = providers.gemini("q", search=True, model="gemini-3.5-flash-lite")
        yes = providers.gemini("q", search=True, model="gemini-3.5-flash-lite")
        self.assertEqual((no.search, no[1]), (False, []))
        self.assertEqual((yes.search, yes[1], yes.engine), (True, ["https://2gis.kz"], "gemini"))
        text, urls, model = yes  # still unpacks like before
        self.assertEqual(model, "gemini-3.5-flash-lite")

    def test_answer_without_search_is_marked_and_kept_an_hour_only(self):
        os.environ["GEMINI_API_KEY"] = "k"
        self.listed("gemini-3.5-flash-lite")
        fake = self.http(gemini=[gemini_reply(grounded=False), gemini_reply(grounded=True)])
        first, second = app.check_one(CHECK), app.check_one(CHECK)
        self.assertEqual((first["search"], first["cached"], first["engine"], first["row"]), (False, False, "gemini", [True, True]))
        self.assertEqual((second["search"], second["cached"]), (False, True))  # a retry within the hour is free, still marked
        self.assertEqual(len(fake.calls), 1)
        now = app.time.time()
        self.patch(app.time, "time", lambda: now + 3601)  # not 72 hours: after an hour it is asked again
        third, fourth = app.check_one(CHECK), app.check_one(CHECK)
        self.assertEqual((third["search"], third["cached"]), (True, False))
        self.assertEqual((fourth["search"], fourth["cached"], fourth["sources"]), (True, True, ["2gis.kz"]))
        self.assertEqual(len(fake.calls), 2)
        self.patch(app.time, "time", lambda: now + 3601 + 70 * 3600)  # an answer with search is kept the full 72 hours
        self.assertTrue(app.check_one(CHECK)["cached"])
        self.assertTrue(OLD_CHECK_KEYS <= set(first))

    def test_search_that_fails_falls_back_marked(self):
        os.environ["GEMINI_API_KEY"] = "k"
        self.listed("gemini-3.5-flash-lite")

        def gemini(prompt, system=None, search=None, model=None, retries=3, timeout=180):
            if search:
                raise providers.ProviderError("HTTP 429: grounding quota")
            return "Советую Alfa.", [], model  # an old-style plain tuple

        self.patch(providers, "gemini", gemini)
        got = app.quick_check({"company": "Alfa", "niche": "курсы", "city": "Алматы", "i": 0})["result"]
        self.assertEqual((got["search"], got["cached"], got["engine"], got["named"]), (False, False, "gemini", True))
        self.assertTrue(OLD_QUICK_KEYS <= set(got))
        self.assertLessEqual(next(iter(app._qcache.values()))[0], app.time.time() + app.QUICK_NOSEARCH_TTL)

    def test_gpt4o_without_search_is_labelled_as_such(self):
        os.environ["OPENAI_API_KEY"] = "k"
        self.http(openai=[{"choices": [{"message": {"content": "Советую Beta."}}], "model": "gpt-4o-2024-08-06"}])
        got = app.check_one(CHECK)
        self.assertEqual((got["engine"], got["search"], got["model"], got["cached"]), ("openai", False, "gpt-4o-2024-08-06", False))
        old = app.run_check({"company": "Alfa", "rivals": ["Beta"], "questions": ["кого посоветуешь?"]})
        self.assertEqual((old["engine"], old["search"], old["model"]), ("openai", [False], "gpt-4o-2024-08-06"))

    def test_measurement_never_goes_to_another_model(self):
        os.environ.update(GEMINI_API_KEY="k", GROQ_API_KEY="g", CLOUDFLARE_ACCOUNT_ID=CF_ID, CLOUDFLARE_API_TOKEN="c")
        self.listed("gemini-3.5-flash-lite")
        fake = self.http(gemini=[providers.ProviderError("HTTP 429: RESOURCE_EXHAUSTED")], groq=[GROQ_OK], cloudflare=[CF_OK])
        with self.assertRaises(providers.ProviderError):
            app.check_one(CHECK)
        with self.assertRaises(providers.ProviderError):
            app.quick_check({"company": "Alfa", "niche": "курсы", "city": "Алматы", "i": 1})
        self.assertEqual(app.run_check({"company": "Alfa", "rivals": ["Beta"], "questions": ["q1", "q2"]})["errors"], 2)
        self.assertEqual(set(fake.hosts()), {"gemini"})

    def test_old_stand_ins_still_work_and_get_the_new_fields(self):
        self.patch(app, "ask", lambda prompt, system=None: ("Советую Beta.", [], "test-model"))
        got = app.check_one(CHECK)
        self.assertEqual(set(got), OLD_CHECK_KEYS | {"engine", "search"})
        self.assertEqual((got["row"], got["model"], got["cached"], got["search"]), ([False, True], "test-model", False, None))
        self.assertTrue(app.check_one(CHECK)["cached"])  # unknown search: kept as before


# ---------- 3. the writing router ----------
class RouterTest(Base):
    def keys(self, groq=True, cloudflare=True, gemini=True):
        if groq:
            os.environ["GROQ_API_KEY"] = "g"
        if cloudflare:
            os.environ.update(CLOUDFLARE_ACCOUNT_ID=CF_ID, CLOUDFLARE_API_TOKEN="c")
        if gemini:
            os.environ["GEMINI_API_KEY"] = "k"

    def main(self, reply=("из основного пути", [], "gemini-test")):
        seen = []

        def ask(*args, **kw):
            seen.append((args, kw))
            return reply

        self.patch(app, "ask", ask)
        return seen

    def test_without_new_keys_write_is_ask_itself(self):
        fake = self.http()
        for gemini in (False, True):
            if gemini:
                os.environ["GEMINI_API_KEY"] = "k"
            reply = ("текст", [], "m")
            seen = self.main(reply)
            for kind in ("own", "client", "anything"):
                self.assertIs(app.write("p", "s", kind=kind), reply)
            self.assertEqual(seen, [(("p", "s"), {})] * 3)
            self.assertEqual((app.writer_chain("own"), app.writer_chain("client")), (["main"], ["main"]))
        self.assertEqual(fake.calls, [])
        self.assertEqual(app._writers["used"], {})  # no counting either

    def test_own_texts_go_groq_then_cloudflare_with_a_pause_after_429(self):
        self.keys()
        seen = self.main()
        fake = self.http(groq=[providers.ProviderError("HTTP 429: rate limit reached, try again in 7s"), GROQ_OK], cloudflare=[CF_OK])
        first = app.write("пост", "система", kind="own")
        self.assertEqual((first[0], first[2], first.engine, first.search), ("текст от cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "cloudflare", False))
        second = app.write("пост", "система", kind="own")
        self.assertEqual(second[0], "текст от cloudflare")
        self.assertEqual(fake.hosts(), ["groq", "cloudflare", "cloudflare"])  # groq is resting, not asked again
        self.assertTrue(all(c["retries"] == 0 for c in fake.calls))  # one try each: no retry storm
        now = app.time.time()
        self.patch(app.time, "time", lambda: now + 901)
        self.assertEqual(app.write("пост", "система", kind="own")[0], "текст от groq")
        self.assertEqual(seen, [])  # the main path was not needed
        groq = fake.calls[0]
        self.assertEqual(groq["url"], "https://api.groq.com/openai/v1/chat/completions")
        self.assertTrue(groq["headers"]["Authorization"].startswith("Bearer "))
        self.assertEqual(groq["payload"], {"model": "openai/gpt-oss-120b", "messages": [{"role": "system", "content": "система"}, {"role": "user", "content": "пост"}]})
        cf = fake.calls[1]
        self.assertEqual(cf["url"], f"https://api.cloudflare.com/client/v4/accounts/{CF_ID}/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast")
        self.assertEqual((cf["payload"]["max_tokens"], cf["payload"]["messages"][1]["content"]), (2048, "пост"))

    def test_a_daily_limit_rests_until_the_next_day(self):
        self.keys(cloudflare=False)
        self.main()
        self.http(groq=[providers.ProviderError("HTTP 429: Limit 100000, Used 99990 on tokens per day (TPD)")])
        self.assertEqual(app.write("пост", kind="own")[0], "из основного пути")
        now = app.time.time()
        self.assertGreaterEqual(app._writers["cool"]["groq"], now - now % 86400 + 86400 - 1)

    def test_daily_budget_per_provider(self):
        self.keys()
        self.main()
        os.environ["KORIN_WRITE_BUDGET"] = "groq=2, cloudflare=1"
        fake = self.http(groq=[GROQ_OK], cloudflare=[CF_OK])
        got = [app.write("пост", kind="own")[0] for _ in range(4)]
        self.assertEqual(got, ["текст от groq", "текст от groq", "текст от cloudflare", "из основного пути"])
        self.assertEqual(fake.hosts(), ["groq", "groq", "cloudflare"])
        now = app.time.time()
        self.patch(app.time, "time", lambda: now + 86400)  # a new UTC day: budgets start again
        self.assertEqual(app.write("пост", kind="own")[0], "текст от groq")

    def test_order_is_configurable(self):
        self.keys()
        self.main()
        os.environ["KORIN_WRITE_ORDER"] = "cloudflare, groq"
        fake = self.http(groq=[GROQ_OK], cloudflare=[CF_OK])
        self.assertEqual(app.writer_chain("own"), ["cloudflare", "groq"])
        self.assertEqual(app.write("пост", kind="own")[0], "текст от cloudflare")
        os.environ["KORIN_WRITE_ORDER"] = "gemini,groq"  # "gemini" and "openai" mean the main path
        self.assertEqual(app.writer_chain("own"), ["main", "groq"])
        self.assertEqual(app.write("пост", kind="own")[0], "из основного пути")
        self.assertEqual(fake.hosts(), ["cloudflare"])

    def test_client_data_never_goes_to_groq(self):
        self.keys(cloudflare=False)
        os.environ.update(KORIN_WRITE_ORDER="groq,main", KORIN_WRITE_ORDER_CLIENT="groq,cloudflare,main")
        fake = self.http(groq=[GROQ_OK])
        plan = '{"posts": [{"kind": "совет", "hook": "h", "threads": "Пост клиента.", "x": "x", "day": "пн"}]}'
        seen = []

        def ask(prompt, system=None):
            seen.append(prompt)
            return (plan if "posts" in prompt else '{"fit": true, "why": "по делу", "reply": "Ответ."}' if "ветка" in prompt else '{"verdict": "v", "reasons": [], "gaps": []}', [], "gemini-test")

        self.patch(app, "ask", ask)
        self.patch(app, "read_page", lambda raw: {"url": raw, "status": 404, "title": "", "text": ""})
        self.assertEqual(app.writer_chain("client"), ["main"])
        self.assertEqual(app.writer_chain("unknown"), ["main"])  # anything not "own" is treated as client data
        app.run_agent({"task": "faq", "context": {"company": "Клиент"}})
        app.analyze({"company": "Клиент", "answers": [{"q": "q", "a": "ответ"}]})
        app.social_plan({"brand": "Клиент", "about": "мебель"})
        app.social_reply({"brand": "Клиент", "about": "мебель", "thread": "Где заказать кухню в Алматы, посоветуйте?"})
        self.assertEqual(len(seen), 4)
        self.assertEqual(fake.calls, [])

    def test_with_only_a_groq_key_client_texts_still_do_not_go_there(self):
        self.keys(cloudflare=False, gemini=False)
        fake = self.http(groq=[GROQ_OK])
        with self.assertRaises(providers.ProviderError) as e:
            app.write("данные клиента", kind="client")
        self.assertTrue(app.no_ai(e.exception))  # the page says no AI is connected, as before
        self.assertEqual(fake.calls, [])
        self.assertEqual(app.write("наш пост", kind="own")[0], "текст от groq")

    def test_cloudflare_is_allowed_for_client_data(self):
        self.keys()
        seen = self.main()
        fake = self.http(groq=[GROQ_OK], cloudflare=[CF_OK])
        out = app.run_agent({"task": "faq", "context": {"company": "Клиент"}})
        self.assertEqual((out["text"], out["model"]), ("текст от cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"))
        self.assertEqual((fake.hosts(), seen), (["cloudflare"], []))

    def test_our_posts_use_the_router(self):
        self.keys(cloudflare=False)
        self.main()
        os.environ.update(KORIN_ADMIN_KEY="secret", THREADS_TOKEN="tok")
        reply = {"choices": [{"message": {"content": '{"text": "Клиент уже спросил нейросеть.", "tags": ["малыйбизнес"]}'}}], "model": "llama-3.3-70b-versatile"}
        fake = self.http(groq=[reply])
        self.patch(app, "_threads", lambda method, path, params: {"id": "1"})
        got = app.marketer_plan({"key": "secret", "slot": 1})
        self.assertEqual((got["ok"], got["format"], got["model"]), (True, "text", "llama-3.3-70b-versatile"))
        self.assertIn("#малыйбизнес", got["text"])
        self.assertEqual(fake.hosts(), ["groq"])

    def test_everything_resting_is_a_quota_error(self):
        self.keys(cloudflare=False, gemini=False)
        os.environ.update(KORIN_WRITE_ORDER="groq", KORIN_WRITE_BUDGET="groq=0")
        fake = self.http(groq=[GROQ_OK])
        with self.assertRaises(providers.ProviderError) as e:
            app.write("пост", kind="own")
        self.assertTrue(app.out_of_quota(e.exception))
        self.assertEqual(fake.calls, [])

    def test_a_broken_writer_never_stops_the_text(self):
        self.keys()
        self.main()
        self.http(groq=[{"unexpected": True}, ValueError("bad json")], cloudflare=[{"success": False, "errors": [{"code": 5007, "message": "No such model"}], "result": None}])
        self.assertEqual(app.write("пост", kind="own")[0], "из основного пути")
        app._writers["cool"].clear()
        self.assertEqual(app.write("пост", kind="own")[0], "из основного пути")
        self.assertEqual(app._writers["used"], {"groq": 2, "cloudflare": 2, "main": 2})

    def test_texts_on_the_main_path_never_use_google_search(self):
        self.keys(cloudflare=False)
        self.listed("gemini-3.5-flash-lite")
        fake = self.http(groq=[providers.ProviderError("HTTP 500: down")], gemini=[gemini_reply(grounded=False)])
        got = app.write("пост", "система", kind="own")
        self.assertEqual(fake.hosts(), ["groq", "gemini"])
        self.assertNotIn("tools", fake.calls[1]["payload"])  # no google_search tool: search quota is for measurement
        self.assertEqual((got.search, app._ai["no_search_until"], app._ai["model"]), (False, 0, None))  # measurement untouched
        # without new keys nothing changes: the old path, search first
        os.environ.pop("GROQ_API_KEY")
        fake.calls.clear()
        app.write("пост", "система", kind="own")
        self.assertIn("tools", fake.calls[0]["payload"])

    def test_a_retired_model_and_a_spent_daily_allocation_get_long_pauses(self):
        now = app.time.time()
        tomorrow = 86400 - int(now) % 86400
        gone = 'HTTP 400: {"error":{"message":"The model `llama-3.3-70b-versatile` has been decommissioned","code":"model_decommissioned"}}'
        self.assertEqual(app._cool_seconds(providers.ProviderError(gone)), 3600)
        self.assertEqual(app._cool_seconds(providers.ProviderError("HTTP 400: The model `x` does not exist or you do not have access to it.")), 3600)
        self.assertEqual(app._cool_seconds(providers.ProviderError("HTTP 400: bad request")), 0)
        spent = 'HTTP 403: {"errors":[{"message":"you have used up your daily free allocation of 10,000 neurons","code":4006}]}'
        self.assertAlmostEqual(app._cool_seconds(providers.ProviderError(spent)), max(600, tomorrow), delta=2)
        self.assertAlmostEqual(app._cool_seconds(providers.ProviderError('cloudflare: пустой ответ [{"code": 4006, "message": "x"}]')), max(600, tomorrow), delta=2)
        self.assertEqual(app._cool_seconds(providers.ProviderError("HTTP 429: Limit 8000, Used 4006, Requested 900 on tokens per minute (TPM)")), 900)

    def test_main_path_budget_counts_only_when_set(self):
        self.keys(groq=False, cloudflare=False)
        seen = self.main()
        os.environ["KORIN_WRITE_BUDGET"] = "main=1"
        app.write("пост", kind="own")
        with self.assertRaises(providers.ProviderError):
            app.write("пост", kind="own")
        self.assertEqual(len(seen), 1)

    def test_bad_cloudflare_settings_are_refused_before_any_call(self):
        fake = self.http(cloudflare=[CF_OK])
        os.environ.update(CLOUDFLARE_ACCOUNT_ID="../../evil", CLOUDFLARE_API_TOKEN="c")
        with self.assertRaises(providers.ProviderError):
            providers.cloudflare("p")
        os.environ.update(CLOUDFLARE_ACCOUNT_ID=CF_ID, CLOUDFLARE_MODEL="@cf/x?y=1")
        with self.assertRaises(providers.ProviderError):
            providers.cloudflare("p")
        self.assertEqual(fake.calls, [])


# ---------- 4. comments we chose not to answer ----------
class DeclinedCommentsTest(Base):
    def setUp(self):
        super().setUp()
        os.environ.update(KORIN_ADMIN_KEY="secret", THREADS_TOKEN="tok")
        self.talk = [
            {"id": "c1", "text": "Проблема не в нейросетях, а в запросах.", "username": "artur", "replied_to": {"id": "p1"}},
            {"id": "c3", "text": "ты дурак", "username": "troll", "replied_to": {"id": "p1"}},
        ]
        self.sent, self.asked = [], []

        def threads(method, path, params):
            if path == "/me":
                return {"username": "korin.kz"}
            if path == "/me/threads" and method == "GET":
                return {"data": [{"id": "p1", "text": "Наш пост"}]}
            if path.endswith("/conversation"):
                return {"data": list(self.talk)}
            if path == "/me/threads":
                self.sent.append(params.get("reply_to_id"))
                self.talk.append({"id": "r" + params.get("reply_to_id", ""), "text": params.get("text"), "username": "korin.kz", "replied_to": {"id": params.get("reply_to_id")}})
            return {"id": "n1"}

        self.patch(app, "_threads", threads)

    def model(self, troll_answer='{"answer": false, "reply": ""}'):
        def ask(prompt, system=None):
            self.asked.append("troll" if "ты дурак" in prompt else "other")
            return (troll_answer if "ты дурак" in prompt else '{"answer": true, "reply": "Согласны, запрос решает многое."}', [], "test-model")
        self.patch(app, "ask", ask)

    def test_a_declined_comment_is_not_sent_to_the_model_again(self):
        self.model()
        runs = [app.marketer_replies({"key": "secret"}) for _ in range(3)]
        self.assertEqual(self.asked, ["other", "troll"])  # three runs, one question about the troll
        self.assertEqual([r["skipped"] for r in runs], [1, 1, 1])  # still reported as skipped
        self.assertEqual(self.sent, ["c1"])
        self.assertIn("c3", app._declined)

    def test_an_unreadable_answer_is_asked_again(self):
        self.model(troll_answer="модель ответила не JSON")
        app.marketer_replies({"key": "secret"})
        app.marketer_replies({"key": "secret"})
        self.assertEqual(self.asked.count("troll"), 2)
        self.assertNotIn("c3", app._declined)

    def test_the_memory_is_bounded(self):
        self.patch(app, "DECLINED_KEEP", 3)
        for i in range(5):
            app._decline(f"id{i}")
        self.assertEqual(list(app._declined), ["id2", "id3", "id4"])

    def test_the_list_survives_a_sleep_when_the_caller_keeps_it(self):
        self.model()
        first = app.marketer_replies({"key": "secret", "declined": []})
        self.assertEqual(first["declined"], ["c3"])
        app._declined.clear()  # the free server slept between runs
        second = app.marketer_replies({"key": "secret", "declined": first["declined"]})
        self.assertEqual(self.asked, ["other", "troll"])  # the troll was not asked again
        self.assertEqual((second["skipped"], second["declined"]), (1, ["c3"]))
        # bad items are ignored, the list handed back is bounded, and a caller that does not send it sees the old answer
        app._declined.clear()
        third = app.marketer_replies({"key": "secret", "declined": [f"id{i}" for i in range(400)] + [5, "x" * 41, "../x", None, {"a": 1}]})
        self.assertFalse({"../x", "x" * 41} & set(third["declined"]))
        self.assertEqual(third["declined"][-1], "c3")
        app._declined.clear()
        fourth = app.marketer_replies({"key": "secret", "declined": [f"id{i}" for i in range(400)]})
        self.assertEqual(len(fourth["declined"]), app.DECLINED_CARRY)
        self.assertEqual(set(app.marketer_replies({"key": "secret"})), {"ok", "brand", "replied", "skipped"})

    def test_optional_age_limit(self):
        self.model()
        old = app.datetime.datetime.fromtimestamp(app.time.time() - 7 * 3600, app.datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
        self.talk[1]["timestamp"] = old
        app.marketer_replies({"key": "secret"})
        self.assertEqual(self.asked, ["other", "troll"])  # off by default
        self.asked.clear()
        app._declined.clear()
        os.environ["KORIN_REPLY_MAX_AGE_HOURS"] = "6"
        app.marketer_replies({"key": "secret"})
        self.assertEqual(self.asked, [])


# ---------- 5. with no new keys nothing changes for the pages ----------
class UnchangedTest(Base):
    def test_writing_endpoints_call_ask_exactly_as_before(self):
        calls = []

        def ask(*args, **kw):
            calls.append((args, kw))
            return ('{"text": "Пост.", "tags": ["малыйбизнес"], "slides": [{"tag": "t", "title": "Заголовок"}], "posts": [{"threads": "п"}], "fit": false, "answer": false}', [], "test-model")

        self.patch(app, "ask", ask)
        self.patch(app, "read_page", lambda raw: {"url": raw, "status": 404, "title": "", "text": ""})
        self.patch(app, "_threads", lambda m, p, params: {"username": "me", "data": [{"id": "p1", "text": "пост"}] if p == "/me/threads" else [{"id": "c1", "text": "Интересно, а как проверить?", "username": "u", "replied_to": {"id": "p1"}}] if p.endswith("conversation") else []})
        os.environ.update(KORIN_ADMIN_KEY="secret", THREADS_TOKEN="tok")
        outs = [
            app.run_agent({"task": "faq", "context": {"company": "A"}}),
            app.analyze({"company": "A", "answers": [{"q": "q", "a": "a"}]}),
            app.social_plan({"brand": "A", "about": "b"}),
            app.social_reply({"brand": "A", "about": "b", "thread": "достаточно длинная ветка про бизнес"}),
            app.marketer_plan({"key": "secret", "slot": 1}),
            app.marketer_replies({"key": "secret"}),
        ]
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(len(args) == 2 and kw == {} for args, kw in calls))  # (prompt, system), nothing new
        self.assertEqual([set(o) for o in outs], [
            {"text", "model", "steps"}, {"analysis", "text", "steps", "model"}, {"posts", "model"},
            {"fit", "why", "reply", "model"}, {"ok", "brand", "format", "single", "topic", "text", "slides", "theme", "model", "slot"},
            {"ok", "brand", "replied", "skipped"}])
        self.assertEqual(outs[0]["text"], ask()[0])
        self.assertEqual(app._writers["used"], {})

    def test_health_keeps_its_fields(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{srv.server_port}/api/health", timeout=10) as r:
                data = json.loads(r.read())
        finally:
            srv.shutdown()
        self.assertTrue({"ok", "ai", "provider", "threads", "marketer"} <= set(data))
        self.assertEqual(data["writers"], {"own": ["main"], "client": ["main"]})


if __name__ == "__main__":
    unittest.main()
