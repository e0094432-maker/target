"""Brand mention and citation detection in an AI answer."""
import json
import re
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s<>\"'\])]+")


def _pattern(names):
    alts = sorted({n.strip() for n in names if n.strip()}, key=len, reverse=True)
    body = "|".join(re.escape(a).replace(r"\ ", r"[\s\-]+") for a in alts)
    return re.compile(r"(?<!\w)(?:" + body + r")(?!\w)", re.IGNORECASE)


def find_mentions(text, companies):
    """Return [{company_id, position, count, first_offset}] ordered by first appearance.

    `companies` are rows/dicts with id, name and aliases (JSON list or list).
    Position 1 is the brand the answer names first.
    """
    found = []
    for c in companies:
        aliases = c["aliases"]
        if isinstance(aliases, str):
            aliases = json.loads(aliases)
        hits = [m.start() for m in _pattern([c["name"], *aliases]).finditer(text or "")]
        if hits:
            found.append({"company_id": c["id"], "count": len(hits), "first_offset": hits[0]})
    found.sort(key=lambda m: m["first_offset"])
    for i, m in enumerate(found, 1):
        m["position"] = i
    return found


def domain_of(url):
    host = (urlparse(url if "//" in url else "//" + url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def find_citations(text, api_urls=()):
    """Merge URLs reported by the provider with URLs written in the answer text."""
    seen, out = set(), []
    pairs = [(u, "api") for u in api_urls] + [(u.rstrip(".,;:!?"), "text") for u in URL_RE.findall(text or "")]
    for url, origin in pairs:
        dom = domain_of(url)
        if dom and url not in seen:
            seen.add(url)
            out.append({"url": url, "domain": dom, "origin": origin})
    return out


def same_site(domain, company_domain):
    if not company_domain:
        return False
    company_domain = domain_of(company_domain)
    return domain == company_domain or domain.endswith("." + company_domain)
