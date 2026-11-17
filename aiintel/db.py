"""SQLite storage. Table names follow the entities from the product concept."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY, study TEXT NOT NULL, name TEXT NOT NULL,
  aliases TEXT NOT NULL, domain TEXT, UNIQUE (study, name));
CREATE TABLE IF NOT EXISTS prompts (
  id INTEGER PRIMARY KEY, study TEXT NOT NULL, category TEXT, lang TEXT,
  text TEXT NOT NULL, UNIQUE (study, text));
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, study TEXT NOT NULL, started_at TEXT NOT NULL,
  is_demo INTEGER NOT NULL DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS responses (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  prompt_id INTEGER NOT NULL REFERENCES prompts(id), provider TEXT NOT NULL,
  model TEXT, repeat INTEGER NOT NULL DEFAULT 0, text TEXT, error TEXT,
  latency_ms INTEGER, created_at TEXT NOT NULL,
  UNIQUE (run_id, prompt_id, provider, repeat));
CREATE TABLE IF NOT EXISTS mentions (
  id INTEGER PRIMARY KEY, response_id INTEGER NOT NULL REFERENCES responses(id),
  company_id INTEGER NOT NULL REFERENCES companies(id),
  position INTEGER NOT NULL, count INTEGER NOT NULL, first_offset INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sources (id INTEGER PRIMARY KEY, domain TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS citations (
  id INTEGER PRIMARY KEY, response_id INTEGER NOT NULL REFERENCES responses(id),
  source_id INTEGER NOT NULL REFERENCES sources(id), url TEXT NOT NULL, origin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visibility_history (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  company_id INTEGER NOT NULL REFERENCES companies(id), provider TEXT NOT NULL,
  total INTEGER NOT NULL, mentioned INTEGER NOT NULL, visibility REAL NOT NULL,
  avg_position REAL, share_of_voice REAL NOT NULL, own_citations INTEGER NOT NULL,
  taken_at TEXT NOT NULL);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def sync_study(con, study):
    """Upsert the study's companies and prompts; returns (companies, prompts) rows."""
    key = study["id"]
    for c in study["companies"]:
        aliases = json.dumps(c.get("aliases", []), ensure_ascii=False)
        con.execute(
            "INSERT INTO companies (study, name, aliases, domain) VALUES (?,?,?,?) "
            "ON CONFLICT (study, name) DO UPDATE SET aliases = excluded.aliases, domain = excluded.domain",
            (key, c["name"], aliases, c.get("domain")),
        )
    for p in study["prompts"]:
        con.execute(
            "INSERT INTO prompts (study, category, lang, text) VALUES (?,?,?,?) "
            "ON CONFLICT (study, text) DO UPDATE SET category = excluded.category, lang = excluded.lang",
            (key, p.get("category"), p.get("lang"), p["text"]),
        )
    con.commit()
    names = [c["name"] for c in study["companies"]]
    texts = [p["text"] for p in study["prompts"]]
    companies = [r for r in con.execute("SELECT * FROM companies WHERE study = ?", (key,)) if r["name"] in names]
    prompts = [r for r in con.execute("SELECT * FROM prompts WHERE study = ?", (key,)) if r["text"] in texts]
    companies.sort(key=lambda r: names.index(r["name"]))
    prompts.sort(key=lambda r: texts.index(r["text"]))
    return companies, prompts
