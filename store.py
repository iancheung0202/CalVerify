"""Student sheet storage in SQLite with a trigram index for substring search.

Search semantics match the previous in-memory Polars implementation: a row
matches when the query, lowercased and treated as a regular expression, is
found in the lowercased text of any single column.
"""
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).parent / "data" / "students.db"
SEP = "\n"
NULL = "\x1e"
REGEX_META = set("\\^$.|?*+()[]{}")

_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sheets (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                last_modified_iso TEXT NOT NULL,
                total_rows INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS student_rows (
                id INTEGER PRIMARY KEY,
                sheet_id INTEGER NOT NULL,
                data TEXT NOT NULL,
                hay TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS student_rows_sheet ON student_rows(sheet_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS student_fts USING fts5(
                hay, content='student_rows', content_rowid='id', tokenize='trigram'
            );
            """
        )
    return _conn


def meta() -> Dict[str, Dict[str, Any]]:
    with _lock:
        cur = conn().execute("SELECT name, last_modified_iso, total_rows FROM sheets ORDER BY id")
        return {n: {"last_modified_iso": m, "total_rows": t} for n, m, t in cur}


def exists(name: str) -> bool:
    with _lock:
        return conn().execute("SELECT 1 FROM sheets WHERE name = ?", (name,)).fetchone() is not None


def delete(name: str) -> bool:
    with _lock:
        c = conn()
        row = c.execute("SELECT id FROM sheets WHERE name = ?", (name,)).fetchone()
        if row is None:
            return False
        with c:
            c.execute(
                "INSERT INTO student_fts(student_fts, rowid, hay) "
                "SELECT 'delete', id, hay FROM student_rows WHERE sheet_id = ?",
                (row[0],),
            )
            c.execute("DELETE FROM student_rows WHERE sheet_id = ?", (row[0],))
            c.execute("DELETE FROM sheets WHERE id = ?", (row[0],))
        return True


def rename(old: str, new: str) -> None:
    with _lock:
        with conn() as c:
            c.execute("UPDATE sheets SET name = ? WHERE name = ?", (new, old))


def store_csv(name: str, csv_source, last_modified_iso: str) -> int:
    """Parse CSV (path or bytes) the same way as before and replace the sheet."""
    import polars as pl

    df = pl.read_csv(csv_source)
    lowered = df.select([pl.col(col).cast(pl.Utf8).str.to_lowercase() for col in df.columns])
    hays = [SEP.join(NULL if v is None else v for v in r) for r in lowered.iter_rows()]
    datas = [_dumps(r) for r in df.to_dicts()]
    with _lock:
        delete(name)
        c = conn()
        with c:
            sheet_id = c.execute(
                "INSERT INTO sheets(name, last_modified_iso, total_rows) VALUES (?, ?, ?)",
                (name, last_modified_iso, len(df)),
            ).lastrowid
            c.executemany(
                "INSERT INTO student_rows(sheet_id, data, hay) VALUES (?, ?, ?)",
                ((sheet_id, d, h) for d, h in zip(datas, hays)),
            )
            c.execute(
                "INSERT INTO student_fts(rowid, hay) SELECT id, hay FROM student_rows WHERE sheet_id = ?",
                (sheet_id,),
            )
    return len(df)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hits(c: sqlite3.Connection, qlower: str) -> List[tuple]:
    """(sheet_id, row id) of every matching row, in sheet then row order."""
    ids = "SELECT r.sheet_id, r.id FROM student_rows r"
    scan = "SELECT r.sheet_id, r.id, r.hay FROM student_rows r"
    fts = " WHERE r.id IN (SELECT rowid FROM student_fts WHERE student_fts MATCH ?)"
    meta_chars = set(qlower) & REGEX_META
    odd = SEP in qlower or NULL in qlower

    def fts_query(parts):
        return " AND ".join('"' + p.replace('"', '""') + '"' for p in parts)

    if not meta_chars and not odd:
        if len(qlower) >= 3:
            hits = c.execute(ids + fts, (fts_query([qlower]),)).fetchall()
        else:
            hits = c.execute(ids + " WHERE instr(r.hay, ?) > 0", (qlower,)).fetchall()
    elif meta_chars == {"."} and not odd and qlower.strip("."):
        parts = [p for p in qlower.split(".") if len(p) >= 3]
        if parts:
            cur = c.execute(scan + fts, (fts_query(parts),))
        else:
            like = "%" + qlower.replace("!", "!!").replace("%", "!%").replace("_", "!_").replace(".", "_") + "%"
            cur = c.execute(scan + " WHERE r.hay LIKE ? ESCAPE '!'", (like,))
        found = re.compile(qlower).search
        hits = [(sid, rid) for sid, rid, hay in cur if found(hay)]
    else:
        whole = re.compile(qlower, re.MULTILINE).search
        found = re.compile(qlower).search
        hits = [
            (sid, rid)
            for sid, rid, hay in c.execute(scan)
            if whole(hay) and any(col != NULL and found(col) for col in hay.split(SEP))
        ]
    hits.sort()
    return hits


def count(query: str) -> int:
    with _lock:
        return len(_hits(conn(), query.lower()))


def search(query: str, limit: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    """Per sheet: match_count, plus the first `limit` rows as encoded JSON objects."""
    with _lock:
        c = conn()
        by_sheet: Dict[int, List[int]] = {}
        for sid, rid in _hits(c, query.lower()):
            by_sheet.setdefault(sid, []).append(rid)
        names = dict(c.execute("SELECT id, name FROM sheets"))
        results: Dict[str, Dict[str, Any]] = {}
        for sid, rids in by_sheet.items():
            keep = rids if limit is None else rids[:limit]
            rows: List[str] = []
            for k in range(0, len(keep), 500):
                chunk = keep[k:k + 500]
                marks = ",".join("?" * len(chunk))
                rows += [d for (d,) in c.execute(f"SELECT data FROM student_rows WHERE id IN ({marks}) ORDER BY id", chunk)]
            results[names[sid]] = {"rows": rows, "match_count": len(rids)}
        return results
