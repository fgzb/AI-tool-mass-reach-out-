"""SQLite storage: companies, e-mails, sends, suppression list, inbox events and agent state."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from .models import Company, EmailCandidate

log = logging.getLogger(__name__)

SENDS_DDL = """
CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    siren TEXT NOT NULL,
    email TEXT NOT NULL,
    campaign TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 1,   -- 1 = first e-mail, 2 = follow-up
    variant TEXT,                      -- A/B template variant
    status TEXT NOT NULL,              -- pending | sent | failed | dry_run
    message_id TEXT, error TEXT, created_at TEXT,
    UNIQUE(email, campaign, step)
);
"""

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
""" + SENDS_DDL + """
CREATE TABLE IF NOT EXISTS suppression (
    value TEXT PRIMARY KEY,        -- an e-mail address or "@domain.fr"
    reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS inbox_events (
    message_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- reply | optout | bounce | auto | other
    from_addr TEXT, subject TEXT, siren TEXT, email TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_state (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS companies_todo ON companies(discovery_done, scrape_done);
CREATE INDEX IF NOT EXISTS emails_siren ON emails(siren);
CREATE INDEX IF NOT EXISTS emails_email ON emails(email);
CREATE INDEX IF NOT EXISTS sends_siren ON sends(siren);
CREATE INDEX IF NOT EXISTS sends_campaign ON sends(campaign, step, status);
CREATE INDEX IF NOT EXISTS sends_status ON sends(status, created_at);
CREATE INDEX IF NOT EXISTS sends_message_id ON sends(message_id);
CREATE INDEX IF NOT EXISTS inbox_siren ON inbox_events(siren);
CREATE INDEX IF NOT EXISTS inbox_from ON inbox_events(from_addr);
"""


def at_domain(col: str) -> str:
    """SQL for an e-mail's domain as "@domain", to match domain-wide suppressions."""
    return f"'@' || substr({col}, instr({col}, '@') + 1)"


# Every exclusion list is computed once (SQLite materialises `x NOT IN (SELECT ...)` into a
# temporary index), instead of correlated sub-queries evaluated for every candidate:
# 77 s -> ~0.1 s on 50 000 companies.
OPTED_OUT_SIRENS = f"""
    SELECT siren FROM inbox_events WHERE kind IN ('reply', 'optout') AND siren IS NOT NULL
    UNION SELECT siren FROM emails
     WHERE email IN (SELECT value FROM suppression WHERE reason != 'bounce')
        OR {at_domain('email')} IN (SELECT value FROM suppression WHERE reason != 'bounce')
"""

RECIPIENTS_SQL = f"""
WITH
candidates AS (
    SELECT e.siren, e.email, e.score,
           ROW_NUMBER() OVER (PARTITION BY e.siren ORDER BY e.score DESC, e.id) AS rn
    FROM emails e
    WHERE (e.mx_ok IS NULL OR e.mx_ok = 1)
      AND e.email NOT IN (SELECT value FROM suppression)
      AND {at_domain('e.email')} NOT IN (SELECT value FROM suppression)
      AND e.email NOT IN (SELECT email FROM sends WHERE campaign = :campaign AND step = 1 AND status = 'failed')
),
handled AS (
    SELECT siren, COUNT(*) AS n FROM sends
    WHERE campaign = :campaign AND step = 1 AND status IN ({{handled}}) GROUP BY siren
)
SELECT c.*, k.email AS email
FROM candidates k
JOIN companies c ON c.siren = k.siren
LEFT JOIN handled h ON h.siren = k.siren
WHERE k.rn <= :per_company
  AND COALESCE(h.n, 0) < :per_company
  -- not already handled in this campaign
  AND k.email NOT IN (SELECT email FROM sends WHERE campaign = :campaign AND step = 1 AND status IN ({{handled}}))
  -- not contacted recently in another campaign
  AND k.siren NOT IN (SELECT siren FROM sends WHERE campaign != :campaign AND status = 'sent'
                      AND created_at >= :recontact_since)
  AND k.email NOT IN (SELECT email FROM sends WHERE campaign != :campaign AND status = 'sent'
                      AND created_at >= :recontact_since)
  -- nobody at this company replied or opted out
  AND k.siren NOT IN ({OPTED_OUT_SIRENS})
  AND k.email NOT IN (SELECT from_addr FROM inbox_events WHERE kind IN ('reply', 'optout') AND from_addr IS NOT NULL)
"""

# Warm-up: the best addresses (company domain, role inbox, verified site) go first, to keep
# bounces minimal while the domain builds its reputation. Afterwards: first collected, first contacted.
ORDER_BEST_FIRST = " ORDER BY k.score DESC, c.website_confidence DESC, c.created_at, c.siren"
ORDER_FIFO = " ORDER BY c.created_at, c.siren, k.rn"

FOLLOWUPS_SQL = f"""
SELECT c.*, s.email AS email, s.message_id AS parent_message_id, s.variant AS variant
FROM sends s JOIN companies c ON c.siren = s.siren
WHERE s.campaign = :campaign AND s.step = 1 AND s.status = 'sent'
  AND s.created_at < :due_before AND s.created_at >= :expire_before
  AND s.email NOT IN (SELECT email FROM sends WHERE campaign = :campaign AND step = 2
                      AND status IN ({{handled}}, 'failed'))
  AND s.siren NOT IN ({OPTED_OUT_SIRENS})
  AND s.email NOT IN (SELECT from_addr FROM inbox_events WHERE kind IN ('reply', 'optout') AND from_addr IS NOT NULL)
  AND s.email NOT IN (SELECT value FROM suppression)
  AND {at_domain('s.email')} NOT IN (SELECT value FROM suppression)
ORDER BY s.created_at
"""


class Store:
    def __init__(self, path: str, clock: Optional[Callable[[], datetime]] = None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # WAL + synchronous=NORMAL: no disk flush on every commit (~20x faster writes), still crash-safe.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA temp_store = MEMORY")
        self.conn.execute("PRAGMA cache_size = -65536")
        self._migrate()
        self.conn.executescript(SCHEMA)
        self._batch_depth = 0

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(sends)")}
        if cols and "step" not in cols:  # v1 schema: add follow-up step + A/B variant
            self.conn.executescript(
                "ALTER TABLE sends RENAME TO sends_v1;" + SENDS_DDL +
                "INSERT INTO sends (id, siren, email, campaign, step, status, message_id, error, created_at) "
                "SELECT id, siren, email, campaign, 1, status, message_id, error, created_at FROM sends_v1;"
                "DROP TABLE sends_v1;"
            )

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    @contextmanager
    def batch(self) -> Iterator["Store"]:
        """Group many writes in one transaction (bulk imports, enrichment batches)."""
        self._batch_depth += 1
        try:
            yield self
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self.conn.commit()

    def _commit(self) -> None:
        if not self._batch_depth:
            self.conn.commit()

    # -- companies ---------------------------------------------------------
    def upsert_company(self, company: Company) -> bool:
        """Insert or refresh a company. Returns True when it was new."""
        row = company.to_row()
        row["extra_json"] = json.dumps(company.extra, ensure_ascii=False, default=str)
        existing = self.conn.execute("SELECT 1 FROM companies WHERE siren = ?", (company.siren,)).fetchone()
        ts = self.now()
        if existing is None:
            row["created_at"] = row["updated_at"] = ts
            cols = ", ".join(row)
            marks = ", ".join(f":{k}" for k in row)
            self.conn.execute(f"INSERT INTO companies ({cols}) VALUES ({marks})", row)
            self._commit()
            return True
        # Never overwrite a website we already found with an empty one.
        if not row["website"]:
            row.pop("website")
            row.pop("website_source")
        row["updated_at"] = ts
        sets = ", ".join(f"{k} = :{k}" for k in row if k != "siren")
        self.conn.execute(f"UPDATE companies SET {sets} WHERE siren = :siren", row)
        self._commit()
        return False

    def upsert_companies(self, companies: Iterable[Company]) -> tuple[int, int]:
        """Bulk version of upsert_company, one transaction per 1 000 rows. Returns (total, new)."""
        total = new = 0
        with self.batch():
            for company in companies:
                total += 1
                new += self.upsert_company(company)
                if total % 1000 == 0:
                    self.conn.commit()
                if total % 10000 == 0:
                    log.info("%d companies imported (%d new)", total, new)
        return total, new

    def companies(self, where: str = "1=1", params: Iterable[Any] = (), limit: Optional[int] = None) -> list[sqlite3.Row]:
        sql = f"SELECT * FROM companies WHERE {where} ORDER BY created_at, siren"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql, tuple(params)))

    def set_website(self, siren: str, url: str, source: str, confidence: int) -> None:
        self.conn.execute(
            "UPDATE companies SET website = ?, website_source = ?, website_confidence = ?, "
            "discovery_done = 1, updated_at = ? WHERE siren = ?",
            (url, source, confidence, self.now(), siren),
        )
        self._commit()

    def mark(self, siren: str, *columns: str) -> None:
        assert columns and set(columns) <= {"discovery_done", "scrape_done"}
        sets = ", ".join(f"{c} = 1" for c in columns)
        self.conn.execute(f"UPDATE companies SET {sets}, updated_at = ? WHERE siren = ?", (self.now(), siren))
        self._commit()

    # -- emails ------------------------------------------------------------
    def add_emails(self, siren: str, candidates: Iterable[EmailCandidate], mx: dict[str, Optional[bool]]) -> int:
        n = 0
        for c in candidates:
            ok = mx.get(c.email.split("@", 1)[1])
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO emails (siren, email, source_url, score, kind, mx_ok, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (siren, c.email, c.source_url, c.score, c.kind, None if ok is None else int(ok), self.now()),
            )
            n += cur.rowcount
        self._commit()
        return n

    # -- recipients ----------------------------------------------------------
    def _recipient_params(self, campaign: str, per_company: int, recontact_days: int) -> dict[str, Any]:
        # recontact_days <= 0 disables the rule ("9999" is later than any timestamp).
        since = (
            (self.clock() - timedelta(days=recontact_days)).astimezone(timezone.utc).isoformat(timespec="seconds")
            if recontact_days > 0 else "9999"
        )
        return {"campaign": campaign, "per_company": per_company, "recontact_since": since}

    @staticmethod
    def _handled(rehearsal: bool) -> str:
        return "'sent', 'pending'" + (", 'dry_run'" if rehearsal else "")

    def recipients(
        self, campaign: str, per_company: int = 1, rehearsal: bool = False, recontact_days: int = 180,
        limit: Optional[int] = None, best_first: bool = False,
    ) -> list[sqlite3.Row]:
        """Contacts that may get a first e-mail now. `rehearsal` also skips dry-run previews."""
        sql = RECIPIENTS_SQL.format(handled=self._handled(rehearsal))
        sql += ORDER_BEST_FIRST if best_first else ORDER_FIFO
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql, self._recipient_params(campaign, per_company, recontact_days)))

    def count_ready(self, campaign: str, per_company: int = 1, rehearsal: bool = False, recontact_days: int = 180) -> int:
        sql = f"SELECT COUNT(*) FROM ({RECIPIENTS_SQL.format(handled=self._handled(rehearsal))})"
        return self.conn.execute(sql, self._recipient_params(campaign, per_company, recontact_days)).fetchone()[0]

    def followups_due(
        self, campaign: str, due_before: str, expire_before: str, rehearsal: bool = False, limit: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        """First e-mails old enough for a follow-up, with no reply, opt-out or bounce since."""
        sql = FOLLOWUPS_SQL.format(handled=self._handled(rehearsal)) + (f" LIMIT {int(limit)}" if limit else "")
        return list(self.conn.execute(
            sql, {"campaign": campaign, "due_before": due_before, "expire_before": expire_before}
        ))

    # -- sends ---------------------------------------------------------------
    def record_send(
        self, siren: str, email: str, campaign: str, status: str, message_id: str = "", error: str = "",
        step: int = 1, variant: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO sends (siren, email, campaign, step, variant, status, message_id, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(email, campaign, step) DO UPDATE SET status = excluded.status, variant = excluded.variant, "
            "message_id = excluded.message_id, error = excluded.error, created_at = excluded.created_at",
            (siren, email.lower(), campaign, step, variant, status, message_id, error, self.now()),
        )
        self._commit()

    def delete_send(self, email: str, campaign: str, step: int = 1) -> None:
        self.conn.execute(
            "DELETE FROM sends WHERE email = ? AND campaign = ? AND step = ?", (email.lower(), campaign, step)
        )
        self._commit()

    def count_sends_since(self, iso_ts: str, statuses: tuple[str, ...] = ("sent",), step: Optional[int] = None) -> int:
        marks = ", ".join("?" for _ in statuses)
        sql = f"SELECT COUNT(*) FROM sends WHERE status IN ({marks}) AND created_at >= ?"
        params: list[Any] = [*statuses, iso_ts]
        if step is not None:
            sql += " AND step = ?"
            params.append(step)
        return self.conn.execute(sql, params).fetchone()[0]

    def sending_days_before(self, iso_ts: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT substr(created_at, 1, 10)) FROM sends WHERE status = 'sent' AND created_at < ?",
            (iso_ts,),
        ).fetchone()[0]

    def clear_dry_runs(self, campaign: str) -> None:
        self.conn.execute("DELETE FROM sends WHERE campaign = ? AND status = 'dry_run'", (campaign,))
        self._commit()

    def send_by_message_ids(self, message_ids: Iterable[str]) -> Optional[sqlite3.Row]:
        ids = [m for m in message_ids if m]
        if not ids:
            return None
        marks = ", ".join("?" for _ in ids)
        return self.conn.execute(
            f"SELECT * FROM sends WHERE message_id IN ({marks}) AND status = 'sent' LIMIT 1", ids
        ).fetchone()

    def send_by_address(self, address: str, domain_fallback: bool = True) -> Optional[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT * FROM sends WHERE email = ? AND status = 'sent' ORDER BY id DESC LIMIT 1", (address.lower(),)
        ).fetchone()
        if row is None and domain_fallback:
            row = self.conn.execute(
                "SELECT * FROM sends WHERE email LIKE ? AND status = 'sent' ORDER BY id DESC LIMIT 1",
                ("%@" + address.lower().split("@", 1)[-1],),
            ).fetchone()
        return row

    def variant_stats(self, campaign: str, since_iso: str) -> list[sqlite3.Row]:
        """First e-mails sent per template variant, and how many of those companies replied / opted out."""
        return list(self.conn.execute(
            "SELECT COALESCE(s.variant, '-') AS variant, COUNT(*) AS sent, "
            "SUM(EXISTS (SELECT 1 FROM inbox_events r WHERE r.siren = s.siren AND r.kind = 'reply')) AS replies, "
            "SUM(EXISTS (SELECT 1 FROM inbox_events r WHERE r.siren = s.siren AND r.kind = 'optout')) AS optouts "
            "FROM sends s WHERE s.campaign = ? AND s.step = 1 AND s.status = 'sent' AND s.created_at >= ? "
            "GROUP BY s.variant ORDER BY s.variant",
            (campaign, since_iso),
        ))

    # -- suppression (opt-out / bounces) ------------------------------------
    def suppress(self, value: str, reason: str = "manual") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO suppression (value, reason, created_at) VALUES (?, ?, ?)",
            (value.strip().lower(), reason, self.now()),
        )
        self._commit()

    def is_suppressed(self, email: str) -> bool:
        email = email.lower()
        domain = "@" + email.split("@", 1)[-1]
        return (
            self.conn.execute("SELECT 1 FROM suppression WHERE value IN (?, ?)", (email, domain)).fetchone()
            is not None
        )

    # -- inbox ---------------------------------------------------------------
    def has_inbox_event(self, message_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM inbox_events WHERE message_id = ?", (message_id,)).fetchone() is not None

    def record_inbox_event(
        self, message_id: str, kind: str, from_addr: str = "", subject: str = "", siren: str = "", email: str = ""
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO inbox_events (message_id, kind, from_addr, subject, siren, email, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, kind, from_addr.lower() or None, subject or None, siren or None, email or None, self.now()),
        )
        self._commit()

    def inbox_events_since(self, iso_ts: str, kinds: tuple[str, ...]) -> list[sqlite3.Row]:
        marks = ", ".join("?" for _ in kinds)
        return list(
            self.conn.execute(
                "SELECT i.*, c.name AS company_name FROM inbox_events i LEFT JOIN companies c ON c.siren = i.siren "
                f"WHERE i.kind IN ({marks}) AND i.created_at >= ? ORDER BY i.created_at",
                (*kinds, iso_ts),
            )
        )

    # -- deliverability health --------------------------------------------------
    def health(self, since_iso: str) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql, (since_iso,)).fetchone()[0]  # noqa: E731
        return {
            "sent": q("SELECT COUNT(*) FROM sends WHERE status = 'sent' AND created_at >= ?"),
            "failed": q("SELECT COUNT(*) FROM sends WHERE status = 'failed' AND created_at >= ?"),
            # Asynchronous bounces (DSN e-mails) for messages the SMTP server had accepted.
            "bounced": q(
                "SELECT COUNT(*) FROM suppression x WHERE x.reason = 'bounce' AND x.created_at >= ? "
                "AND x.value IN (SELECT email FROM sends WHERE status = 'sent')"
            ),
            "optouts": q("SELECT COUNT(*) FROM inbox_events WHERE kind = 'optout' AND siren IS NOT NULL AND created_at >= ?"),
            "replies": q("SELECT COUNT(*) FROM inbox_events WHERE kind = 'reply' AND created_at >= ?"),
        }

    # -- agent state ------------------------------------------------------------
    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM agent_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO agent_state (key, value) VALUES (?, ?)", (key, str(value)))
        self._commit()

    def del_state(self, key: str) -> None:
        self.conn.execute("DELETE FROM agent_state WHERE key = ?", (key,))
        self._commit()

    def states(self, prefix: str) -> dict[str, str]:
        return dict(self.conn.execute("SELECT key, value FROM agent_state WHERE key LIKE ?", (prefix + "%",)).fetchall())

    def try_lock(self, owner: str, stale_seconds: int = 600) -> bool:
        """Single-agent lock (also refreshes our own lock). False if another live agent holds it."""
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT value FROM agent_state WHERE key = 'lock'").fetchone()
            if row:
                holder, _, ts = row[0].rpartition("|")
                if holder != owner and time.time() - float(ts or 0) < stale_seconds:
                    self.conn.rollback()
                    return False
            self.conn.execute(
                "INSERT OR REPLACE INTO agent_state (key, value) VALUES ('lock', ?)", (f"{owner}|{time.time()}",)
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def release_lock(self, owner: str) -> None:
        row = self.get_state("lock")
        if row and row.rpartition("|")[0] == owner:
            self.del_state("lock")

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
            "sent": q("SELECT COUNT(*) FROM sends WHERE status = 'sent' AND step = 1"),
            "followups_sent": q("SELECT COUNT(*) FROM sends WHERE status = 'sent' AND step = 2"),
            "dry_run": q("SELECT COUNT(*) FROM sends WHERE status = 'dry_run'"),
            "failed": q("SELECT COUNT(*) FROM sends WHERE status = 'failed'"),
            "replies": q("SELECT COUNT(*) FROM inbox_events WHERE kind = 'reply'"),
            "optouts": q("SELECT COUNT(*) FROM inbox_events WHERE kind = 'optout'"),
            "suppressed": q("SELECT COUNT(*) FROM suppression"),
        }
