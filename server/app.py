"""Körin backend: real site audit, real AI-visibility check and AI agents.

Standard library only. Run:  python3 -m server.app   (PORT, GEMINI_API_KEY or OPENAI_API_KEY, KORIN_ORIGINS)

Gemini model: GEMINI_MODEL wins; then GEMINI_FALLBACKS; then GEMINI_PREFER (comma list, default
gemini-3.5-flash-lite) as far as the key's own model list (ListModels, read once an hour) has them; then other
flash / flash-lite models from that list, newest first (other families such as gemini-4-hydrogen only through
GEMINI_PREFER: they cost more); gemini-2.5-flash keeps a place while Google still lists it. At most 5 are tried.

Measurement (/api/quick, /api/check-one, /api/check) always asks the engine it measures. An answer made without a
real web search comes back with "search": false and is kept for an hour only, not the 72 hours of a normal answer.

Texts (our Threads posts and replies, agents, breakdowns, social texts) go through write(), optional providers:
  GROQ_API_KEY (+ GROQ_MODEL, default openai/gpt-oss-120b — check in console.groq.com) — only our own public
    posts and replies, never client data;
  CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN (+ CLOUDFLARE_MODEL, default @cf/meta/llama-3.3-70b-instruct-fp8-fast,
    CLOUDFLARE_MAX_TOKENS 2048 — check in the Cloudflare dashboard);
  "main" — the Gemini/OpenAI path above.
  KORIN_WRITE_ORDER (default groq,cloudflare,main), KORIN_WRITE_ORDER_CLIENT (for client data; groq is always dropped),
  KORIN_WRITE_BUDGET like "groq=300,cloudflare=60,main=100" (calls a UTC day; main has no limit unless set),
  KORIN_WRITE_COOLDOWN seconds after a 429 (default 900). Budgets and pauses live in memory: a restart resets them.
  With neither GROQ nor CLOUDFLARE keys set, write() is ask() exactly as before. When they are set and a text
  falls through to "main", it is written without Google Search: search is for measurement.
KORIN_REPLY_MAX_AGE_HOURS: when set, comments older than this are not sent to the model (default: no limit).
/api/marketer-replies takes and returns "declined" (ids of comments the model chose not to answer) when the caller
sends it: the free server sleeps between runs and forgets, so replies.yml keeps the list in the Actions cache.
"""
import ipaddress
import json
import os
import re
import base64
import datetime
import hashlib
import hmac
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiintel import detect, providers

ORIGINS = [o.strip() for o in os.environ.get("KORIN_ORIGINS", "https://kenesai.asia").split(",") if o.strip()]
BOTS = [
    ("OAI-SearchBot", "поиск в ChatGPT"),
    ("GPTBot", "обучение моделей OpenAI"),
    ("ChatGPT-User", "когда человек просит ChatGPT открыть страницу"),
    ("PerplexityBot", "Perplexity"),
    ("Google-Extended", "Gemini"),
    ("ClaudeBot", "Claude"),
    ("bingbot", "поиск Bing: через него ChatGPT часто ищет в интернете"),
]
MAX_BYTES = 2_000_000
UA = "Mozilla/5.0 (compatible; KorinAudit/0.1)"


class BadRequest(Exception):
    pass


# ---------- site audit ----------
def safe_url(raw):
    """Normalise a user-supplied address and refuse anything that is not a public web host."""
    raw = (raw or "").strip()
    if not raw:
        raise BadRequest("Укажите адрес сайта.")
    if "://" not in raw:
        raw = "https://" + raw
    u = urllib.parse.urlsplit(raw)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise BadRequest("Нужен обычный адрес сайта, например example.kz.")
    if u.port not in (None, 80, 443):
        raise BadRequest("Нестандартные порты не проверяются.")
    if os.environ.get("KORIN_SKIP_DNS_CHECK") != "1":
        try:
            infos = socket.getaddrinfo(u.hostname, None)
        except socket.gaierror:
            raise BadRequest("Сайт не найден: проверьте адрес.") from None
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise BadRequest("Этот адрес нельзя проверить.")
    return f"{u.scheme}://{u.netloc}"


class _NoRedirectAway(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to public hosts, so a site cannot bounce the audit inward."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_NoRedirectAway)


def fetch(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru,kk;q=0.9"})
    t0 = time.monotonic()
    try:
        with _opener.open(req, timeout=timeout) as r:
            body = r.read(MAX_BYTES + 1)[:MAX_BYTES]
            charset = r.headers.get_content_charset() or "utf-8"
            return r.status, body.decode(charset, "replace"), time.monotonic() - t0, r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, "", time.monotonic() - t0, url
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return 0, "", time.monotonic() - t0, url


def visible_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg|template)\b.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", html))).strip()


def analyse_page(status, html, seconds):
    """Checks on the home page that do not need a browser. Each: (id, label, ok, detail)."""
    text = visible_text(html)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    desc = re.search(r"(?is)<meta[^>]+name=[\"']description[\"'][^>]*>", html)
    return [
        ("open", "Главная страница открывается без входа", status == 200, f"код ответа {status or 'нет ответа'}"),
        ("text", "Текст виден без JavaScript", len(text) >= 400, f"{len(text)} знаков текста в коде страницы"),
        ("title", "Есть заголовок и описание страницы", bool(title and title.group(1).strip() and desc), "теги title и meta description"),
        ("price", "На странице упомянуты цены", bool(re.search(r"(?i)цен[аыу]|стоимост|прайс|тариф|₸|\bтг\b|тенге|бағасы", text)), "слова о цене или знак ₸"),
        ("contact", "Есть телефон или адрес", bool(re.search(r"(\+7|8)[\s(-]*7\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}", text) or re.search(r"(?i)\bул\.|пр\.|проспект|көшесі|даңғылы", text)), "телефон или адрес в тексте"),
        ("schema", "Есть разметка данных о компании", "application/ld+json" in html, "блок JSON-LD"),
        ("speed", "Страница отвечает быстрее трёх секунд", 0 < seconds < 3, f"{seconds:.2f} с"),
    ]


def audit_site(raw_url):
    base = safe_url(raw_url)
    rstatus, robots, _, _ = fetch(base + "/robots.txt")
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots.splitlines() if rstatus == 200 else [])
    bots = [{"bot": b, "what": w, "allowed": rp.can_fetch(b, base + "/")} for b, w in BOTS]
    status, html, seconds, final = fetch(base + "/")
    checks = [{"id": i, "label": l, "ok": ok, "detail": d} for i, l, ok, d in analyse_page(status, html, seconds)]
    checks.insert(0, {"id": "robots", "label": "Файл robots.txt найден", "ok": rstatus == 200, "detail": f"код ответа {rstatus or 'нет ответа'}"})
    # Bing finds pages through the sitemap: either the standard address or the one named in robots.txt
    listed = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots if rstatus == 200 else "")
    sstatus, sbody, _, _ = fetch(base + "/sitemap.xml")
    has_map = sstatus == 200 and bool(re.search(r"<(urlset|sitemapindex)\b", sbody))
    checks.append({"id": "sitemap", "label": "Есть карта сайта для Bing и других поисковиков", "ok": has_map or bool(listed), "detail": "sitemap.xml найден" if has_map else "адрес карты указан в robots.txt" if listed else "sitemap.xml не найден"})
    return {"url": final, "bots": bots, "checks": checks}


# ---------- connectors ----------
def page_url(raw):
    """Full address of one public page, checked the same way as a site address."""
    raw = (raw or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    safe_url(raw)
    u = urllib.parse.urlsplit(raw)
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path or "/", u.query, ""))


def read_page(raw):
    """What anyone can see at this address: status, title and visible text."""
    url = page_url(raw)
    status, html, _, final = fetch(url)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return {"url": final, "status": status, "title": unescape(title.group(1)).strip()[:160] if title else "", "text": visible_text(html)}


def connect(body):
    page = read_page(body.get("url"))
    return {"ok": page["status"] == 200, "url": page["url"], "status": page["status"], "title": page["title"], "chars": len(page["text"])}


# ---------- AI ----------
def provider_name():
    return "gemini" if os.environ.get("GEMINI_API_KEY") else "openai" if os.environ.get("OPENAI_API_KEY") else ""


_ai = {"model": None, "no_search_until": 0}

# The cheapest name from Google's migration advice. Tried only if the key's own model list has it (when the list
# cannot be read, as it is), so a wrong or retired name costs nothing. Override: GEMINI_PREFER. gemini-4-hydrogen,
# the other advised name, costs ~3-5x more: add it there only after a live test shows search works and it is needed.
GEMINI_PREFER = "gemini-3.5-flash-lite"
GEMINI_ALIASES = ["gemini-flash-lite-latest", "gemini-flash-latest"]  # Google's moving names, tried blind only when the list is unreadable
GEMINI_LAST = "gemini-2.5-flash"  # reported to be switched off for the Gemini API on 2026-12-01; last while it is listed
_NOT_TEXT = ("tts", "image", "live", "embedding", "audio", "vision", "robotics", "computer", "exp", "pro", "ultra")  # and pro/ultra cost far more


def _gemini_list(page):
    """One page of Google's ListModels for our key."""
    key = "".join(os.environ.get("GEMINI_API_KEY", "").split())
    url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200" + ("&pageToken=" + urllib.parse.quote(page) if page else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"x-goog-api-key": key}), timeout=15) as r:
        return json.loads(r.read())


def _listed_models():
    """Text models this key can really call, asked from Google once an hour: old names disappear without warning.
    Any Gemini text model is listed, so a GEMINI_PREFER name without «flash» (gemini-4-hydrogen) is found;
    gemini_models() picks only flash ones by itself."""
    if time.time() - _ai.get("listed_at", 0) > 3600:
        _ai["listed_at"] = time.time()
        try:
            found, page = [], ""
            for _ in range(5):
                data = _gemini_list(page)
                found += [m["name"].split("/")[-1] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
                page = data.get("nextPageToken") or ""
                if not page:
                    break
            _ai["listed"] = [m for m in dict.fromkeys(found) if m.startswith("gemini-") and not any(x in m for x in _NOT_TEXT)]
        except Exception as e:  # any failure (reset, half-read page, 403, odd JSON) must not fail the visitor's request
            _ai["listed_at"] = time.time() - 3300  # ask again in 5 minutes, keep the last good list meanwhile
            print(f"could not list gemini models: {type(e).__name__}: {e}", flush=True)
    return _ai.get("listed", [])


def _version(name):
    m = re.match(r"gemini-(\d+(?:\.\d+)?)-", name + "-")
    return float(m.group(1)) if m else None


def _newest_first(names):
    """Newer version first; in one version the cheap kinds first (lite, flash, other); stable before preview."""
    return sorted(names, key=lambda m: (-(_version(m) or 0), 0 if "lite" in m else 1 if "flash" in m else 2, "preview" in m, m))


def gemini_models():
    listed = _listed_models()
    pinned = [os.environ.get("GEMINI_MODEL", "").strip()] + [m.strip() for m in os.environ.get("GEMINI_FALLBACKS", "").split(",")]
    prefer = [m.strip() for m in os.environ.get("GEMINI_PREFER", GEMINI_PREFER).split(",") if m.strip() and m.strip() != GEMINI_LAST]
    if listed:
        # beyond GEMINI_PREFER only flash / flash-lite are picked by themselves: other families (hydrogen, argon,
        # carbon, neon...) may cost several times more or be paid-only
        others = [m for m in listed if m not in prefer and m != GEMINI_LAST and "flash" in m]
        newer = [m for m in others if _version(m) is None or _version(m) > _version(GEMINI_LAST)]
        keep_last = GEMINI_LAST in listed
        auto = [m for m in prefer if m in listed] + _newest_first(newer) + [GEMINI_LAST] * keep_last + _newest_first([m for m in others if m not in newer])
    else:  # the list could not be read: try the names as they are, the old model last
        auto, keep_last = prefer + GEMINI_ALIASES + [GEMINI_LAST], True
    order = []
    for m in pinned + auto:
        if m and m not in order:
            order.append(m)
    # gemini-2.5-flash keeps the last of the 5 places while it may exist: it is the one known to search on our key
    order = order[:5] if not keep_last or GEMINI_LAST in order[:5] else order[:4] + [GEMINI_LAST]
    if _ai["model"] in order and time.time() - _ai.get("model_at", time.time()) < 3600:
        # the one that last answered with search goes first, for an hour: one busy moment must not move us for good
        order.remove(_ai["model"])
        order.insert(0, _ai["model"])
    return order


def ask(prompt, system=None, web=True):
    """One answer, quickly. A person is waiting on the page, so no long retries:
    try with web search, then without it, on each model in turn; remember what worked.
    web=False (texts from the router) never asks for Google Search, so texts neither spend the search quota
    measurement needs nor switch measurement off search for 15 minutes when their search fails.
    The answer is (text, urls, model) with .engine and .search: search is True only when the web was really searched."""
    name = provider_name()
    if not name:
        raise providers.ProviderError("не задан ключ нейросети")
    if name != "gemini":
        out = providers.openai(prompt, system)
        return providers.Answer(*out[:3], engine="openai", search=getattr(out, "search", False))
    with_search = web and time.time() > _ai["no_search_until"]
    last, ungrounded = None, None
    for search in ([True, False] if with_search else [False]):
        if not search and ungrounded is not None:
            break  # a model already answered, only without searching: a plain no-search round adds nothing
        models, i = gemini_models(), 0
        while i < len(models):
            model = models[i]
            i += 1
            try:
                # one quick second try covers a busy moment at Google
                out = providers.gemini(prompt, system, search=search, model=model, retries=1, timeout=60)
            except providers.ProviderError as e:
                if "не задан" in str(e):
                    raise
                last = e
                print(f"gemini {model} search={search} failed: {str(e)[:700]}", flush=True)
                continue
            searched = search and getattr(out, "search", True)
            if search and not searched:
                # the model was given Google Search but answered from memory. Keep this answer and try ONE more
                # model, the old flash first while it is in the list (it is the one known to search on our key),
                # so a new model that skips search does not quietly make measurement worse than before.
                if ungrounded is not None:
                    break
                ungrounded = out
                rest = models[i:]
                nxt = GEMINI_LAST if GEMINI_LAST in rest else (rest[0] if rest else None)
                models = models[:i] + ([nxt] if nxt else [])
                continue
            # only a model that really searched (or any, while search is off for measurement) jumps the queue;
            # a text written without search says nothing about search, so it leaves the queue alone
            if searched or (web and not with_search):
                _ai["model"], _ai["model_at"] = model, time.time()
            if with_search and not search:
                _ai["no_search_until"] = time.time() + 900  # this key cannot search right now; stop asking for a while
            return providers.Answer(*out[:3], engine="gemini", search=searched)
    if ungrounded is not None:
        return providers.Answer(*ungrounded[:3], engine="gemini", search=False)
    raise last


def _how(ans):
    """Which engine answered and whether it really searched the web; None when unknown (a stand-in in tests)."""
    return {"engine": getattr(ans, "engine", None) or provider_name() or None, "search": getattr(ans, "search", None)}


def no_ai(error):
    return "не задан" in str(error) or "insufficient_quota" in str(error)


def out_of_quota(error):
    return any(k in str(error) for k in ("HTTP 429", "RESOURCE_EXHAUSTED", "quota"))


# ---------- writing: our posts and replies, texts for clients ----------
# Only texts come here. A measurement never does: it must be the answer of the engine we measure (ask above).
WRITE_ORDER = "groq,cloudflare,main"  # "main" is the Gemini/OpenAI path of ask()
CLIENT_OK = ("cloudflare", "main")  # a client's data goes only where it is not used to train models; never Groq
WRITE_BUDGET = {"groq": 300, "cloudflare": 60}  # calls a UTC day, a guess under the free tiers: check in each console
_writers = {"day": "", "used": {}, "cool": {}}  # in memory: a restart or a sleep of the free server resets it
_wlock = threading.Lock()


def _writer_ready(name):
    if name == "groq":
        return bool(os.environ.get("GROQ_API_KEY", "").strip())
    if name == "cloudflare":
        return bool(os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip() and os.environ.get("CLOUDFLARE_API_TOKEN", "").strip())
    return name == "main" and bool(provider_name())


def writer_chain(kind):
    """Writers to try for a kind of text, in order: "own" — our public posts and replies, anything else — a client's data."""
    client = kind != "own"
    raw = (os.environ.get("KORIN_WRITE_ORDER_CLIENT", "") if client else "") or os.environ.get("KORIN_WRITE_ORDER", "") or WRITE_ORDER
    chain = []
    for n in raw.lower().split(","):
        n = {"gemini": "main", "openai": "main"}.get(n.strip(), n.strip())
        if n in ("groq", "cloudflare", "main") and n not in chain and (not client or n in CLIENT_OK) and _writer_ready(n):
            chain.append(n)
    return chain or ["main"]  # nothing usable listed: the old path, which also says clearly when no key is set


def _budget(name):
    """Calls a day for a writer; None means no limit (the main path has none unless KORIN_WRITE_BUDGET sets one)."""
    got = dict(re.findall(r"(\w+)\s*=\s*(\d+)", os.environ.get("KORIN_WRITE_BUDGET", "")))
    return int(got[name]) if name in got else WRITE_BUDGET.get(name)


def _take(name):
    """True and one call counted, unless the writer is over today's budget or still cooling down after an error."""
    with _wlock:
        today = time.strftime("%Y-%m-%d", time.gmtime(time.time()))
        if _writers["day"] != today:
            _writers.update(day=today, used={})
        if _writers["cool"].get(name, 0) > time.time():
            return False
        limit = _budget(name)
        if limit is not None and _writers["used"].get(name, 0) >= limit:
            return False
        _writers["used"][name] = _writers["used"].get(name, 0) + 1
        return True


def _cool_seconds(error):
    """How long to leave a writer alone after this error. Asking again at once only burns quota."""
    s = str(error)
    until_tomorrow = max(600, 86400 - int(time.time()) % 86400)
    if re.search(r"(?i)neurons|\"code\":\s*4006\b", s):
        return until_tomorrow  # Cloudflare's daily free allocation, whatever HTTP status it comes with
    if re.match(r"HTTP 4\d\d\b|groq:|cloudflare:", s) and re.search(r"(?i)decommission|model_not_found|no such model|model\W.{0,80}does not exist", s):
        return 3600  # a retired or wrong model name: asking again on every call only burns time
    if out_of_quota(error):
        if re.search(r"(?i)per ?day|daily|\bRPD\b|\bTPD\b|insufficient_quota", s):
            return until_tomorrow  # a daily limit: until the next UTC day
        try:
            return int(os.environ.get("KORIN_WRITE_COOLDOWN", "900"))
        except ValueError:
            return 900
    if re.match(r"HTTP (401|403|404)\b", s) or "не задан" in s:
        return 3600  # a wrong key or model name will not fix itself in a minute
    if re.match(r"HTTP 5\d\d|network", s):
        return 120
    return 0


def write(prompt, system=None, kind="client"):
    """A text for a person to read: kind "own" — our public posts and replies, "client" — texts made from a client's data.
    Each writer in writer_chain() is tried once; one over its daily budget or cooling down is skipped.
    With no GROQ or CLOUDFLARE keys this is ask() itself, exactly as before."""
    chain = writer_chain(kind)
    if chain == ["main"] and _budget("main") is None:
        return ask(prompt, system)
    last = None
    for name in chain:
        if not _take(name):
            continue
        try:
            # a text on the main path is written without Google Search: search is for measurement only
            return ask(prompt, system, web=False) if name == "main" else getattr(providers, name)(prompt, system)
        except Exception as e:  # a failing writer must never stop the post: the next one writes it
            if name == "main" and not isinstance(e, providers.ProviderError):
                raise
            last = e if isinstance(e, providers.ProviderError) else providers.ProviderError(f"{name}: {type(e).__name__}")
            pause = _cool_seconds(last)
            if pause:
                with _wlock:
                    _writers["cool"][name] = time.time() + pause
            print(f"writer {name} ({kind}) failed, pause {pause} s: {str(last)[:300]}", flush=True)
    raise last or providers.ProviderError("HTTP 429: quota: у всех нейросетей для текстов на сегодня кончился лимит или идёт пауза после ошибки")


# ---------- who was named ----------
def _squash(t):
    return re.sub(r"[^a-zа-яё0-9]", "", t.lower().replace("ё", "е"))


def named_ids(text, companies):
    """Ids of the companies named in the answer. Besides the exact name, catches a name typed
    as one word when the model writes it as two («vegalegal» and «Vega Legal»)."""
    ids = {m["company_id"] for m in detect.find_mentions(text, companies)}
    flat = _squash(text)
    for c in companies:
        key = _squash(c["name"])
        if len(key) >= 5 and key in flat:
            ids.add(c["id"])
    return ids


# ---------- AI-visibility check ----------
def run_check(body):
    company = str(body.get("company", "")).strip()[:60]
    rivals = [str(r).strip()[:60] for r in body.get("rivals", []) if str(r).strip()][:5]
    questions = [str(q).strip()[:200] for q in body.get("questions", []) if str(q).strip()][:12]
    if not company or not rivals or not questions:
        raise BadRequest("Нужны компания, конкуренты и вопросы.")
    names = [company, *rivals]
    companies = [{"id": i, "name": n, "aliases": []} for i, n in enumerate(names)]
    system = f"Пользователь находится в городе {str(body.get('city', 'Алматы'))[:40]}, Казахстан."
    grid, answers, sources, errors, used, searched, engine = [], [], {}, 0, None, [], None
    for q in questions:
        try:
            ans = ask(q, system)  # measurement: the engine itself, never write()
        except providers.ProviderError as e:
            if no_ai(e):
                raise
            errors += 1
            grid.append(None)
            answers.append(None)
            searched.append(None)
            continue
        text, urls, model = ans
        mentioned = named_ids(text, companies)
        grid.append([i in mentioned for i in range(len(names))])
        answers.append(text[:4000])
        used, engine = model, _how(ans)["engine"]
        searched.append(_how(ans)["search"])
        for c in detect.find_citations(text, urls):
            sources[c["domain"]] = sources.get(c["domain"], 0) + 1
    top = sorted(sources.items(), key=lambda kv: -kv[1])[:8]
    return {"names": names, "questions": questions, "grid": grid, "answers": answers, "sources": top, "errors": errors, "model": used, "engine": engine, "search": searched}


# ---------- free check on the landing page ----------
# The model's answer depends on the question and the city, not on who asks.
# So one paid call serves every company in the same niche for three days.
QUICK_TTL = 72 * 3600
QUICK_NOSEARCH_TTL = 3600  # an answer made without web search is the model's memory: kept an hour only, marked
QUICK_PER_DAY = int(os.environ.get("KORIN_QUICK_PER_DAY", "400"))  # real model calls a day, cache hits are free
_quick = {"day": "", "n": 0}
_qcache, _qlocks, _qguard = {}, {}, threading.Lock()


def quick_questions(niche, city):
    return [f"{niche} в городе {city}: кого посоветуешь?", f"{niche} с хорошими отзывами, {city} — назови конкретные компании", f"{city} қаласында {niche}: қай компанияны ұсынасың?"]


def cached_answer(question, city):
    """(text, urls, model, from_cache, how), how = {"engine", "search"}. Two visitors asking the same thing at once share one call.
    An answer made with a real web search is kept 72 hours. One without it is the model's memory: it is kept an hour only
    (a visitor retrying the same niche and city does not spend the daily quota again) and always comes back "search": false."""
    key = (question.lower(), city.lower())
    with _qguard:
        lock = _qlocks.setdefault(key, threading.Lock())
    with lock:
        hit = _qcache.get(key)
        if hit and hit[0] > time.time():
            return hit[1], hit[2], hit[3], True, dict(hit[4])
        today = time.strftime("%Y-%m-%d")
        with _qguard:
            if _quick["day"] != today:
                _quick.update(day=today, n=0)
            if _quick["n"] >= QUICK_PER_DAY:
                raise BadRequest("На сегодня бесплатные проверки закончились. Оставьте заявку, и мы сделаем замер вручную.")
            _quick["n"] += 1
        ans = ask(question, f"Пользователь находится в городе {city}, Казахстан. Называй конкретные компании.")  # never write()
        text, urls, model = ans
        how = _how(ans)
        ttl = QUICK_TTL
        if how["search"] is False:
            ttl = QUICK_NOSEARCH_TTL
            print(f"measurement answer without web search ({how['engine']} {model}): returned with search=false, kept {ttl // 60} min, not 72 h", flush=True)
        with _qguard:
            if len(_qcache) > 3000:
                now = time.time()
                for k in [k for k, v in _qcache.items() if v[0] <= now] or list(_qcache)[:500]:
                    _qcache.pop(k, None)
                    _qlocks.pop(k, None)
            _qcache[key] = (time.time() + ttl, text, urls, model, how)
        return text, urls, model, False, dict(how)


def quick_check(body):
    """Real questions about the visitor's niche, answers word for word. With "i" — one question, so the page can show cards as they come."""
    company, niche, city = (re.sub(r"\s+", " ", str(body.get(k, ""))).strip()[:60] for k in ("company", "niche", "city"))
    if not company or not niche or not city:
        raise BadRequest("Нужны компания, ниша и город.")
    questions = quick_questions(niche, city)
    me = [{"id": 0, "name": company, "aliases": []}]

    def one(q):
        text, urls, model, cached, how = cached_answer(q, city)
        return {"question": q, "answer": text[:3000], "named": bool(named_ids(text, me)), "sources": sorted({c["domain"] for c in detect.find_citations(text, urls)})[:6], "model": model, "cached": cached, **how}

    if body.get("i") is not None:
        if body["i"] not in (0, 1, 2):
            raise BadRequest("Нет такого вопроса.")
        return {"company": company, "result": one(questions[body["i"]])}
    out, fails = [None] * len(questions), []

    def run(i, q):
        try:
            out[i] = one(q)
        except (providers.ProviderError, BadRequest) as e:
            fails.append(e)

    threads = [threading.Thread(target=run, args=(i, q)) for i, q in enumerate(questions)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = [x for x in out if x]
    if not got:
        raise fails[0]
    return {"company": company, "results": got, "named": sum(x["named"] for x in got), "asked": len(got)}


# ---------- onboarding: find competitors ----------
def clean_names(text, company):
    """Company names from a free-form model answer: one per line, no numbering, no explanations."""
    names = []
    for line in text.splitlines():
        n = re.sub(r"^[\s\-–—•*#\d.)]+", "", line).strip()
        n = re.split(r"\s[—–-]\s|:|\(", n)[0].strip().strip("*«»\"'.").strip()
        if 2 <= len(n) <= 40 and n.lower() not in (company.lower(), "нет") and n.lower() not in [x.lower() for x in names]:
            names.append(n)
    return names[:5]


def suggest(body):
    company, niche, city = (re.sub(r"\s+", " ", str(body.get(k, ""))).strip()[:60] for k in ("company", "niche", "city"))
    if not company or not niche or not city:
        raise BadRequest("Нужны компания, ниша и город.")
    text, _, model = ask(f"Назови 5 реально существующих компаний — прямых конкурентов «{company}» в нише «{niche}», город {city}, Казахстан. Ответь списком: одно название в строке, без нумерации и пояснений. Не выдумывай: если не знаешь таких компаний, ответь одним словом НЕТ.")
    return {"rivals": clean_names(text, company), "model": model}


# ---------- real check, one question per request ----------
def check_one(body):
    """One question of the check. The page asks them one after another and shows progress."""
    company = str(body.get("company", "")).strip()[:60]
    rivals = [str(r).strip()[:60] for r in body.get("rivals", []) if str(r).strip()][:5]
    question = re.sub(r"\s+", " ", str(body.get("question", ""))).strip()[:200]
    city = str(body.get("city", "Алматы")).strip()[:40] or "Алматы"
    if not company or not rivals or not question:
        raise BadRequest("Нужны компания, конкуренты и вопрос.")
    names = [company, *rivals]
    companies = [{"id": i, "name": n, "aliases": []} for i, n in enumerate(names)]
    text, urls, model, cached, how = cached_answer(question, city)
    mentioned = named_ids(text, companies)
    return {"row": [i in mentioned for i in range(len(names))], "answer": text[:4000], "sources": sorted({c["domain"] for c in detect.find_citations(text, urls)}), "model": model, "cached": cached, **how}


# ---------- breakdown: why them and not you ----------
NOT_COMPANY_SITES = ("2gis", "google", "yandex", "instagram", "facebook", "youtube", "wikipedia", "tiktok", "t.me", "vk.com", "linkedin", "zoon", "flamp", "krisha", "kolesa", "kaspi", "gov.kz", "egov", "adilet", "zakon.kz", "tengrinews", "forbes", "kapital", "inform.kz", "nur.kz")


def parse_json(text):
    """The model is asked for JSON but may wrap it in prose or a code fence."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def analyze(body):
    company, niche, city = (re.sub(r"\s+", " ", str(body.get(k, ""))).strip()[:80] for k in ("company", "niche", "city"))
    rivals = [str(r).strip()[:60] for r in body.get("rivals", []) if str(r).strip()][:6]
    answers = [(str(x.get("q", ""))[:200], str(x.get("a", ""))[:1400]) for x in body.get("answers", []) if isinstance(x, dict) and x.get("a")][:10]
    if not company or not answers:
        raise BadRequest("Сначала нужна проверка: без ответов нейросети разбирать нечего.")
    steps, pages = [{"text": f"Перечитал ответы нейросети: {len(answers)}", "ok": True}], []
    site = str(body.get("site", "")).strip()[:300]
    if site:
        try:
            page = read_page(site)
            got = page["status"] == 200 and len(page["text"]) >= 200
            steps.append({"text": f"Прочитал ваш сайт: {len(page['text'])} знаков" if got else f"Ваш сайт не отдал текст (код {page['status'] or 'нет ответа'})", "ok": got})
            if got:
                pages.append(f"САЙТ КЛИЕНТА ({page['url']}), заголовок: {page['title']}\n{page['text'][:3000]}")
        except BadRequest as e:
            steps.append({"text": f"Ваш сайт: {e}", "ok": False})
    else:
        steps.append({"text": "Ваш сайт не подключён: сравню только по ответам нейросети", "ok": False})
    theirs = [d for d in (str(x).strip().lower()[:80] for x in body.get("sources", [])) if d and not any(n in d for n in NOT_COMPANY_SITES)][:2]
    for d in theirs:
        try:
            page = read_page(d)
        except BadRequest:
            continue
        if page["status"] == 200 and len(page["text"]) >= 200:
            steps.append({"text": f"Прочитал сайт, на который ссылается нейросеть: {d}", "ok": True})
            pages.append(f"САЙТ, НА КОТОРЫЙ ССЫЛАЕТСЯ НЕЙРОСЕТЬ ({d}), заголовок: {page['title']}\n{page['text'][:2200]}")
    qa = "\n\n".join(f"ВОПРОС: {q}\nОТВЕТ НЕЙРОСЕТИ: {a}" for q, a in answers)
    prompt = f"""Компания клиента: «{company}». Ниша: {niche}. Город: {city}, Казахстан.
Конкуренты: {", ".join(rivals) or "не указаны"}.

Ниже настоящие ответы нейросети на вопросы покупателей и тексты сайтов.

{qa[:11000]}

{chr(10).join(pages)[:8000]}

Разбери, почему нейросеть называет конкурентов и не называет (или редко называет) компанию клиента.
Правила: опирайся только на тексты выше, ничего не выдумывай; пиши очень простыми словами, короткими предложениями, без терминов; цитаты бери дословно из ответов нейросети.
Верни ТОЛЬКО JSON такого вида, без пояснений:
{{"verdict": "главная причина одним предложением",
 "reasons": [{{"rival": "название конкурента", "why": "за что его называет нейросеть", "quote": "короткая дословная цитата из ответа"}}],
 "gaps": [{{"title": "что сделать, начиная с глагола", "why": "что есть у конкурентов и чего нет у клиента", "how": "первый шаг на сегодня", "effort": "час" }}]}}
В reasons от 2 до 4 пунктов, в gaps от 3 до 5, самые сильные первыми. effort — одно из: "час", "день", "неделя"."""
    text, _, model = write(prompt, "Ты опытный маркетолог. Отвечаешь только валидным JSON на русском языке.", kind="client")
    obj = parse_json(text)
    if obj:
        obj = {"verdict": str(obj.get("verdict", ""))[:300],
               "reasons": [{k: str(r.get(k, ""))[:300] for k in ("rival", "why", "quote")} for r in obj.get("reasons", []) if isinstance(r, dict)][:4],
               "gaps": [{k: str(g.get(k, ""))[:300] for k in ("title", "why", "how", "effort")} for g in obj.get("gaps", []) if isinstance(g, dict)][:5]}
    steps.append({"text": f"Разбор написала нейросеть {model}", "ok": True})
    return {"analysis": obj, "text": "" if obj else text[:4000], "steps": steps, "model": model}


# ---------- social: posts and replies ----------
def _brand(body):
    brand, about, city = (re.sub(r"\s+", " ", str(body.get(k, ""))).strip()[:n] for k, n in (("brand", 60), ("about", 300), ("city", 40)))
    if not brand or not about:
        raise BadRequest("Напишите название и чем занимается компания.")
    return brand, about, city or "Казахстан"


def social_plan(body):
    brand, about, city = _brand(body)
    n = max(3, min(7, int(body.get("n", 7)) if str(body.get("n", 7)).isdigit() else 7))
    prompt = f"""Бренд: «{brand}». Чем занимается: {about}. Где: {city}.
Читатели: владельцы малого и среднего бизнеса в Казахстане, которые сидят в Threads и X.

Составь {n} постов на неделю. Каждый пост должен быть полезен сам по себе: наблюдение, короткий совет, вопрос к читателю, цифра или маленькая история. Не реклама в лоб.
Правила: очень простые слова и короткие предложения; без канцелярита и без слов «уникальный», «инновационный», «под ключ»; не обещай результат и не давай гарантий; не выдумывай цифры, клиентов и кейсы; бренд упоминай не чаще чем в каждом третьем посте и только в конце; не больше одного хэштега; без эмодзи в начале строки.
Верни ТОЛЬКО JSON:
{{"posts": [{{"kind": "вопрос", "hook": "фраза для картинки, до 55 знаков, без точки в конце", "threads": "текст для Threads, до 450 знаков", "x": "текст для X, до 260 знаков", "day": "пн"}}]}}
kind — одно из: "вопрос", "совет", "наблюдение", "цифра", "история". day — пн, вт, ср, чт, пт, сб, вс по порядку."""
    text, _, model = write(prompt, "Ты редактор соцсетей для малого бизнеса. Пишешь живо и просто. Отвечаешь только валидным JSON на русском языке.", kind="client")
    obj = parse_json(text) or {}
    posts = [{"kind": str(x.get("kind", "совет"))[:20], "hook": str(x.get("hook", ""))[:90], "threads": str(x.get("threads", ""))[:500], "x": str(x.get("x", ""))[:280], "day": str(x.get("day", ""))[:4]} for x in obj.get("posts", []) if isinstance(x, dict) and x.get("threads")][:7]
    if not posts:
        raise providers.ProviderError("нейросеть вернула план в неверном виде")
    return {"posts": posts, "model": model}


def social_reply(body):
    """Is this thread ours to answer, and what to say. A person reads it and posts it."""
    brand, about, city = _brand(body)
    thread = re.sub(r"\s+", " ", str(body.get("thread", ""))).strip()[:1500]
    if len(thread) < 20:
        raise BadRequest("Вставьте текст ветки, на которую хотите ответить.")
    prompt = f"""Бренд: «{brand}». Чем занимается: {about}. Где: {city}.

Чужая ветка в соцсети:
«{thread}»

Реши, стоит ли бренду отвечать. Подходит, если ветка про малый бизнес, продвижение, клиентов, рекламу, нейросети или близкую тему, и можно сказать что-то полезное. Не подходит, если тема личная, политическая, трагическая, спорная или не связана с делом бренда.
Если подходит — напиши ответ: по смыслу ветки, с пользой для автора, как человек, а не как реклама. Без ссылок. Бренд не называй, если это не отвечает прямо на вопрос автора. До 260 знаков, простыми словами.
Верни ТОЛЬКО JSON: {{"fit": true, "why": "почему подходит или нет, одной фразой", "reply": "текст ответа или пустая строка"}}"""
    text, _, model = write(prompt, "Ты ведёшь соцсети бренда. Отвечаешь только валидным JSON на русском языке.", kind="client")
    obj = parse_json(text)
    if not obj:
        raise providers.ProviderError("нейросеть вернула ответ в неверном виде")
    fit = bool(obj.get("fit"))
    return {"fit": fit, "why": str(obj.get("why", ""))[:300], "reply": str(obj.get("reply", ""))[:280] if fit else "", "model": model}


# ---------- Threads (the brand's own account) ----------
THREADS = "https://graph.threads.net/v1.0"


GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
_jwks = {"at": 0, "keys": {}}
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _github_keys():
    if time.time() - _jwks["at"] > 3600 or not _jwks["keys"]:
        with urllib.request.urlopen(GITHUB_ISSUER + "/.well-known/jwks", timeout=15) as r:
            _jwks["keys"] = {k["kid"]: k for k in json.loads(r.read()).get("keys", []) if k.get("kty") == "RSA"}
        _jwks["at"] = time.time()
    return _jwks["keys"]


def verify_rs256(token, keys):
    """Claims of a JWT signed with RS256 by one of the given keys, or None. Standard library only."""
    try:
        head_b, body_b, sig_b = token.split(".")
        head = json.loads(_b64(head_b))
        key = keys.get(head.get("kid"))
        if head.get("alg") != "RS256" or not key:
            return None
        n, e = int.from_bytes(_b64(key["n"]), "big"), int.from_bytes(_b64(key["e"]), "big")
        size = (n.bit_length() + 7) // 8
        got = pow(int.from_bytes(_b64(sig_b), "big"), e, n).to_bytes(size, "big")
        digest = hashlib.sha256(f"{head_b}.{body_b}".encode()).digest()
        want = b"\x00\x01" + b"\xff" * (size - len(_SHA256_PREFIX) - len(digest) - 3) + b"\x00" + _SHA256_PREFIX + digest
        return json.loads(_b64(body_b)) if hmac.compare_digest(got, want) else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def from_our_workflow(token):
    """True when the token was issued by GitHub to a workflow of our own repository."""
    if not token:
        return False
    try:
        claims = verify_rs256(str(token), _github_keys())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False
    repo = os.environ.get("KORIN_REPO", "e0094432-maker/target")
    return bool(claims) and claims.get("iss") == GITHUB_ISSUER and claims.get("aud") == "korin-api" and claims.get("repository") == repo and float(claims.get("exp", 0)) > time.time()


def _admin(body):
    """Posting goes to our own account: the page sends the owner's code, the daily job sends GitHub's signed token."""
    key = os.environ.get("KORIN_ADMIN_KEY", "")
    if not (key and hmac.compare_digest(str(body.get("key", "")), key)) and not from_our_workflow(body.get("oidc")):
        raise BadRequest("Неверный код владельца.")
    brand = BRANDS.get(str(body.get("brand", "korin")), BRANDS["korin"])
    token = "".join(os.environ.get(brand["env"], "").split())
    if not token:
        raise BadRequest(f"Threads для «{brand['name']}» ещё не подключён: на сервере нет токена.")
    return token


def _threads(method, path, params):
    data = urllib.parse.urlencode(params)
    url = f"{THREADS}{path}" + ("?" + data if method == "GET" else "")
    req = urllib.request.Request(url, data=data.encode() if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        print(f"threads {method} {path} failed: HTTP {e.code}: {detail}", flush=True)
        try:
            msg = json.loads(detail).get("error", {}).get("message", "")
        except json.JSONDecodeError:
            msg = ""
        raise BadRequest(f"Threads отказал: {msg or 'код ' + str(e.code)}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise BadRequest(f"Threads не ответил: {e}") from None


def threads_post(body):
    """Publish a text post, or a reply when reply_to is given. One call — one post, started by a person."""
    token = _admin(body)
    text = str(body.get("text", "")).strip()[:500]
    if len(text) < 3:
        raise BadRequest("Пустой текст.")
    params = {"media_type": "TEXT", "text": text, "access_token": token}
    reply_to = re.sub(r"\D", "", str(body.get("reply_to", "")))[:30]
    if reply_to:
        params["reply_to_id"] = reply_to
    made = _threads("POST", "/me/threads", params)
    done = _threads("POST", "/me/threads_publish", {"creation_id": made.get("id", ""), "access_token": token})
    link = _threads("GET", f"/{done.get('id', '')}", {"fields": "permalink", "access_token": token}).get("permalink", "") if done.get("id") else ""
    return {"ok": True, "id": done.get("id", ""), "link": link}


BRANDS = {
    "korin": {
        "name": "Körin", "env": "THREADS_TOKEN", "site": "kenesai.asia/korin",
        "about": "Körin проверяет, называет ли нейросеть (ChatGPT, Gemini, Perplexity) компанию, когда клиент спрашивает совета, и говорит, что сделать, чтобы называла. Проверка бесплатная.",
        "last": "Проверить бесплатно: kenesai.asia/korin",
        "topics": [
            "люди теперь спрашивают нейросеть, куда пойти, а не листают поиск",
            "как за минуту проверить, называет ли нейросеть ваш бизнес",
            "почему нейросеть называет конкурента: у него на сайте написаны цены",
            "отзывы в 2ГИС и на картах: нейросеть их читает",
            "сайт может быть закрыт для нейросети, а хозяин об этом не знает",
            "нейросеть называет два-три места, остальных для клиента нет",
            "страница с ответами на частые вопросы клиентов помогает попасть в ответ",
            "длинные точные запросы: клиент пишет нейросети целую фразу",
            "почему только Instagram без сайта мешает нейросети вас найти",
            "название, адрес и телефон должны совпадать на всех площадках",
            "вопрос к читателям: назовёт ли нейросеть ваш бизнес, если спросить про вашу нишу",
            "три дела на эту неделю, чтобы нейросеть узнала о вашем бизнесе",
            "казахский язык: на нём нейросеть тоже отвечает, а конкурентов меньше",
            "миф: в ответы нейросети нельзя попасть без больших денег",
            "что написать в описании профиля, чтобы нейросеть поняла, чем вы заняты",
            "нейросеть берёт ответы с сайтов: с каких именно",
        ],
    },
    "kenes": {
        "name": "Kenes AI", "env": "THREADS_TOKEN_KENES", "site": "kenesai.asia",
        "about": "Kenes AI — агентство из Казахстана. Запускает для бизнеса рекламу в ChatGPT: всё делает само, от текста и картинки до настройки, а владелец получает заявки.",
        "last": "Запустим за вас: kenesai.asia",
        "topics": [
            "в ChatGPT теперь можно показывать рекламу своего бизнеса, и в Казахстане тоже",
            "чем реклама в нейросети отличается от таргета: человек сам пришёл за советом",
            "кому подходит реклама в нейросети: курсы, мебель, рестораны, услуги",
            "таргет дорожает: что ещё попробовать малому бизнесу",
            "в рекламе нейросетей пока мало конкурентов: кто придёт первым",
            "вместо ключевых слов там подсказки: вы описываете, кому показывать",
            "как выглядит объявление рядом с ответом нейросети",
            "частая ошибка: объявление не должно притворяться ответом нейросети, такое отклоняют",
            "что нужно для запуска: сайт, короткий текст и одна картинка",
            "можно начать с маленького бюджета и посмотреть, идут ли люди",
            "показали рекламу и зашли на сайт — это разные вещи: в чём разница",
            "почему владельцу не надо разбираться в кабинете самому",
            "вопрос к читателям: где вы сейчас берёте клиентов",
            "с рекламы человек приходит на сайт: что должно быть на первой странице",
            "миф: реклама в нейросетях только для больших компаний",
        ],
    },
}
FORMS = ["вопрос к читателю", "простой совет", "наблюдение", "разбор мифа", "три шага", "маленький опыт для читателя"]


HOOKS = [
    "начни с короткой истории из жизни владельца бизнеса: три предложения и вывод",
    "начни с неожиданного наблюдения, с которым хочется поспорить",
    "начни фразой «Мало кто замечает, но…» и объясни одну простую вещь",
    "задай один прямой вопрос читателю и дай свой короткий ответ",
    "дай список из трёх коротких пунктов, каждый с новой строки",
    "сравни «раньше» и «теперь» в двух коротких строках",
    "начни с ошибки, которую делают почти все, и скажи, как проще",
]
ALMATY = 5 * 3600  # Kazakhstan is UTC+5 all year, no summer time
SLOT_TIMES = [10 * 60 + 30, 14 * 60, 19 * 60]  # minutes after midnight in Almaty: morning, midday, evening
OPEN, CLOSE = 9 * 60 + 30, 21 * 60 + 45  # auto posts start only in this window, so one with pictures is out before 22:00


def _stamp(raw):
    """Threads time like "2026-11-22T20:49:36+0000" (also with Z or +00:00) as Unix seconds; None if unreadable."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?$", str(raw).strip())
    if not m:
        return None
    zone = (m[2] or "Z").replace("Z", "+0000").replace(":", "")  # no zone means UTC
    try:
        return datetime.datetime.strptime(m[1] + zone, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:  # a date that does not exist, like month 13
        return None


def _auto_guard(token):
    """For scheduled runs: how many posts are out today, and why to post nothing now (None when a post is due).
    GitHub runs come hours late, twice or not at all, so the account itself says what is left to post today.
    A skip with "alarm" needs a person (the account cannot be read), so the run fails instead of staying green."""
    now = time.time()
    day, minute = int((now + ALMATY) // 86400), int((now + ALMATY) % 86400 // 60)
    clock = f"{minute // 60}:{minute % 60:02d}"
    if minute < OPEN or minute >= CLOSE:
        return 0, {"skip": f"в Алматы {clock}: новые посты начинаем с 9:30 до 21:45, чтобы ничего не вышло ночью"}
    try:
        posts = _threads("GET", "/me/threads", {"fields": "id,timestamp,media_type", "limit": "10", "access_token": token}).get("data") or []
    except BadRequest as e:
        return 0, {"skip": f"не удалось прочитать свои посты, вслепую не публикуем: {e}", "alarm": True}
    posts = [p if isinstance(p, dict) else {} for p in posts]
    stamps = [_stamp(p.get("timestamp")) for p in posts if p.get("media_type") != "REPOST_FACADE"]  # our reposts of others are listed too
    if None in stamps:
        return 0, {"skip": "не понятно время своих постов, вслепую не публикуем", "alarm": True}
    # only posts inside today's window take a slot: a stray post after midnight does not use up the morning carousel
    posted = sum(int((s + ALMATY) // 86400) == day and (s + ALMATY) % 86400 >= OPEN * 60 for s in stamps)
    due = sum(minute >= t for t in SLOT_TIMES)
    if posted >= due:
        return posted, {"skip": f"уже вышло {posted} из {due} на сегодня" if due else f"в Алматы {clock}, первый пост в 10:30"}
    if stamps and now - max(stamps) < 150 * 60:
        return posted, {"skip": f"прошлый пост вышел {int((now - max(stamps)) // 60)} мин назад, между постами нужно хотя бы 2,5 часа"}
    return posted, None


def marketer_plan(body):
    """Today's post for a brand and a time slot. Morning: a carousel. Midday: short text. Evening: a question or one picture.
    With "auto" the slot is chosen by what is already posted today; with an explicit slot a person forces a post."""
    token = _admin(body)
    key = str(body.get("brand", "korin")) if str(body.get("brand", "korin")) in BRANDS else "korin"
    brand = BRANDS[key]
    day = int((time.time() + ALMATY) // 86400)  # the Almaty day, so the three posts of one day get three topics
    extra = {}
    if body.get("auto"):
        posted, skip = _auto_guard(token)
        if skip:
            print(f"marketer {key}: skipped: {skip['skip']}", flush=True)
            return {"ok": False, "brand": key, **skip}
        slot, extra = min(posted, 2), {"posted_today": posted}
    else:
        slot = int(body.get("slot", 0)) % 3 if str(body.get("slot", 0)).isdigit() else 0
    n = day * 3 + slot
    topic = brand["topics"][n % len(brand["topics"])]
    fmt = "carousel" if slot == 0 else "text" if slot == 1 else ("image" if day % 2 else "text")
    who = f"Ты ведёшь Threads «{brand['name']}». {brand['about']} Сайт: {brand['site']}. Работает в Казахстане.\n\nЧитатели: владельцы малого бизнеса в Казахстане. Тема: {topic}."
    rules = "Правила: начни с одной конкретной боли владельца бизнеса, которую он узнает с первой строки (заявок стало меньше, реклама дорожает, клиент ушёл к соседу, непонятно, куда уходят деньги), а не с общих слов про рынок; очень простые слова и короткие предложения, как будто пишешь другу; первая строка такая, чтобы захотелось читать дальше; без слов «уникальный», «инновационный», «под ключ», «эффективный»; не обещай результат; не выдумывай клиентов, цифры, проценты и отзывы; пиши как человек, а не как реклама."
    tags_rule = '"tags": ["от 2 до 4 хэштегов без решётки, одним словом, по теме"]'
    if fmt == "text":
        prompt = f"""{who} Подача: {HOOKS[n % len(HOOKS)]}.

Напиши короткий текстовый пост без картинки. Такой, чтобы на него хотелось ответить или переслать. {rules} Бренд и сайт не упоминай вообще, если это не просится само: такие посты читают охотнее.
Верни ТОЛЬКО JSON: {{"text": "пост до 280 знаков, можно с переносами строк, в конце вопрос к читателю", {tags_rule}}}"""
    else:
        form = FORMS[n % len(FORMS)]
        prompt = f"""{who} Подача: {form}.

Сделай пост {'-карусель из двух картинок, который хочется долистать и сохранить' if fmt == 'carousel' else 'с одной картинкой: на ней одна крупная фраза'}. {rules}
Верни ТОЛЬКО JSON:
{{"text": "подпись до 330 знаков: цепляющая первая строка; интересный факт или наблюдение (без выдуманных чисел); одна строка с пользой; в конце вопрос к читателю или строка «{brand['last']}»",
 {tags_rule},
 "slides": [
  {{"tag": "одно слово-рубрика", "title": "крупная фраза для картинки, до 45 знаков, без точки"}}{', {"tag": "одно слово", "title": "заголовок второй картинки, до 30 знаков", "points": ["пункт до 55 знаков", "пункт до 55 знаков", "пункт до 55 знаков"]}' if fmt == 'carousel' else ''}
 ]}}"""
    text, _, model = write(prompt, "Ты маркетолог, пишешь по-русски живо и просто. Отвечаешь только валидным JSON.", kind="own")
    obj = parse_json(text) or {}
    slides = [{"tag": str(x.get("tag", ""))[:18], "title": str(x.get("title", ""))[:70], "points": [str(q)[:80] for q in x.get("points", []) if str(q).strip()][:3]} for x in obj.get("slides", []) if isinstance(x, dict) and x.get("title")][:2 if fmt == "carousel" else 1]
    post = str(obj.get("text", "")).strip()[:400]
    tags = [re.sub(r"[^\wа-яёА-ЯЁ]", "", str(t)) for t in obj.get("tags", []) if str(t).strip()][:4]
    tags = [t for t in tags if 2 < len(t) < 30] or ["малыйбизнес", "Казахстан"]
    if not post or (fmt != "text" and not slides):
        raise providers.ProviderError("нейросеть вернула пост в неверном виде")
    post = (post + "\n\n" + " ".join("#" + t for t in tags))[:500]
    return {"ok": True, "brand": key, "format": fmt, "single": fmt == "image", "topic": topic, "text": post, "slides": slides if fmt != "text" else [], "theme": n % 3, "model": model, "slot": slot, **extra}


def marketer_publish(body):
    """Publish the post with its pictures: one picture as a photo, several as a carousel."""
    token = _admin(body)
    text = str(body.get("text", "")).strip()[:500]
    images = [str(u).strip() for u in body.get("images", []) if re.match(r"https://kenesai\.asia/korin/media/[\w.-]+\.png$", str(u).strip())][:6]
    if not text:
        raise BadRequest("Нужен текст поста.")
    if not images:
        made = _threads("POST", "/me/threads", {"media_type": "TEXT", "text": text, "access_token": token})
    elif len(images) == 1:
        made = _threads("POST", "/me/threads", {"media_type": "IMAGE", "image_url": images[0], "text": text, "access_token": token})
    else:
        kids = [_threads("POST", "/me/threads", {"media_type": "IMAGE", "image_url": u, "is_carousel_item": "true", "access_token": token}).get("id", "") for u in images]
        time.sleep(4)
        made = _threads("POST", "/me/threads", {"media_type": "CAROUSEL", "children": ",".join(kids), "text": text, "access_token": token})
    time.sleep(6 if images else 1)  # Threads asks to wait before publishing so it can fetch the pictures
    done = _threads("POST", "/me/threads_publish", {"creation_id": made.get("id", ""), "access_token": token})
    link = _threads("GET", f"/{done.get('id', '')}", {"fields": "permalink", "access_token": token}).get("permalink", "") if done.get("id") else ""
    print(f"marketer published: {link} ({len(images)} pictures)", flush=True)
    return {"ok": True, "link": link}


_declined = {}  # comment id -> when the model said not to answer it; that comment is not sent to the model again
DECLINED_KEEP = 5000  # bounded; in memory, so a restart or a sleep of the free server forgets it
DECLINED_CARRY = 300  # the newest ids handed to and taken back from replies.yml, which keeps them between runs


def _decline(comment_id):
    with _wlock:
        _declined.pop(comment_id, None)
        _declined[comment_id] = time.time()
        while len(_declined) > DECLINED_KEEP:
            _declined.pop(next(iter(_declined)))


def _too_old(comment):
    """Opt-in (KORIN_REPLY_MAX_AGE_HOURS): an old comment is not worth a model call. Unreadable time counts as new."""
    try:
        hours = float(os.environ.get("KORIN_REPLY_MAX_AGE_HOURS", "") or 0)
    except ValueError:
        hours = 0
    when = _stamp(comment.get("timestamp")) if hours > 0 else None
    return when is not None and when < time.time() - hours * 3600


def marketer_replies(body):
    """Answer new comments under our own recent posts. People who wrote to us get a reply; nobody else is touched."""
    token = _admin(body)
    key = str(body.get("brand", "korin")) if str(body.get("brand", "korin")) in BRANDS else "korin"
    brand = BRANDS[key]
    # the free server sleeps between runs and forgets: the caller may bring back the list it was given last time
    carried = body.get("declined") if isinstance(body.get("declined"), list) else []
    for cid in carried[-DECLINED_CARRY:]:
        if isinstance(cid, str) and re.fullmatch(r"[\w-]{1,40}", cid) and cid not in _declined:
            _decline(cid)
    me = _threads("GET", "/me", {"fields": "username", "access_token": token}).get("username", "")
    posts = _threads("GET", "/me/threads", {"fields": "id,text,timestamp", "limit": "8", "access_token": token}).get("data", [])
    done, skipped, limit = [], 0, 4
    for post in posts:
        if len(done) >= limit:
            break
        talk = _threads("GET", f"/{post.get('id', '')}/conversation", {"fields": "id,text,username,timestamp,replied_to", "reverse": "false", "access_token": token}).get("data", [])
        answered = {(x.get("replied_to") or {}).get("id") for x in talk if x.get("username") == me}
        by_id = {x.get("id"): x for x in talk}
        told = {}  # how many times we have already answered each person under this post
        for x in talk:
            if x.get("username") == me:
                who = by_id.get((x.get("replied_to") or {}).get("id"), {}).get("username")
                told[who] = told.get(who, 0) + 1
        for c in talk:
            if len(done) >= limit:
                break
            text = re.sub(r"\s+", " ", str(c.get("text", ""))).strip()
            # only comments written to the post itself or to us, by someone else, not answered yet
            if c.get("username") == me or c.get("id") in answered or len(text) < 4 or told.get(c.get("username"), 0) >= 2:
                continue
            parent = (c.get("replied_to") or {}).get("id")
            if parent and parent != post.get("id") and parent not in {x.get("id") for x in talk if x.get("username") == me}:
                continue
            if c.get("id") in _declined:  # the model already said no: asking every half hour changes nothing
                skipped += 1
                answered.add(c.get("id"))
                continue
            if _too_old(c):
                continue
            prompt = f"""Ты ведёшь Threads «{brand['name']}». {brand['about']}

Наш пост: «{str(post.get('text', ''))[:500]}»
Комментарий от @{c.get('username', '')}: «{text[:600]}»

Реши, отвечать ли. Не отвечай, если комментарий — оскорбление, спам, реклама чужого, политика, религия, личная беда или просто набор символов.
Если отвечать: сначала согласись с тем, в чём человек прав, потом одной фразой спокойно поверни к нашей мысли. Если он задал вопрос — ответь прямо. Пиши как живой человек: просто, коротко, с уважением, можно с лёгкой иронией, но без колкостей и без спора. Без ссылок, без хэштегов, без обещаний и выдуманных цифр. Не говори, что мы можем «научить» или «заставить» нейросеть называть компанию: мы только проверяем и подсказываем, что сделать. Не обращайся через @. До 230 знаков.
Верни ТОЛЬКО JSON: {{"answer": true, "reply": "текст ответа"}} или {{"answer": false, "reply": ""}}"""
            obj = parse_json(write(prompt, "Ты вежливый и остроумный представитель бренда. Отвечаешь только валидным JSON на русском языке.", kind="own")[0]) or {}
            reply = str(obj.get("reply", "")).strip()[:300]
            if not obj.get("answer") or len(reply) < 3:
                skipped += 1
                answered.add(c.get("id"))
                if obj and not obj.get("answer") and c.get("id"):  # a clear "no"; an unreadable answer is asked again next time
                    _decline(c.get("id"))
                continue
            made = _threads("POST", "/me/threads", {"media_type": "TEXT", "text": reply, "reply_to_id": c.get("id", ""), "access_token": token})
            time.sleep(2)
            _threads("POST", "/me/threads_publish", {"creation_id": made.get("id", ""), "access_token": token})
            answered.add(c.get("id"))
            told[c.get("username")] = told.get(c.get("username"), 0) + 1
            done.append({"to": c.get("username", ""), "comment": text[:140], "reply": reply})
            print(f"marketer replied to @{c.get('username', '')} ({key}): {reply[:120]}", flush=True)
    out = {"ok": True, "brand": key, "replied": done, "skipped": skipped}
    if "declined" in body:  # only for a caller that keeps the list; the old answer stays as it was for everyone else
        with _wlock:
            out["declined"] = list(_declined)[-DECLINED_CARRY:]
    return out


def threads_search(body):
    """Popular threads by a keyword, for a person to choose which to answer."""
    token = _admin(body)
    q = re.sub(r"\s+", " ", str(body.get("q", ""))).strip()[:60]
    if len(q) < 2:
        raise BadRequest("Напишите слово для поиска.")
    data = _threads("GET", "/keyword_search", {"q": q, "search_type": "TOP", "fields": "id,text,username,permalink,timestamp", "access_token": token})
    found = [{"id": str(x.get("id", "")), "text": str(x.get("text", ""))[:600], "user": str(x.get("username", ""))[:40], "link": str(x.get("permalink", ""))[:200], "time": str(x.get("timestamp", ""))[:25]} for x in data.get("data", []) if x.get("text")][:15]
    return {"found": found}


def threads_me(body):
    token = _admin(body)
    me = _threads("GET", "/me", {"fields": "id,username", "access_token": token})
    return {"username": me.get("username", "")}


# ---------- agents ----------
AGENTS = {
    "why": "Ты аналитик видимости в нейросетях. По данным кабинета объясни простым языком, почему компанию называют реже конкурентов, и дай три первых шага. Не выдумывай фактов о компании.",
    "faq": "Ты редактор сайта. Напиши для компании страницу «Вопросы и ответы» из 6 вопросов: вопрос в заголовке, полный ответ в первом предложении. Там, где нужны факты, которых нет в данных, поставь [что вписать].",
    "review": "Ты менеджер по отзывам. Напиши два коротких сообщения клиенту с просьбой оставить отзыв на площадке из данных: для WhatsApp и для Instagram. Без давления и без обещаний скидок за отзыв.",
    "bio": "Ты SMM-специалист. Предложи три варианта описания профиля Instagram до 150 знаков: ниша и город словами, одно отличие, способ связи. Неизвестное отметь как [что вписать].",
    "compare": "Ты маркетолог. Составь план страницы сравнения компании с конкурентом-лидером из данных: только проверяемые критерии, без оценок конкурента. Факты, которых нет, отметь как [что вписать].",
    "pitch": "Ты специалист по связям с изданиями. Напиши короткое вежливое письмо автору подборки или каталога с предложением добавить компанию. Факты, которых нет в данных, отметь как [что вписать]. Без преувеличений.",
    "chat": "Ты ассистент Körin для владельца бизнеса и маркетолога в Казахстане. Помогаешь сделать так, чтобы нейросети чаще называли компанию: тексты, письма, планы, разбор сайта и отзывов. Опирайся на данные кабинета и прочитанные страницы. Не выдумывай фактов о компании: чего нет в данных, отметь как [что вписать].",
    "narrow": "Ты специалист по поисковому спросу в Казахстане. Люди задают нейросетям длинные точные вопросы. Придумай для компании 10 узких запросов из 5–9 слов: с размером, ценой, районом, сроком или случаем использования, на русском и два на казахском. Только то, что компания из данных реально может продавать. Затем для самого сильного запроса напиши план страницы: заголовок, первый абзац с прямым ответом, 5 подзаголовков. Факты, которых нет в данных, отметь как [что вписать].",
    "post": "Ты маркетолог компании и ведёшь её Threads. Напиши один пост для владельцев малого бизнеса в Казахстане: цепляющая первая строка, интересное наблюдение или факт без выдуманных цифр, одна польза для читателя, в конце вопрос. До 400 знаков, очень простыми словами. Потом с новой строки 3–5 хэштегов по теме. Ничего не выдумывай о компании.",
    "plan": "Ты руководитель проекта. Составь план работ на 4 недели, чтобы нейросети начали чаще называть компанию: по неделям, с ответственным и результатом. Опирайся на данные кабинета.",
}


def run_agent(body):
    """An agent first reads the real pages from the connectors, then writes. Every step is reported."""
    task = body.get("task")
    if task not in AGENTS:
        raise BadRequest("Неизвестный агент.")
    ctx = body.get("context") if isinstance(body.get("context"), dict) else {}
    links = ctx.get("links") if isinstance(ctx.get("links"), dict) else {}
    steps, material = [], []
    for name, raw in list(links.items())[:4]:
        try:
            page = read_page(str(raw)[:300])
        except BadRequest as e:
            steps.append({"text": f"{str(name)[:30]}: {e}", "ok": False})
            continue
        got = page["status"] == 200 and len(page["text"]) >= 200
        steps.append({"text": f"{str(name)[:30]}: прочитал {len(page['text'])} знаков" if got else f"{str(name)[:30]}: страница не отдала текст (код {page['status'] or 'нет ответа'})", "ok": got})
        if got:
            material.append(f"Страница «{str(name)[:30]}» ({page['url']}), заголовок: {page['title']}\n{page['text'][:2500]}")
    if ctx.get("not_named_on"):
        steps.append({"text": f"Взял из замера вопросы, где компанию не назвали: {len(ctx['not_named_on'])}", "ok": True})
    context = json.dumps({k: v for k, v in ctx.items() if k != "links"}, ensure_ascii=False)[:4000]
    question = str(body.get("question", ""))[:1000]
    pages = "\n\n".join(material)[:9000]
    prompt = f"Данные кабинета (JSON):\n{context}\n\n{'Прочитанные страницы:' + chr(10) + pages + chr(10) + chr(10) if pages else ''}Запрос пользователя: {question or 'выполни свою задачу'}\n\nОтвечай по-русски, коротко и по делу."
    text, _, model = write(prompt, AGENTS[task], kind="client")
    steps.append({"text": f"Текст написала нейросеть {model}", "ok": True})
    return {"text": text, "model": model, "steps": steps}


# ---------- HTTP ----------
_hits = {}


def allowed(ip, limit=30, window=3600):
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        _hits[ip] = hits
        return False
    _hits[ip] = hits + [now]
    return True


ROUTES = {"/api/site-audit": lambda b: audit_site(b.get("url")), "/api/check": run_check, "/api/agent": run_agent, "/api/connect": connect, "/api/quick": quick_check, "/api/suggest": suggest, "/api/check-one": check_one, "/api/analyze": analyze, "/api/social-plan": social_plan, "/api/social-reply": social_reply, "/api/threads-post": threads_post, "/api/threads-search": threads_search, "/api/threads-me": threads_me, "/api/marketer-plan": marketer_plan, "/api/marketer-publish": marketer_publish, "/api/marketer-replies": marketer_replies}


class Handler(BaseHTTPRequestHandler):
    server_version = "Korin/0.1"

    def _send(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        origin = self.headers.get("Origin", "")
        if origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        if self.path == "/api/health":
            return self._send(200, {"ok": True, "ai": bool(provider_name()), "provider": provider_name(), "threads": bool(os.environ.get("THREADS_TOKEN") and os.environ.get("KORIN_ADMIN_KEY")), "marketer": [k for k, v in BRANDS.items() if os.environ.get(v["env"])],
                                    "writers": {"own": writer_chain("own"), "client": writer_chain("client")}, "gemini_model": _ai.get("model")})
        self._send(404, {"error": "Не найдено."})

    def do_POST(self):
        route = ROUTES.get(self.path)
        if not route:
            return self._send(404, {"error": "Не найдено."})
        if not allowed((self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()):
            return self._send(429, {"error": "Слишком много запросов. Попробуйте через час."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 20_000:
                raise BadRequest("Слишком большой запрос.")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise BadRequest("Ожидается объект JSON.")
            self._send(200, route(body))
        except BadRequest as e:
            self._send(400, {"error": str(e)})
        except json.JSONDecodeError:
            self._send(400, {"error": "Запрос не в формате JSON."})
        except Exception as e:  # never drop the connection: the page must get an answer it can show
            if not isinstance(e, providers.ProviderError):
                print(f"unexpected error on {self.path}: {type(e).__name__}: {e}", flush=True)
                return self._send(500, {"error": "На сервере ошибка. Мы её уже видим в журнале."})
            print(f"provider error on {self.path}: {e}", flush=True)
            msg = "ИИ не подключён: на сервере нет ключа или закончился баланс." if no_ai(e) else "Бесплатный лимит нейросети исчерпан. Подождите минуту и попробуйте снова; если не помогает — лимит на сегодня закончился." if out_of_quota(e) else "Нейросеть не ответила. Попробуйте позже."
            self._send(503, {"error": msg})

    def log_message(self, fmt, *args):
        pass


def main():
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
