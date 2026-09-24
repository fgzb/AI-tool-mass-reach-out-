"""SQLite storage: companies, discovered e-mails, sends and the suppression list."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .models import Company, EmailCandidate

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    siren TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    legal_name TEXT, sigle TEXT, naf TEXT, category TEXT, headcount_band TEXT,
    creation_date TEXT, address TEXT, postal_code TEXT, city TEXT,
    department TEXT, region TEXT, is_individual INTEGER DEFAULT 0,
    director_first_name TEXT, director_last_name TEXT, director_role TEXT,
    revenue INTEGER, net_income INTEGER, finances_year TEXT,
    website TEXT, website_source TEXT, website_confidence INTEGER,
    source TEXT, extra_json TEXT,
    discovery_done INTEGER DEFAULT 0,
    scrape_done INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    siren TEXT NOT NULL REFERENCES companies(siren),
    email TEXT NOT NULL,
    source_url TEXT, score INTEGER, kind TEXT, mx_ok INTEGER,
    created_at TEXT,
    UNIQUE(siren, email)
);
CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    siren TEXT NOT NULL,
    email TEXT NOT NULL,
    campaign TEXT NOT NULL,
    status TEXT NOT NULL,          -- sent | dry_run | failed
    message_id TEXT, error TEXT, created_at TEXT,
    UNIQUE(email, campaign)
);
CREATE TABLE IF NOT EXISTS suppression (
    value TEXT PRIMARY KEY,        -- an e-mail address or "@domain.fr"
    reason TEXT, created_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- companies ---------------------------------------------------------
    def upsert_company(self, company: Company) -> bool:
        """Insert or refresh a company. Returns True when it was new."""
        row = company.to_row()
        row["extra_json"] = json.dumps(company.extra, ensure_ascii=False, default=str)
        existing = self.conn.execute(
            "SELECT website FROM companies WHERE siren = ?", (company.siren,)
        ).fetchone()
        ts = now()
        if existing is None:
            row["created_at"] = row["updated_at"] = ts
            cols = ", ".join(row)
            marks = ", ".join(f":{k}" for k in row)
            self.conn.execute(f"INSERT INTO companies ({cols}) VALUES ({marks})", row)
            self.conn.commit()
            return True
        # Never overwrite a website we already found with an empty one.
        if not row["website"]:
            row.pop("website")
            row.pop("website_source")
        row["updated_at"] = ts
        sets = ", ".join(f"{k} = :{k}" for k in row if k != "siren")
        self.conn.execute(f"UPDATE companies SET {sets} WHERE siren = :siren", row)
        self.conn.commit()
        return False

    def companies(self, where: str = "1=1", params: Iterable[Any] = (), limit: Optional[int] = None) -> list[sqlite3.Row]:
        sql = f"SELECT * FROM companies WHERE {where} ORDER BY created_at, siren"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql, tuple(params)))

    def set_website(self, siren: str, url: str, source: str, confidence: int) -> None:
        self.conn.execute(
            "UPDATE companies SET website = ?, website_source = ?, website_confidence = ?, "
            "discovery_done = 1, updated_at = ? WHERE siren = ?",
            (url, source, confidence, now(), siren),
        )
        self.conn.commit()

    def mark(self, siren: str, column: str) -> None:
        assert column in {"discovery_done", "scrape_done"}
        self.conn.execute(f"UPDATE companies SET {column} = 1, updated_at = ? WHERE siren = ?", (now(), siren))
        self.conn.commit()

    # -- emails ------------------------------------------------------------
    def add_emails(self, siren: str, candidates: Iterable[EmailCandidate], mx: dict[str, Optional[bool]]) -> int:
        n = 0
        for c in candidates:
            domain = c.email.split("@", 1)[1]
            ok = mx.get(domain)
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO emails (siren, email, source_url, score, kind, mx_ok, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (siren, c.email, c.source_url, c.score, c.kind, None if ok is None else int(ok), now()),
            )
            n += cur.rowcount
        self.conn.commit()
        return n

    def best_emails(self, siren: str, limit: int = 1) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM emails WHERE siren = ? AND (mx_ok IS NULL OR mx_ok = 1) "
                "ORDER BY score DESC, id LIMIT ?",
                (siren, limit),
            )
        )

    # -- sending -----------------------------------------------------------
    def pending_recipients(self, campaign: str, per_company: int = 1) -> Iterator[tuple[sqlite3.Row, sqlite3.Row]]:
        """Yield (company, email) pairs not yet contacted in this campaign."""
        for company in self.companies("EXISTS (SELECT 1 FROM emails e WHERE e.siren = companies.siren)"):
            if self.conn.execute(
                "SELECT 1 FROM sends WHERE siren = ? AND campaign = ? AND status IN ('sent', 'dry_run')",
                (company["siren"], campaign),
            ).fetchone():
                continue
            for email in self.best_emails(company["siren"], per_company):
                if self.is_suppressed(email["email"]):
                    continue
                if self.already_contacted(email["email"], campaign):
                    continue
                yield company, email

    def already_contacted(self, email: str, campaign: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM sends WHERE email = ? AND campaign = ? AND status IN ('sent', 'dry_run')",
                (email.lower(), campaign),
            ).fetchone()
            is not None
        )

    def record_send(self, siren: str, email: str, campaign: str, status: str, message_id: str = "", error: str = "") -> None:
        self.conn.execute(
            "INSERT INTO sends (siren, email, campaign, status, message_id, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(email, campaign) DO UPDATE SET status = excluded.status, "
            "message_id = excluded.message_id, error = excluded.error, created_at = excluded.created_at",
            (siren, email.lower(), campaign, status, message_id, error, now()),
        )
        self.conn.commit()

    def sent_since(self, iso_ts: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM sends WHERE status = 'sent' AND created_at >= ?", (iso_ts,)
        ).fetchone()[0]

    def clear_dry_runs(self, campaign: str) -> None:
        self.conn.execute("DELETE FROM sends WHERE campaign = ? AND status = 'dry_run'", (campaign,))
        self.conn.commit()

    # -- suppression (opt-out / bounces) ------------------------------------
    def suppress(self, value: str, reason: str = "manual") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO suppression (value, reason, created_at) VALUES (?, ?, ?)",
            (value.strip().lower(), reason, now()),
        )
        self.conn.commit()

    def is_suppressed(self, email: str) -> bool:
        email = email.lower()
        domain = "@" + email.split("@", 1)[-1]
        return (
            self.conn.execute("SELECT 1 FROM suppression WHERE value IN (?, ?)", (email, domain)).fetchone()
            is not None
        )

    # -- reporting ---------------------------------------------------------
    def stats(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "companies": q("SELECT COUNT(*) FROM companies"),
            "with_website": q("SELECT COUNT(*) FROM companies WHERE website IS NOT NULL AND website != ''"),
            "discovery_pending": q("SELECT COUNT(*) FROM companies WHERE discovery_done = 0"),
            "scrape_pending": q(
                "SELECT COUNT(*) FROM companies WHERE scrape_done = 0 AND website IS NOT NULL AND website != ''"
            ),
            "with_email": q("SELECT COUNT(DISTINCT siren) FROM emails"),
            "emails": q("SELECT COUNT(*) FROM emails"),
            "sent": q("SELECT COUNT(*) FROM sends WHERE status = 'sent'"),
            "dry_run": q("SELECT COUNT(*) FROM sends WHERE status = 'dry_run'"),
            "failed": q("SELECT COUNT(*) FROM sends WHERE status = 'failed'"),
            "suppressed": q("SELECT COUNT(*) FROM suppression"),
        }
