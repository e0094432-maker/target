"""Sends every prompt to every provider and stores answers, mentions, citations."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import detect, metrics, providers


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ask(fn, text, system):
    t0 = time.monotonic()
    try:
        answer, urls, model = fn(text, system)
        return {"text": answer, "urls": urls, "model": model, "error": None, "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:  # one failed call must not stop the study
        return {"text": None, "urls": [], "model": None, "error": str(e)[:500], "ms": int((time.monotonic() - t0) * 1000)}


def run(con, study, companies, prompts, provider_names, repeats=1, limit=None, workers=4, log=print):
    fns = providers.get(provider_names, companies)
    prompts = prompts[:limit] if limit else prompts
    is_demo = int(all(n.startswith("demo") for n in provider_names))
    run_id = con.execute(
        "INSERT INTO runs (study, started_at, is_demo, note) VALUES (?,?,?,?)",
        (study["id"], now(), is_demo, ",".join(provider_names)),
    ).lastrowid
    con.commit()
    jobs = [(p, name, r) for p in prompts for name in provider_names for r in range(repeats)]
    system = study.get("system")
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda j: _ask(fns[j[1]], j[0]["text"], system), jobs)
        for i, ((prompt, name, rep), res) in enumerate(zip(jobs, results), 1):
            rid = con.execute(
                "INSERT INTO responses (run_id, prompt_id, provider, model, repeat, text, error, latency_ms, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, prompt["id"], name, res["model"], rep, res["text"], res["error"], res["ms"], now()),
            ).lastrowid
            if res["error"]:
                failed += 1
                log(f"[{i}/{len(jobs)}] {name}: ошибка — {res['error']}")
            else:
                store_analysis(con, rid, res["text"], res["urls"], companies)
            if i % 20 == 0:
                con.commit()
                log(f"[{i}/{len(jobs)}] готово")
    con.commit()
    metrics.snapshot(con, run_id, now())
    return run_id, len(jobs), failed


def store_analysis(con, response_id, text, api_urls, companies):
    for m in detect.find_mentions(text, companies):
        con.execute(
            "INSERT INTO mentions (response_id, company_id, position, count, first_offset) VALUES (?,?,?,?,?)",
            (response_id, m["company_id"], m["position"], m["count"], m["first_offset"]),
        )
    for c in detect.find_citations(text, api_urls):
        con.execute("INSERT OR IGNORE INTO sources (domain) VALUES (?)", (c["domain"],))
        sid = con.execute("SELECT id FROM sources WHERE domain = ?", (c["domain"],)).fetchone()[0]
        con.execute(
            "INSERT INTO citations (response_id, source_id, url, origin) VALUES (?,?,?,?)",
            (response_id, sid, c["url"], c["origin"]),
        )
