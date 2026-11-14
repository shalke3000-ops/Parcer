"""SQLite storage for leads, local businesses and monitor bookkeeping.

Raw `sqlite3` with parameterised queries only. Every function opens its own
short-lived connection so the API process and the monitor process can share
one database file safely (WAL mode).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .dedupe import fingerprint
from .lead_traits import budget_eur, has_fixed_pay, is_long_term, is_one_off

log = logging.getLogger(__name__)

STATUSES = ("new", "contacted", "replied", "client", "rejected")
MAX_NOTE_LENGTH = 4000
MAX_PAGE_SIZE = 500
# Sorted by when the post was published: that is the date every card shows.
LEAD_ORDER = {
    "newest": "posted_at DESC, id DESC",
    "oldest": "posted_at ASC, id ASC",
    "budget": "budget_eur(text) DESC NULLS LAST, posted_at DESC, id DESC",
}
# The same text coming back is an advert, not an order, when it keeps coming for a day,
# or floods in. A client cross-posting an order to a few chats within an hour is neither.
SPAM_MIN_COPIES = 4
SPAM_MIN_SPAN_HOURS = 24
SPAM_FLOOD_COPIES = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    source_name   TEXT NOT NULL DEFAULT '',
    external_id   TEXT NOT NULL,
    text          TEXT NOT NULL,
    link          TEXT NOT NULL DEFAULT '',
    author        TEXT NOT NULL DEFAULT '',
    posted_at     TEXT NOT NULL,
    found_at      TEXT NOT NULL,
    score         INTEGER NOT NULL,
    matched_json  TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'new',
    note          TEXT NOT NULL DEFAULT '',
    fingerprint   TEXT NOT NULL DEFAULT '',
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_leads_found ON leads(found_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);

CREATE TABLE IF NOT EXISTS seen (
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    seen_at      TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS businesses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    osm_type     TEXT NOT NULL,
    osm_id       INTEGER NOT NULL,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    address      TEXT NOT NULL DEFAULT '',
    phone        TEXT NOT NULL DEFAULT '',
    website      TEXT NOT NULL DEFAULT '',
    messenger    TEXT NOT NULL DEFAULT '',
    social       TEXT NOT NULL DEFAULT '',
    opportunity  INTEGER NOT NULL DEFAULT 0,
    city         TEXT NOT NULL DEFAULT '',
    distance_m   INTEGER NOT NULL DEFAULT 0,
    tags_json    TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'new',
    note         TEXT NOT NULL DEFAULT '',
    found_at     TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (osm_type, osm_id)
);
CREATE INDEX IF NOT EXISTS idx_biz_city ON businesses(city);

CREATE TABLE IF NOT EXISTS monitor_state (
    source         TEXT PRIMARY KEY,
    last_run_at    TEXT,
    last_ok_at     TEXT,
    last_error     TEXT NOT NULL DEFAULT '',
    checked_total  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS repeats (
    fingerprint  TEXT PRIMARY KEY,
    copies       INTEGER NOT NULL,
    first_at     TEXT NOT NULL,
    last_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    query       TEXT PRIMARY KEY,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    label       TEXT NOT NULL,
    cached_at   TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class NewLead:
    source: str
    source_name: str
    external_id: str
    text: str
    link: str
    author: str
    posted_at: str
    score: int
    matched: tuple[str, ...]


@dataclass(frozen=True)
class BusinessRecord:
    osm_type: str
    osm_id: int
    name: str
    category: str
    lat: float
    lon: float
    address: str
    phone: str
    website: str
    messenger: str
    social: str
    opportunity: bool
    city: str
    distance_m: int
    tags: dict[str, str]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        self._migrate_lead_fingerprints()

    def _migrate_lead_fingerprints(self) -> None:
        """Fingerprint leads stored before repeat detection existed, then drop the repeats.

        Runs on every open but only does work for rows without a fingerprint. Leads
        the user has touched (status or note) are never deleted.
        """
        with self.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
            if "fingerprint" not in columns:
                conn.execute("ALTER TABLE leads ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
            if "spam" not in columns:
                conn.execute("ALTER TABLE leads ADD COLUMN spam INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_fingerprint ON leads(fingerprint)")
            pending = conn.execute("SELECT id, text, author FROM leads WHERE fingerprint = ''").fetchall()
            conn.executemany(
                "UPDATE leads SET fingerprint = ? WHERE id = ?",
                [(fingerprint(row["text"], row["author"]), row["id"]) for row in pending],
            )
        if not pending:
            return
        with self.connect() as conn:
            repeats = _repeat_lead_ids(conn)
        if not repeats:
            return
        self._backup_once(f"{self.path.stem}.before-dedupe{self.path.suffix}")
        with self.connect() as conn:
            conn.executemany("DELETE FROM leads WHERE id = ?", [(lead_id,) for lead_id in repeats])
        log.info("Removed %d repeated leads", len(repeats))

    def _backup_once(self, name: str) -> None:
        target = self.path.with_name(name)
        if target.exists():
            return
        with self.connect() as source:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        # SQLite's LIKE only folds ASCII; fold() makes search case-insensitive for Cyrillic too.
        conn.create_function("fold", 1, _fold, deterministic=True)
        conn.create_function("has_fixed_pay", 1, has_fixed_pay, deterministic=True)
        conn.create_function("is_long_term", 1, is_long_term, deterministic=True)
        conn.create_function("is_one_off", 2, is_one_off, deterministic=True)
        conn.create_function("budget_eur", 1, budget_eur, deterministic=True)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- dedup -----------------------------------------------------------

    def is_seen(self, source: str, external_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen WHERE source = ? AND external_id = ?", (source, external_id)
            ).fetchone()
        return row is not None

    def mark_seen(self, source: str, external_id: str) -> bool:
        """Record an item as seen. Returns True if it was new."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO seen (source, external_id, seen_at) VALUES (?, ?, ?)",
                (source, external_id, utc_now()),
            )
        return cur.rowcount == 1

    # ---- leads -----------------------------------------------------------

    def insert_lead(self, lead: NewLead, skip_duplicates: bool = False) -> int | None:
        """Insert a lead; returns its id, or None if it was not stored.

        With skip_duplicates, the same order already stored under any other source
        or id is not stored again but counted as a repeat; enough repeats mark it as
        spam. One insert statement, so two processes cannot race.
        """
        mark = fingerprint(lead.text, lead.author)
        with self.connect() as conn:
            known = conn.execute(
                "SELECT 1 FROM leads WHERE source = ? AND external_id = ?", (lead.source, lead.external_id)
            ).fetchone()
            cur = conn.execute(
                """INSERT OR IGNORE INTO leads
                   (source, source_name, external_id, text, link, author, posted_at, found_at, score, matched_json,
                    fingerprint)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   WHERE NOT ? OR NOT EXISTS (SELECT 1 FROM leads WHERE fingerprint = ?)""",
                (
                    lead.source, lead.source_name, lead.external_id, lead.text, lead.link,
                    lead.author, lead.posted_at, utc_now(), lead.score, json.dumps(list(lead.matched)),
                    mark, skip_duplicates, mark,
                ),
            )
            if cur.rowcount == 1:
                return cur.lastrowid
            if skip_duplicates and not known:
                _count_repeat(conn, mark, lead.posted_at)
            return None

    def list_leads(
        self,
        status: str | None = None,
        query: str | None = None,
        min_score: int = 0,
        source: str | None = None,
        limit: int | None = 200,
        offset: int = 0,
        sort: str = "newest",
        fixed_pay: bool = False,
        long_term: bool = False,
        one_off: bool = False,
        min_budget: float | None = None,
        max_budget: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """limit=None returns every match (CSV export); paged calls are capped. Spam is never listed.

        A budget bound (euros) keeps only leads that name a price.
        """
        if sort not in LEAD_ORDER:
            raise ValueError(f"Unknown sort {sort!r}")
        where, params = ["spam = 0", "score >= ?"], [min_score]
        if min_budget is not None:
            where.append("budget_eur(text) >= ?")
            params.append(min_budget)
        if max_budget is not None:
            where.append("budget_eur(text) <= ?")
            params.append(max_budget)
        if status:
            where.append("status = ?")
            params.append(status)
        if source:
            where.append("source = ?")
            params.append(source)
        if fixed_pay:
            where.append("has_fixed_pay(text)")
        if long_term:
            where.append("is_long_term(text)")
        if one_off:
            where.append("is_one_off(text, source_name)")
        if query:
            where.append(_search_clause(("text", "note", "source_name")))
            params.extend([_like_pattern(query)] * 3)
        clause = " AND ".join(where)
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM leads WHERE {clause}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM leads WHERE {clause} ORDER BY {LEAD_ORDER[sort]} {_page(limit, offset)}",
                params,
            ).fetchall()
        return [_lead_row(r) for r in rows], total

    def update_lead(self, lead_id: int, status: str | None, note: str | None) -> dict[str, Any] | None:
        return self._update_tracking("leads", lead_id, status, note, _lead_row)

    # ---- businesses ------------------------------------------------------

    def upsert_business(self, biz: BusinessRecord) -> bool:
        """Insert or refresh one business. Returns True if it was newly inserted."""
        return self.upsert_businesses([biz]) == 1

    def upsert_businesses(self, records: list[BusinessRecord]) -> int:
        """Insert or refresh OSM facts in ONE transaction; never touches status/note.

        Returns how many businesses were newly inserted.
        """
        now = utc_now()
        with self.connect() as conn:
            return sum(_upsert_one(conn, biz, now) for biz in records)

    def list_businesses(
        self,
        status: str | None = None,
        query: str | None = None,
        opportunity_only: bool = False,
        city: str | None = None,
        limit: int | None = 300,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """limit=None returns every match (CSV export); paged calls are capped."""
        where, params = ["1 = 1"], []
        if status:
            where.append("status = ?")
            params.append(status)
        if opportunity_only:
            where.append("opportunity = 1")
        if city:
            where.append("city = ?")
            params.append(city)
        if query:
            where.append(_search_clause(("name", "address", "note", "category")))
            params.extend([_like_pattern(query)] * 4)
        clause = " AND ".join(where)
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM businesses WHERE {clause}", params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT * FROM businesses WHERE {clause}
                    ORDER BY opportunity DESC, distance_m ASC, id ASC {_page(limit, offset)}""",
                params,
            ).fetchall()
        return [_business_row(r) for r in rows], total

    def business_cities(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT DISTINCT city FROM businesses WHERE city != '' ORDER BY city").fetchall()
        return [r["city"] for r in rows]

    def update_business(self, biz_id: int, status: str | None, note: str | None) -> dict[str, Any] | None:
        return self._update_tracking("businesses", biz_id, status, note, _business_row)

    # ---- stats & monitor state ------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            lead_counts = dict(
                conn.execute("SELECT status, COUNT(*) FROM leads WHERE spam = 0 GROUP BY status").fetchall()
            )
            spam = conn.execute("SELECT COUNT(*) FROM leads WHERE spam = 1").fetchone()[0]
            biz_counts = dict(conn.execute("SELECT status, COUNT(*) FROM businesses GROUP BY status").fetchall())
            opportunities = conn.execute("SELECT COUNT(*) FROM businesses WHERE opportunity = 1").fetchone()[0]
            top_score = conn.execute("SELECT COALESCE(MAX(score), 0) FROM leads WHERE spam = 0").fetchone()[0]
            fixed_pay, long_term, one_off, priced = conn.execute(
                """SELECT COALESCE(SUM(has_fixed_pay(text)), 0), COALESCE(SUM(is_long_term(text)), 0),
                          COALESCE(SUM(is_one_off(text, source_name)), 0),
                          COUNT(budget_eur(text)) FROM leads WHERE spam = 0"""
            ).fetchone()
        return {
            "leads": {s: lead_counts.get(s, 0) for s in STATUSES}
            | {"total": sum(lead_counts.values()), "fixed_pay": fixed_pay, "long_term": long_term, "one_off": one_off,
               "priced": priced, "spam": spam},
            "businesses": {s: biz_counts.get(s, 0) for s in STATUSES}
            | {"total": sum(biz_counts.values()), "opportunities": opportunities},
            "top_score": top_score,
        }

    def record_monitor_run(self, source: str, checked: int, error: str = "", ok: bool | None = None) -> None:
        """`ok` defaults to "no error"; pass ok=True for a partially successful run."""
        now = utc_now()
        ok = (not error) if ok is None else ok
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO monitor_state (source, last_run_at, last_ok_at, last_error, checked_total)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET
                     last_run_at = excluded.last_run_at,
                     last_ok_at = COALESCE(excluded.last_ok_at, monitor_state.last_ok_at),
                     last_error = excluded.last_error,
                     checked_total = monitor_state.checked_total + excluded.checked_total""",
                (source, now, now if ok else None, error[:500], checked),
            )

    def monitor_states(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM monitor_state").fetchall()
        return {r["source"]: dict(r) for r in rows}

    # ---- geocode cache ---------------------------------------------------

    def cached_geocode(self, query: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT lat, lon, label FROM geocode_cache WHERE query = ?", (query,)).fetchone()
        return dict(row) if row else None

    def cache_geocode(self, query: str, lat: float, lon: float, label: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO geocode_cache (query, lat, lon, label, cached_at) VALUES (?, ?, ?, ?, ?)",
                (query, lat, lon, label, utc_now()),
            )

    # ---- helpers ---------------------------------------------------------

    def _update_tracking(self, table, row_id, status, note, mapper):
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        sets, params = [], []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if note is not None:
            sets.append("note = ?")
            params.append(note[:MAX_NOTE_LENGTH])
        with self.connect() as conn:
            if sets:
                conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", [*params, row_id])
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        return mapper(row) if row else None


def _fold(value: object) -> str:
    return str(value).casefold() if value is not None else ""


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_pattern(query: str) -> str:
    return f"%{_escape_like(query.casefold())}%"


def _search_clause(columns: tuple[str, ...]) -> str:
    # Column names are internal constants, never user input.
    return "(" + " OR ".join(f"fold({col}) LIKE ? ESCAPE '\\'" for col in columns) + ")"


def _page(limit: int | None, offset: int) -> str:
    """LIMIT/OFFSET clause from validated ints; None means unbounded (export)."""
    if limit is None:
        return ""
    return f"LIMIT {max(1, min(int(limit), MAX_PAGE_SIZE))} OFFSET {max(0, int(offset))}"


def _upsert_one(conn: sqlite3.Connection, biz: BusinessRecord, now: str) -> bool:
    values = (
        biz.name, biz.category, biz.lat, biz.lon, biz.address, biz.phone, biz.website,
        biz.messenger, biz.social, int(biz.opportunity), biz.city, biz.distance_m,
        json.dumps(biz.tags, ensure_ascii=False), now,
    )
    existing = conn.execute(
        "SELECT id FROM businesses WHERE osm_type = ? AND osm_id = ?", (biz.osm_type, biz.osm_id)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE businesses SET name=?, category=?, lat=?, lon=?, address=?, phone=?, website=?,
               messenger=?, social=?, opportunity=?, city=?, distance_m=?, tags_json=?, updated_at=?
               WHERE id = ?""",
            (*values, existing["id"]),
        )
        return False
    conn.execute(
        """INSERT INTO businesses (name, category, lat, lon, address, phone, website, messenger, social,
           opportunity, city, distance_m, tags_json, updated_at, osm_type, osm_id, found_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (*values, biz.osm_type, biz.osm_id, now),
    )
    return True


def _repeat_lead_ids(conn: sqlite3.Connection) -> list[int]:
    """Ids to delete: per order keep every lead the user touched, or else the first found."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT id, fingerprint, status, note FROM leads ORDER BY id"):
        groups.setdefault(row["fingerprint"], []).append(row)
    repeats: list[int] = []
    for rows in groups.values():
        touched = [row for row in rows if row["status"] != "new" or row["note"]]
        keep = {row["id"] for row in (touched or rows[:1])}
        repeats.extend(row["id"] for row in rows if row["id"] not in keep)
    return repeats


def _count_repeat(conn: sqlite3.Connection, mark: str, posted_at: str) -> None:
    """Count one more copy of a stored order; hide it as spam once the copies say it is an advert."""
    conn.execute(
        """INSERT OR IGNORE INTO repeats (fingerprint, copies, first_at, last_at)
           SELECT fingerprint, 1, posted_at, posted_at FROM leads WHERE fingerprint = ? ORDER BY id LIMIT 1""",
        (mark,),
    )
    conn.execute(
        """UPDATE repeats SET copies = copies + 1, first_at = MIN(first_at, ?), last_at = MAX(last_at, ?)
           WHERE fingerprint = ?""",
        (posted_at, posted_at, mark),
    )
    row = conn.execute(
        "SELECT copies, (julianday(last_at) - julianday(first_at)) * 24 AS span_h FROM repeats WHERE fingerprint = ?",
        (mark,),
    ).fetchone()
    if row is None:
        return
    lasting = row["copies"] >= SPAM_MIN_COPIES and (row["span_h"] or 0) >= SPAM_MIN_SPAN_HOURS
    if lasting or row["copies"] >= SPAM_FLOOD_COPIES:
        # Leads the user already acted on stay visible: a status or a note means a person judged it.
        conn.execute("UPDATE leads SET spam = 1 WHERE fingerprint = ? AND status = 'new' AND note = ''", (mark,))


def _lead_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data.pop("fingerprint", None)  # internal: the API never needs it
    data.pop("spam", None)
    data["budget_eur"] = budget_eur(data["text"])
    data["matched"] = json.loads(data.pop("matched_json") or "[]")
    data["fixed_pay"] = has_fixed_pay(data["text"])
    data["long_term"] = is_long_term(data["text"])
    data["one_off"] = is_one_off(data["text"], data["source_name"])
    return data


def _business_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["opportunity"] = bool(data["opportunity"])
    data.pop("tags_json", None)  # raw tags stay server side; API exposes the derived fields
    return data
