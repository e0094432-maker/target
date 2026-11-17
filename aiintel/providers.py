"""AI providers. Each returns (answer_text, cited_urls, model_name).

Plain HTTPS calls, no SDKs. Model names change often: override them with
OPENAI_MODEL / GEMINI_MODEL / PERPLEXITY_MODEL instead of editing code.
"""
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request


class ProviderError(Exception):
    pass


def _post(url, headers, payload, timeout=180, retries=3):
    body = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
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
    value = os.environ.get(name)
    if not value:
        raise ProviderError(f"не задан {name}")
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
    return msg.get("content") or "", urls, data.get("model", model)


def perplexity(prompt, system=None):
    model = os.environ.get("PERPLEXITY_MODEL", "sonar")
    data = _post(
        "https://api.perplexity.ai/chat/completions",
        {"Authorization": "Bearer " + _key("PERPLEXITY_API_KEY")},
        {"model": model, "messages": _messages(system, prompt)},
    )
    urls = list(data.get("citations") or [])
    urls += [r["url"] for r in data.get("search_results") or [] if r.get("url") and r["url"] not in urls]
    return data["choices"][0]["message"].get("content") or "", urls, data.get("model", model)


def gemini(prompt, system=None):
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if os.environ.get("GEMINI_SEARCH", "1") != "0":
        payload["tools"] = [{"google_search": {}}]
    data = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": _key("GEMINI_API_KEY")},
        payload,
    )
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    chunks = cand.get("groundingMetadata", {}).get("groundingChunks") or []
    urls = [c["web"]["uri"] for c in chunks if c.get("web", {}).get("uri")]
    return text, urls, data.get("modelVersion", model)


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
