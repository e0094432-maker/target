"""Self-contained HTML report for one run."""
from html import escape as e

from . import metrics

CSS = """
:root{--bg:#faf8f2;--paper:#fff;--ink:#0d0d0c;--muted:#69655c;--accent:#ffc800;--tint:#efebdf}
@media (prefers-color-scheme:dark){:root{--bg:#0d0d0c;--paper:#1a1a18;--ink:#f6f2e7;--muted:#a39e92;--tint:#22221f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 Onest,system-ui,-apple-system,"Segoe UI",sans-serif}
.w{max-width:1040px;margin:0 auto;padding:28px 16px 80px}
h1{font-size:clamp(26px,6vw,44px);line-height:1.1;letter-spacing:-.03em;margin:10px 0 6px;font-weight:800}
h2{font-size:22px;letter-spacing:-.02em;margin:44px 0 6px}
.meta,.note,td small,th{font:500 12px/1.5 ui-monospace,Menlo,monospace;color:var(--muted)}
.note{margin:0 0 14px;max-width:75ch}
.demo{background:var(--accent);color:#0d0d0c;border:2px solid #0d0d0c;border-radius:12px;padding:12px 16px;font-weight:700;margin-bottom:18px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:22px}
.kpi{background:var(--paper);border:1.5px solid var(--ink);border-radius:18px;padding:18px;box-shadow:4px 4px 0 var(--ink)}
.kpi b{display:block;font-size:44px;line-height:1;letter-spacing:-.04em;font-weight:800}
.kpi.main{background:var(--accent);color:#0d0d0c}.kpi span{font-size:14px}
.card{background:var(--paper);border:1.5px solid var(--ink);border-radius:18px;padding:18px;overflow-x:auto}
.bar{display:grid;grid-template-columns:minmax(110px,170px) 1fr 52px;gap:10px;align-items:center;margin:9px 0;font-size:14px}
.bar i{display:block;height:22px;border-radius:6px;background:var(--ink);min-width:2px}
.bar.me i{background:var(--accent);outline:1.5px solid var(--ink)}.bar.me span:first-child{font-weight:700}
.bar em{font-style:normal;text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--tint);vertical-align:top}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr.me td{font-weight:700;background:color-mix(in srgb,var(--accent) 22%,transparent)}
details{border:1.5px solid var(--ink);border-radius:14px;background:var(--paper);margin:8px 0;padding:0 14px}
summary{cursor:pointer;padding:12px 0;font-weight:600}pre{white-space:pre-wrap;font:13px/1.5 ui-monospace,Menlo,monospace;margin:0 0 14px}
ul{padding-left:20px;margin:6px 0}li{margin:4px 0}
"""


def pct(x):
    return f"{round(x * 100)}%"


def pos(x):
    return "—" if x is None else f"{x:.1f}".replace(".", ",")


def build(con, run_id, client_name=None):
    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"запуск {run_id} не найден")
    data = metrics.compute(con, run_id)
    overall = data[metrics.ALL]
    if not overall:
        raise ValueError("в исследовании нет компаний")
    client_id = next((cid for cid, s in overall.items() if s["name"] == client_name), None)
    if client_id is None:
        if client_name:
            raise ValueError(f"компании «{client_name}» нет в исследовании")
        client_id = next(iter(overall))
    me = overall[client_id]
    providers = [p for p in data if p != metrics.ALL]
    counts = con.execute(
        "SELECT COUNT(*) AS n, SUM(error IS NOT NULL) AS failed, COUNT(DISTINCT prompt_id) AS prompts "
        "FROM responses WHERE run_id = ?", (run_id,)
    ).fetchone()
    models = con.execute(
        "SELECT provider, GROUP_CONCAT(DISTINCT model) AS m FROM responses WHERE run_id = ? AND error IS NULL "
        "GROUP BY provider", (run_id,)
    ).fetchall()
    ranked = sorted(overall.items(), key=lambda kv: -kv[1]["visibility"])
    place = [cid for cid, _ in ranked].index(client_id) + 1

    h = [f"<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>AI Visibility · {e(me['name'])}</title><style>{CSS}</style><div class='w'>"]
    if run["is_demo"]:
        h.append("<div class='demo'>ДЕМО-ДАННЫЕ. Ответы сгенерированы заглушкой, а не нейросетями. "
                 "Цифры ничего не говорят о реальных компаниях.</div>")
    h.append(f"<div class='meta'>AI Intelligence · запуск №{run_id} · {e(run['started_at'][:10])}</div>")
    h.append(f"<h1>Видимость в ответах ИИ: {e(me['name'])}</h1>")
    h.append("<div class='meta'>" + e(run["study"]) + " · вопросов: " + str(counts["prompts"]) + " · ответов: "
             + str(counts["n"] - (counts["failed"] or 0)) + (f" · ошибок: {counts['failed']}" if counts["failed"] else "")
             + " · " + e("; ".join(f"{m['provider']}: {m['m']}" for m in models)) + "</div>")

    h.append("<div class='kpis'>")
    h.append(f"<div class='kpi main'><b>{pct(me['visibility'])}</b><span>AI Visibility Score: доля ответов, где компания названа "
             f"({me['mentioned']} из {me['total']})</span></div>")
    h.append(f"<div class='kpi'><b>{place}<small style='font-size:18px'> из {len(overall)}</small></b><span>место среди отслеживаемых компаний</span></div>")
    h.append(f"<div class='kpi'><b>{pos(me['avg_position'])}</b><span>средняя позиция в ответе; первой названа {me['first_place']} раз</span></div>")
    h.append(f"<div class='kpi'><b>{pct(me['share_of_voice'])}</b><span>доля от всех упоминаний отслеживаемых компаний</span></div>")
    h.append(f"<div class='kpi'><b>{me['own_citations']}</b><span>ответов со ссылкой на собственный сайт</span></div></div>")

    h.append("<h2>Частота упоминаний</h2><div class='card'>")
    for cid, s in ranked:
        h.append(f"<div class='bar{' me' if cid == client_id else ''}'><span>{e(s['name'])}</span>"
                 f"<i style='width:{s['visibility'] * 100:.1f}%'></i><em>{pct(s['visibility'])}</em></div>")
    h.append("</div>")

    if len(providers) > 1:
        h.append("<h2>Разница между ИИ-системами</h2><div class='card'><table><tr><th>Компания</th>"
                 + "".join(f"<th class='n'>{e(p)}</th>" for p in providers) + "<th class='n'>все</th></tr>")
        for cid, s in ranked:
            h.append(f"<tr class='{'me' if cid == client_id else ''}'><td>{e(s['name'])}</td>"
                     + "".join(f"<td class='n'>{pct(data[p][cid]['visibility'])}</td>" for p in providers)
                     + f"<td class='n'>{pct(s['visibility'])}</td></tr>")
        h.append("</table></div>")

    h.append("<h2>Подробно по компаниям</h2><div class='card'><table><tr><th>Компания</th><th class='n'>Упоминаний</th>"
             "<th class='n'>Видимость</th><th class='n'>Ср. позиция</th><th class='n'>Названа первой</th>"
             "<th class='n'>Доля голоса</th><th class='n'>Ссылок на свой сайт</th></tr>")
    for cid, s in ranked:
        h.append(f"<tr class='{'me' if cid == client_id else ''}'><td>{e(s['name'])}</td><td class='n'>{s['mentioned']}</td>"
                 f"<td class='n'>{pct(s['visibility'])}</td><td class='n'>{pos(s['avg_position'])}</td>"
                 f"<td class='n'>{s['first_place']}</td><td class='n'>{pct(s['share_of_voice'])}</td>"
                 f"<td class='n'>{s['own_citations']}</td></tr>")
    h.append("</table></div>")

    gaps = metrics.gaps(con, run_id, client_id)
    h.append(f"<h2>Где компанию не называют</h2><p class='note'>Вопросы, в ответах на которые есть конкуренты, но нет «{e(me['name'])}»: "
             f"{len(gaps)} из {counts['prompts']}.</p>")
    if gaps:
        h.append("<div class='card'><table><tr><th>Вопрос</th><th>Кого называют вместо</th></tr>")
        for g in gaps[:15]:
            rivals = ", ".join(f"{e(n)} ×{k}" for n, k in sorted(g["rivals"].items(), key=lambda kv: -kv[1]))
            h.append(f"<tr><td>{e(g['text'])}<br><small>{e(g['category'] or '')}</small></td><td>{rivals}</td></tr>")
        h.append("</table></div>")

    sources = metrics.top_sources(con, run_id)
    h.append("<h2>Источники, на которые ссылаются ИИ</h2>")
    if sources:
        h.append("<div class='card'><table><tr><th>Сайт</th><th class='n'>Ответов со ссылкой</th><th class='n'>Всего ссылок</th></tr>")
        h.extend(f"<tr><td>{e(s['domain'])}</td><td class='n'>{s['responses']}</td><td class='n'>{s['links']}</td></tr>" for s in sources)
        h.append("</table></div>")
    else:
        h.append("<p class='note'>В этом запуске ИИ не дали ни одной ссылки. Модели без веб-поиска ссылок обычно не дают.</p>")

    hist = metrics.history(con, run["study"], client_id)
    if len(hist) > 1:
        h.append("<h2>История видимости</h2><div class='card'><table><tr><th>Дата</th><th class='n'>Запуск</th><th class='n'>Видимость</th></tr>")
        h.extend(f"<tr><td>{e(r['taken_at'][:10])}</td><td class='n'>№{r['run_id']}</td><td class='n'>{pct(r['visibility'])}</td></tr>" for r in hist)
        h.append("</table></div>")

    samples = con.execute(
        "SELECT r.provider, r.text, p.text AS q FROM responses r JOIN prompts p ON p.id = r.prompt_id "
        "WHERE r.run_id = ? AND r.error IS NULL ORDER BY r.id LIMIT 6", (run_id,)
    ).fetchall()
    h.append("<h2>Примеры ответов</h2>")
    h.extend(f"<details><summary>{e(s['provider'])}: {e(s['q'])}</summary><pre>{e(s['text'] or '')}</pre></details>" for s in samples)

    h.append("<h2>Как читать отчёт</h2><ul class='note' style='max-width:80ch'>"
             "<li>Ответы получены через API. Они могут отличаться от того, что человек видит в приложении: там другие настройки, поиск и персонализация.</li>"
             "<li>Результат зависит от формулировки вопроса, языка, региона, даты и версии модели. Один запуск — это срез, а не постоянная величина.</li>"
             "<li>Упоминания ищутся по названию компании и заданным вариантам написания. Компании вне списка не учитываются.</li>"
             "<li>Отчёт показывает, что отвечают ИИ, но не доказывает, почему. Связь с источниками — наблюдение, а не установленная причина.</li></ul>")
    h.append("</div></html>")
    return "".join(h)
