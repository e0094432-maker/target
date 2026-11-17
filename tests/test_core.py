import json
import tempfile
import unittest
from pathlib import Path

from aiintel import cli, db, detect, metrics, runner

COMPANIES = [
    {"id": 1, "name": "BI Group", "aliases": ["Би Ай Групп"], "domain": "bi.group"},
    {"id": 2, "name": "Bazis-A", "aliases": ["Базис-А", "Bazis A"], "domain": "bazis.kz"},
    {"id": 3, "name": "RAMS Qazaqstan", "aliases": ["RAMS"], "domain": "rams.kz"},
]


class DetectTest(unittest.TestCase):
    def test_order_count_and_aliases(self):
        text = "Сначала посмотрите Базис-А, затем bi group. У BI Group больше объектов."
        got = detect.find_mentions(text, COMPANIES)
        self.assertEqual([(m["company_id"], m["position"], m["count"]) for m in got], [(2, 1, 1), (1, 2, 2)])

    def test_no_match_inside_other_words(self):
        self.assertEqual(detect.find_mentions("Программа PROGRAMS и трамваи", COMPANIES), [])

    def test_spacing_variants(self):
        self.assertEqual(len(detect.find_mentions("Застройщик BI-Group и Bazis  A", COMPANIES)), 2)

    def test_citations_merge_and_dedupe(self):
        got = detect.find_citations("См. https://www.krisha.kz/a/1. и https://bi.group/", ["https://bi.group/"])
        self.assertEqual([(c["domain"], c["origin"]) for c in got], [("bi.group", "api"), ("krisha.kz", "text")])

    def test_same_site(self):
        self.assertTrue(detect.same_site("astana.bi.group", "bi.group"))
        self.assertFalse(detect.same_site("notbi.group", "bi.group"))
        self.assertFalse(detect.same_site("krisha.kz", None))


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = db.connect(str(Path(self.tmp.name) / "t.db"))
        self.study = {
            "id": "t",
            "companies": [{"name": c["name"], "aliases": c["aliases"], "domain": c["domain"]} for c in COMPANIES],
            "prompts": [{"text": "q1", "category": "c"}, {"text": "q2", "category": "c"}],
        }
        self.companies, self.prompts = db.sync_study(self.con, self.study)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _response(self, run_id, prompt, text, urls=(), error=None):
        rid = self.con.execute(
            "INSERT INTO responses (run_id, prompt_id, provider, model, repeat, text, error, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, prompt["id"], "x", "m", 0, text, error, "2026-01-01"),
        ).lastrowid
        if not error:
            runner.store_analysis(self.con, rid, text, urls, self.companies)

    def test_metrics_and_gaps(self):
        run_id = self.con.execute("INSERT INTO runs (study, started_at) VALUES ('t','2026-01-01')").lastrowid
        self._response(run_id, self.prompts[0], "Лучшие: BI Group и RAMS", ["https://bi.group/x"])
        self._response(run_id, self.prompts[1], "Советую RAMS")
        self.con.execute(
            "INSERT INTO responses (run_id, prompt_id, provider, repeat, error, created_at) VALUES (?,?,?,?,?,?)",
            (run_id, self.prompts[1]["id"], "y", 0, "boom", "2026-01-01"),
        )
        ids = {c["name"]: c["id"] for c in self.companies}
        m = metrics.compute(self.con, run_id)[metrics.ALL]
        bi, rams, bazis = m[ids["BI Group"]], m[ids["RAMS Qazaqstan"]], m[ids["Bazis-A"]]
        self.assertEqual((bi["total"], bi["mentioned"], bi["visibility"]), (2, 1, 0.5))
        self.assertEqual((bi["avg_position"], bi["first_place"], bi["own_citations"]), (1.0, 1, 1))
        self.assertEqual((rams["visibility"], rams["avg_position"]), (1.0, 1.5))
        self.assertAlmostEqual(rams["share_of_voice"], 2 / 3)
        self.assertEqual((bazis["mentioned"], bazis["avg_position"]), (0, None))
        gaps = metrics.gaps(self.con, run_id, ids["BI Group"])
        self.assertEqual([(g["text"], g["rivals"]) for g in gaps], [("q2", {"RAMS Qazaqstan": 1})])

    def test_sync_is_idempotent(self):
        db.sync_study(self.con, self.study)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 3)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], 2)

    def test_demo_cli_writes_report_and_history(self):
        study_path = Path(self.tmp.name) / "s.json"
        study_path.write_text(json.dumps(self.study, ensure_ascii=False), encoding="utf-8")
        out = Path(self.tmp.name) / "r.html"
        dbp = str(Path(self.tmp.name) / "cli.db")
        self.assertEqual(cli.main(["--db", dbp, "demo", str(study_path), "--out", str(out)]), 0)
        html = out.read_text(encoding="utf-8")
        self.assertIn("ДЕМО-ДАННЫЕ", html)
        self.assertIn("BI Group", html)
        con = db.connect(dbp)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 6)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM visibility_history").fetchone()[0], 12)
        con.close()

    def test_missing_key_is_recorded_not_raised(self):
        import os
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            run_id, total, failed = runner.run(self.con, self.study, self.companies, self.prompts, ["openai"], log=lambda *_: None)
        finally:
            if old:
                os.environ["OPENAI_API_KEY"] = old
        self.assertEqual((total, failed), (2, 2))
        self.assertIn("OPENAI_API_KEY", self.con.execute("SELECT error FROM responses WHERE run_id = ?", (run_id,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
