"""AI providers. Each returns (answer_text, cited_urls, model_name).

Plain HTTPS calls, no SDKs. Model names change often: override them with
OPENAI_MODEL / GEMINI_MODEL / PERPLEXITY_MODEL instead of editing code.
"""
import hashlib
import json
import re
import os
import random
import time
import urllib.error
import urllib.request


class ProviderError(Exception):
    pass


class Answer(tuple):
    """(text, urls, model) as before, so `text, urls, model = ...` keeps working, plus how it was made:
    .engine ("gemini", "openai", ...) and .search — True only when the engine really searched the web
    for this answer. Without a search the text is the model's memory, not what a person sees in the app."""

    def __new__(cls, text, urls, model, engine="", search=False):
        self = super().__new__(cls, (text, urls, model))
        self.engine, self.search = engine, bool(search)
        return self

    def __getnewargs__(self):  # copy, deepcopy and pickle call __new__ with these
        return (*self, self.engine, self.search)


def _post(url, headers, payload, timeout=180, retries=3):
    body = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:1500]
            # an empty balance also comes back as 429; waiting will not help
            if e.code in (429, 500, 502, 503, 504) and attempt < retries and "insufficient_quota" not in detail:
                time.sleep(2 ** attempt * 2)
                continue
            raise ProviderError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise ProviderError(f"network: {e}") from None


def _key(name):
    # keys pasted into a dashboard often carry a space, a line break or quotes
    value = "".join((os.environ.get(name) or "").split()).strip("\"'")
    if not value:
        raise ProviderError(f"не задан {name}")
    if not value.isascii() or not value.isprintable():
        raise ProviderError(f"не задан {name}: в ключе посторонние символы, вставьте его заново")
    return value


def _messages(system, prompt):
    return ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]


def openai(prompt, system=None):
    model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    data = _post(
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": "Bearer " + _key("OPENAI_API_KEY")},
        {"model": model, "messages": _messages(system, prompt)},
    )
    msg = data["choices"][0]["message"]
    urls = [a["url_citation"]["url"] for a in msg.get("annotations") or [] if a.get("type") == "url_citation"]
    # chat/completions with gpt-4o does not search the web; only links it cites prove a search happened
    return Answer(msg.get("content") or "", urls, data.get("model", model), engine="openai", search=bool(urls))


def perplexity(prompt, system=None):
    model = os.environ.get("PERPLEXITY_MODEL", "sonar")
    data = _post(
        "https://api.perplexity.ai/chat/completions",
        {"Authorization": "Bearer " + _key("PERPLEXITY_API_KEY")},
        {"model": model, "messages": _messages(system, prompt)},
    )
    urls = list(data.get("citations") or [])
    urls += [r["url"] for r in data.get("search_results") or [] if r.get("url") and r["url"] not in urls]
    return Answer(data["choices"][0]["message"].get("content") or "", urls, data.get("model", model), engine="perplexity", search=bool(urls))


def undouble(text):
    """The same answer given twice in a row (whole text, or the same run of paragraphs) comes back once."""
    s = str(text or "")
    t = s.strip()
    n = len(t)
    if n >= 40:
        for gap in range(0, 5):
            if (n - gap) % 2:
                continue
            h = (n - gap) // 2
            if t[:h] == t[n - h:] and not t[h:n - h].strip():
                return t[:h].strip()
    paras = [x.strip() for x in re.split(r"\n\s*\n", t)]
    if n >= 40 and len(paras) >= 2 and len(paras) % 2 == 0 and paras[: len(paras) // 2] == paras[len(paras) // 2:]:
        return "\n\n".join(paras[: len(paras) // 2])
    out = []
    for x in paras:
        if out and len(x) >= 30 and x == out[-1]:
            continue
        out.append(x)
    return "\n\n".join(out) if len(out) < len(paras) else s


def gemini(prompt, system=None, search=None, model=None, retries=3, timeout=180):
    # gemini-2.5-flash is reported to be switched off for this API on 2026-12-01; Google's moving flash-lite name
    # follows the cheap line the server prefers. The server always passes its own model; this default is for the CLI.
    model = model or os.environ.get("GEMINI_MODEL", "").strip() or "gemini-flash-lite-latest"
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if os.environ.get("GEMINI_SEARCH", "1") != "0" if search is None else search:
        payload["tools"] = [{"google_search": {}}]
    data = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": _key("GEMINI_API_KEY")},
        payload,
        timeout=timeout,
        retries=retries,
    )
    cand = (data.get("candidates") or [{}])[0]
    # thought parts are the model's notes, not the answer; a part that repeats the text already given (seen with
    # grounded answers: the whole reply comes twice) is dropped, so the page never shows the same answer twice
    text = ""
    for p in cand.get("content", {}).get("parts", []):
        piece = p.get("text", "") if isinstance(p, dict) and not p.get("thought") else ""
        if len(piece.strip()) >= 20 and piece.strip() in text:
            continue
        text += piece
    text = undouble(text)
    grounding = cand.get("groundingMetadata") or {}
    chunks = grounding.get("groundingChunks") or []
    # with the google_search tool the model itself decides whether to search; it did only if Google says so
    searched = bool(payload.get("tools") and (grounding.get("webSearchQueries") or chunks))
    # search results come as redirect links; the real site name is in the title
    urls = []
    for c in chunks:
        web = c.get("web", {})
        title = (web.get("title") or "").strip().lower()
        if re.fullmatch(r"[a-z0-9а-яё.-]+\.[a-zа-яё]{2,}", title):
            urls.append("https://" + title)
        elif web.get("uri"):
            urls.append(web["uri"])
    return Answer(text, urls, data.get("modelVersion", model), engine="gemini", search=searched)


# ---------- writers: texts only, never a measurement ----------
# These are not the engines we measure, so they are not in get() below. Model names change often:
# the defaults are what was there when this was written, check them in each console.
_UA = "KorinServer/1.0"


def _content(data, where):
    try:
        return data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError(f"{where}: ответ в неверном виде") from None


def groq(prompt, system=None, timeout=45):
    """Groq, OpenAI-compatible chat completions. One try: the router moves on instead of retrying."""
    # Llama was reported gone from Groq's free plan in August 2026; gpt-oss-120b was listed as free. Check in console.groq.com
    model = os.environ.get("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"
    data = _post(
        "https://api.groq.com/openai/v1/chat/completions",
        {"Authorization": "Bearer " + _key("GROQ_API_KEY"), "User-Agent": _UA},
        {"model": model, "messages": _messages(system, prompt)},
        timeout=timeout,
        retries=0,
    )
    text = _content(data, "groq")
    if not text.strip():
        raise ProviderError("groq: пустой ответ")
    return Answer(text, [], data.get("model", model), engine="groq", search=False)


def cloudflare(prompt, system=None, timeout=60):
    """Cloudflare Workers AI REST: POST /accounts/{id}/ai/run/{model}. One try, like groq()."""
    account = "".join((os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").split()).strip("\"'")
    if not re.fullmatch(r"[\w-]{8,64}", account):
        raise ProviderError("не задан CLOUDFLARE_ACCOUNT_ID")
    model = os.environ.get("CLOUDFLARE_MODEL", "").strip() or "@cf/meta/llama-3.3-70b-instruct-fp8-fast"  # check in the Cloudflare dashboard
    if not re.fullmatch(r"@?[\w.-]+(/[\w.-]+)*", model):
        raise ProviderError("CLOUDFLARE_MODEL: неверное имя модели")
    try:
        max_tokens = max(64, min(8192, int(os.environ.get("CLOUDFLARE_MAX_TOKENS", "2048"))))
    except ValueError:
        max_tokens = 2048
    data = _post(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}",
        {"Authorization": "Bearer " + _key("CLOUDFLARE_API_TOKEN"), "User-Agent": _UA},
        # without max_tokens many models stop after a few hundred tokens
        {"messages": _messages(system, prompt), "max_tokens": max_tokens},
        timeout=timeout,
        retries=0,
    )
    result = data.get("result") if isinstance(data, dict) else None
    text = result.get("response") if isinstance(result, dict) else None
    if not isinstance(text, str):
        text = _content(result, "cloudflare") if isinstance(result, dict) and "choices" in result else ""
    if not (isinstance(data, dict) and data.get("success", True)) or not text.strip():
        errors = json.dumps(data.get("errors") if isinstance(data, dict) else data, ensure_ascii=False)[:300]
        raise ProviderError(f"cloudflare: пустой ответ {errors}")
    return Answer(text, [], model, engine="cloudflare", search=False)


_DEMO_SOURCES = ["krisha.kz", "kn.kz", "korter.kz", "forbes.kz", "kapital.kz", "2gis.kz", "inbusiness.kz"]


def make_demo(companies):
    """Offline stand-in that invents answers. For checking the pipeline only."""
    names = [c["name"] for c in companies]
    domains = {c["name"]: c["domain"] for c in companies}
    weights = [0.72, 0.55, 0.4, 0.28, 0.18] + [0.12] * len(names)

    def demo(prompt, system=None, _bias=0.0):
        rnd = random.Random(hashlib.sha256((prompt + str(_bias)).encode()).hexdigest())
        picked = [n for n, w in zip(names, weights) if rnd.random() < w + _bias]
        rnd.shuffle(picked)
        if not picked:
            return "Однозначного лидера назвать сложно: сравните сроки сдачи, отзывы и условия рассрочки.", [], "demo"
        lines = [f"{i}. {n} — часто упоминается в обзорах и отзывах покупателей." for i, n in enumerate(picked, 1)]
        urls = [f"https://{d}/obzor" for d in rnd.sample(_DEMO_SOURCES, rnd.randint(1, 3))]
        if domains.get(picked[0]) and rnd.random() < 0.5:
            urls.append(f"https://{domains[picked[0]]}/")
        text = "Вот варианты, на которые стоит обратить внимание:\n" + "\n".join(lines) + "\nИсточники: " + ", ".join(urls[:1])
        return text, urls, "demo"

    return demo


def get(names, companies):
    table = {"openai": openai, "gemini": gemini, "perplexity": perplexity}
    out = {}
    for n in names:
        if n.startswith("demo"):
            base = make_demo(companies)
            bias = {"demo": 0.0, "demo-b": -0.1, "demo-c": 0.08}.get(n, 0.0)
            out[n] = (lambda b: lambda prompt, system=None: base(prompt, system, _bias=b))(bias)
        elif n in table:
            out[n] = table[n]
        else:
            raise ProviderError(f"неизвестный провайдер: {n} (есть openai, gemini, perplexity, demo)")
    return out
