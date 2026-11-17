"""Command line: python -m aiintel run|report|demo|runs"""
import argparse
import json
import sys
from pathlib import Path

from . import db, report, runner
from .providers import ProviderError


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_report(con, run_id, client, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.build(con, run_id, client), encoding="utf-8")
    print(f"Отчёт: {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aiintel", description="Видимость компаний в ответах ИИ")
    ap.add_argument("--db", default="data/aiintel.db", help="файл базы SQLite")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="задать вопросы ИИ и сохранить ответы")
    r.add_argument("study")
    r.add_argument("--providers", default="openai,gemini,perplexity")
    r.add_argument("--repeats", type=int, default=1, help="сколько раз задавать каждый вопрос")
    r.add_argument("--limit", type=int, help="взять только первые N вопросов")
    r.add_argument("--out", help="сразу собрать отчёт в этот файл")
    r.add_argument("--client", help="компания, для которой строится отчёт")

    p = sub.add_parser("report", help="собрать HTML-отчёт по запуску")
    p.add_argument("study")
    p.add_argument("--run", type=int, help="номер запуска (по умолчанию последний)")
    p.add_argument("--client")
    p.add_argument("--out", default="reports/report.html")

    d = sub.add_parser("demo", help="прогон на заглушке без ключей API")
    d.add_argument("study")
    d.add_argument("--out", default="reports/demo.html")

    sub.add_parser("runs", help="список запусков")
    args = ap.parse_args(argv)
    con = db.connect(args.db)

    if args.cmd == "runs":
        for row in con.execute("SELECT r.*, (SELECT COUNT(*) FROM responses WHERE run_id = r.id) AS n FROM runs r ORDER BY id"):
            print(f"№{row['id']}  {row['started_at']}  {row['study']}  {row['note']}  ответов: {row['n']}{'  ДЕМО' if row['is_demo'] else ''}")
        return 0

    study = _load(args.study)
    companies, prompts = db.sync_study(con, study)
    try:
        if args.cmd in ("run", "demo"):
            names = ["demo", "demo-b", "demo-c"] if args.cmd == "demo" else [n.strip() for n in args.providers.split(",") if n.strip()]
            run_id, total, failed = runner.run(
                con, study, companies, prompts, names,
                repeats=getattr(args, "repeats", 1), limit=getattr(args, "limit", None),
            )
            print(f"Запуск №{run_id}: {total - failed} ответов, ошибок: {failed}")
            if failed == total:
                print("Ни одного ответа не получено, отчёт не собран.", file=sys.stderr)
                return 1
            if args.out:
                _write_report(con, run_id, getattr(args, "client", None) or study.get("client"), args.out)
        else:
            run_id = args.run or (con.execute("SELECT MAX(id) FROM runs WHERE study = ?", (study["id"],)).fetchone()[0])
            if run_id is None:
                print("Запусков ещё нет: сначала выполните run или demo.", file=sys.stderr)
                return 1
            _write_report(con, run_id, args.client or study.get("client"), args.out)
    except (ProviderError, ValueError) as err:
        print(f"Ошибка: {err}", file=sys.stderr)
        return 1
    return 0
