"""Tiny SQLite store so planner and approver see the same runs."""
from __future__ import annotations

import io
import json
import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd

DB_PATH = os.environ.get("ALLOC_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "app.db"))


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month TEXT, created_by TEXT, created_at TEXT,
            status TEXT, solver_status TEXT,
            summary TEXT, allocation TEXT, capacity TEXT, warnings TEXT, settings TEXT,
            submitted_at TEXT, decided_by TEXT, decided_at TEXT, comment TEXT
        )""")
    return con


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def save_run(month, user, result, capacity, settings) -> int:
    s = dict(result["summary"])
    s["by_warehouse"] = s["by_warehouse"].reset_index().to_dict("records")
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO runs (month, created_by, created_at, status, solver_status, summary, allocation,"
            " capacity, warnings, settings) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (month, user, _now(), "Draft", result["status"], json.dumps(s),
             result["allocation"].to_json(orient="records"), capacity.to_json(orient="split"),
             json.dumps(result["warnings"]), json.dumps(settings)))
        return cur.lastrowid


def list_runs(status: str | None = None, created_by: str | None = None) -> pd.DataFrame:
    q, args = "SELECT id, month, created_by, created_at, status, solver_status, decided_by, decided_at FROM runs", []
    where = []
    if status:
        where.append("status = ?"); args.append(status)
    if created_by:
        where.append("created_by = ?"); args.append(created_by)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC"
    with _conn() as con:
        return pd.read_sql_query(q, con, params=args)


def get_run(run_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["summary"] = json.loads(d["summary"])
    d["summary"]["by_warehouse"] = pd.DataFrame(d["summary"]["by_warehouse"]).set_index("warehouse")
    d["allocation"] = pd.read_json(io.StringIO(d["allocation"]), orient="records")
    d["capacity"] = pd.read_json(io.StringIO(d["capacity"]), orient="split")
    d["warnings"] = json.loads(d["warnings"])
    d["settings"] = json.loads(d["settings"])
    return d


def submit(run_id: int):
    with _conn() as con:
        con.execute("UPDATE runs SET status='Submitted', submitted_at=? WHERE id=? AND status IN ('Draft','Rejected')",
                    (_now(), run_id))


def decide(run_id: int, approver: str, approve: bool, comment: str):
    with _conn() as con:
        con.execute("UPDATE runs SET status=?, decided_by=?, decided_at=?, comment=? WHERE id=? AND status='Submitted'",
                    ("Approved" if approve else "Rejected", approver, _now(), comment, run_id))
