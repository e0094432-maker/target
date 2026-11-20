"""Körin backend: real site audit, real AI-visibility check and AI agents.

Standard library only. Run:  python3 -m server.app   (PORT, GEMINI_API_KEY or OPENAI_API_KEY, KORIN_ORIGINS)
"""
import ipaddress
import json
import os
import re
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


def ask(prompt, system=None):
    name = provider_name()
    if not name:
        raise providers.ProviderError("не задан ключ нейросети")
    if name != "gemini":
        return providers.openai(prompt, system)
    try:
        return providers.gemini(prompt, system)
    except providers.ProviderError as e:
        if no_ai(e):
            raise
        # some keys cannot use search grounding: answer from the model alone rather than fail
        print(f"gemini with search failed, retrying without: {e}", flush=True)
        return providers.gemini(prompt, system, search=False)


def no_ai(error):
    return any(k in str(error) for k in ("не задан", "insufficient_quota", "RESOURCE_EXHAUSTED"))


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
    grid, answers, sources, errors, used = [], [], {}, 0, None
    for q in questions:
        try:
            text, urls, model = ask(q, system)
        except providers.ProviderError as e:
            if no_ai(e):
                raise
            errors += 1
            grid.append(None)
            answers.append(None)
            continue
        mentioned = {m["company_id"] for m in detect.find_mentions(text, companies)}
        grid.append([i in mentioned for i in range(len(names))])
        answers.append(text[:4000])
        used = model
        for c in detect.find_citations(text, urls):
            sources[c["domain"]] = sources.get(c["domain"], 0) + 1
    top = sorted(sources.items(), key=lambda kv: -kv[1])[:8]
    return {"names": names, "questions": questions, "grid": grid, "answers": answers, "sources": top, "errors": errors, "model": used}


# ---------- free check on the landing page ----------
# The model's answer depends on the question and the city, not on who asks.
# So one paid call serves every company in the same niche for three days.
QUICK_TTL = 72 * 3600
QUICK_PER_DAY = int(os.environ.get("KORIN_QUICK_PER_DAY", "200"))  # real model calls a day, cache hits are free
_quick = {"day": "", "n": 0}
_qcache, _qlocks, _qguard = {}, {}, threading.Lock()


def quick_questions(niche, city):
    return [f"{niche} в городе {city}: кого посоветуешь?", f"{niche} с хорошими отзывами, {city} — назови конкретные компании", f"{city} қаласында {niche}: қай компанияны ұсынасың?"]


def cached_answer(question, city):
    """(text, urls, model, from_cache). Two visitors asking the same thing at once share one call."""
    key = (question.lower(), city.lower())
    with _qguard:
        lock = _qlocks.setdefault(key, threading.Lock())
    with lock:
        hit = _qcache.get(key)
        if hit and hit[0] > time.time():
            return (*hit[1:], True)
        today = time.strftime("%Y-%m-%d")
        with _qguard:
            if _quick["day"] != today:
                _quick.update(day=today, n=0)
            if _quick["n"] >= QUICK_PER_DAY:
                raise BadRequest("На сегодня бесплатные проверки закончились. Оставьте заявку, и мы сделаем замер вручную.")
            _quick["n"] += 1
        text, urls, model = ask(question, f"Пользователь находится в городе {city}, Казахстан. Называй конкретные компании.")
        with _qguard:
            if len(_qcache) > 3000:
                now = time.time()
                for k in [k for k, v in _qcache.items() if v[0] <= now] or list(_qcache)[:500]:
                    _qcache.pop(k, None)
                    _qlocks.pop(k, None)
            _qcache[key] = (time.time() + QUICK_TTL, text, urls, model)
        return text, urls, model, False


def quick_check(body):
    """Real questions about the visitor's niche, answers word for word. With "i" — one question, so the page can show cards as they come."""
    company, niche, city = (re.sub(r"\s+", " ", str(body.get(k, ""))).strip()[:60] for k in ("company", "niche", "city"))
    if not company or not niche or not city:
        raise BadRequest("Нужны компания, ниша и город.")
    questions = quick_questions(niche, city)
    me = [{"id": 0, "name": company, "aliases": []}]

    def one(q):
        text, urls, model, cached = cached_answer(q, city)
        return {"question": q, "answer": text[:3000], "named": bool(detect.find_mentions(text, me)), "sources": sorted({c["domain"] for c in detect.find_citations(text, urls)})[:6], "model": model, "cached": cached}

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
    text, _, model = ask(prompt, AGENTS[task])
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


ROUTES = {"/api/site-audit": lambda b: audit_site(b.get("url")), "/api/check": run_check, "/api/agent": run_agent, "/api/connect": connect, "/api/quick": quick_check, "/api/suggest": suggest}


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
            return self._send(200, {"ok": True, "ai": bool(provider_name()), "provider": provider_name()})
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
            msg = "ИИ не подключён: на сервере нет ключа или закончился баланс." if no_ai(e) else "Нейросеть не ответила. Попробуйте позже."
            self._send(503, {"error": msg})

    def log_message(self, fmt, *args):
        pass


def main():
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
