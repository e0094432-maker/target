"""Visibility metrics for one run. Failed responses are excluded everywhere."""
from . import detect

ALL = "all"


def _companies(con, run_id):
    study = con.execute("SELECT study FROM runs WHERE id = ?", (run_id,)).fetchone()["study"]
    return con.execute("SELECT * FROM companies WHERE study = ? ORDER BY id", (study,)).fetchall()


def compute(con, run_id):
    """Return {provider: {company_id: stats}} plus the same under provider 'all'."""
    companies = _companies(con, run_id)
    responses = con.execute(
        "SELECT id, provider FROM responses WHERE run_id = ? AND error IS NULL", (run_id,)
    ).fetchall()
    by_provider = {}
    for r in responses:
        by_provider.setdefault(r["provider"], []).append(r["id"])
    by_provider[ALL] = [r["id"] for r in responses]

    mentions = {}
    for m in con.execute(
        "SELECT m.* FROM mentions m JOIN responses r ON r.id = m.response_id WHERE r.run_id = ?", (run_id,)
    ):
        mentions.setdefault(m["response_id"], []).append(m)
    cited = {}
    for c in con.execute(
        "SELECT c.response_id, s.domain FROM citations c JOIN sources s ON s.id = c.source_id "
        "JOIN responses r ON r.id = c.response_id WHERE r.run_id = ?", (run_id,)
    ):
        cited.setdefault(c["response_id"], set()).add(c["domain"])

    out = {}
    for provider, ids in by_provider.items():
        voice_total = sum(len(mentions.get(i, [])) for i in ids)
        stats = {}
        for c in companies:
            own = [m for i in ids for m in mentions.get(i, []) if m["company_id"] == c["id"]]
            own_cites = sum(1 for i in ids if any(detect.same_site(d, c["domain"]) for d in cited.get(i, ())))
            stats[c["id"]] = {
                "name": c["name"],
                "total": len(ids),
                "mentioned": len(own),
                "visibility": len(own) / len(ids) if ids else 0.0,
                "avg_position": sum(m["position"] for m in own) / len(own) if own else None,
                "first_place": sum(1 for m in own if m["position"] == 1),
                "share_of_voice": len(own) / voice_total if voice_total else 0.0,
                "own_citations": own_cites,
            }
        out[provider] = stats
    return out


def snapshot(con, run_id, taken_at):
    """Write this run's numbers to visibility_history so trends can be built later."""
    con.execute("DELETE FROM visibility_history WHERE run_id = ?", (run_id,))
    for provider, stats in compute(con, run_id).items():
        for cid, s in stats.items():
            con.execute(
                "INSERT INTO visibility_history (run_id, company_id, provider, total, mentioned, visibility, "
                "avg_position, share_of_voice, own_citations, taken_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, cid, provider, s["total"], s["mentioned"], s["visibility"], s["avg_position"],
                 s["share_of_voice"], s["own_citations"], taken_at),
            )
    con.commit()


def top_sources(con, run_id, limit=15):
    return con.execute(
        "SELECT s.domain, COUNT(DISTINCT c.response_id) AS responses, COUNT(*) AS links "
        "FROM citations c JOIN sources s ON s.id = c.source_id JOIN responses r ON r.id = c.response_id "
        "WHERE r.run_id = ? GROUP BY s.domain ORDER BY responses DESC, links DESC, s.domain LIMIT ?",
        (run_id, limit),
    ).fetchall()


def gaps(con, run_id, client_id):
    """Prompts where a competitor is named in some answer but the client never is."""
    rows = con.execute(
        "SELECT p.id, p.text, p.category, m.company_id, c.name FROM responses r "
        "JOIN prompts p ON p.id = r.prompt_id LEFT JOIN mentions m ON m.response_id = r.id "
        "LEFT JOIN companies c ON c.id = m.company_id WHERE r.run_id = ? AND r.error IS NULL",
        (run_id,),
    ).fetchall()
    per = {}
    for r in rows:
        e = per.setdefault(r["id"], {"text": r["text"], "category": r["category"], "client": False, "rivals": {}})
        if r["company_id"] == client_id:
            e["client"] = True
        elif r["company_id"] is not None:
            e["rivals"][r["name"]] = e["rivals"].get(r["name"], 0) + 1
    out = [e for e in per.values() if not e["client"] and e["rivals"]]
    out.sort(key=lambda e: -sum(e["rivals"].values()))
    return out


def history(con, study, company_id):
    return con.execute(
        "SELECT h.taken_at, h.visibility, h.run_id FROM visibility_history h JOIN runs r ON r.id = h.run_id "
        "WHERE r.study = ? AND h.company_id = ? AND h.provider = ? ORDER BY h.taken_at",
        (study, company_id, ALL),
    ).fetchall()
