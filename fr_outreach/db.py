"""SQLite storage: companies, e-mails, sends, suppression list, inbox events and agent state."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

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
    status TEXT NOT NULL,          -- pending | sent | failed | dry_run
    message_id TEXT, error TEXT, created_at TEXT,
    UNIQUE(email, campaign)
);
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
CREATE INDEX IF NOT EXISTS emails_siren ON emails(siren);
CREATE INDEX IF NOT EXISTS sends_siren ON sends(siren);
CREATE INDEX IF NOT EXISTS sends_status ON sends(status, created_at);
CREATE INDEX IF NOT EXISTS sends_message_id ON sends(message_id);
CREATE INDEX IF NOT EXISTS inbox_siren ON inbox_events(siren);
CREATE INDEX IF NOT EXISTS inbox_from ON inbox_events(from_addr);
"""

# The e-mail's domain as "@domain", to match domain-wide suppressions.
AT_DOMAIN = "'@' || substr({col}, instr({col}, '@') + 1)"

RECIPIENTS_SQL = """
WITH candidates AS (
    SELECT e.id AS email_id, e.siren, e.email,
           ROW_NUMBER() OVER (PARTITION BY e.siren ORDER BY e.score DESC, e.id) AS rn
    FROM emails e
    WHERE (e.mx_ok IS NULL OR e.mx_ok = 1)
      AND NOT EXISTS (SELECT 1 FROM suppression x WHERE x.value IN (e.email, {at_e}))
      AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.campaign = :campaign AND s.email = e.email
                      AND s.status = 'failed')
)
SELECT c.*, k.email AS email
FROM candidates k JOIN companies c ON c.siren = k.siren
WHERE k.rn <= :per_company
  -- not already handled in this campaign
  AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.campaign = :campaign AND s.email = k.email
                  AND s.status IN ({handled}))
  AND (SELECT COUNT(*) FROM sends s WHERE s.campaign = :campaign AND s.siren = k.siren
       AND s.status IN ({handled})) < :per_company
  -- not contacted recently in another campaign
  AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.campaign != :campaign AND s.status = 'sent'
                  AND s.created_at >= :recontact_since AND (s.siren = k.siren OR s.email = k.email))
  -- nobody at this company replied or opted out
  AND NOT EXISTS (SELECT 1 FROM inbox_events r WHERE r.kind IN ('reply', 'optout')
                  AND (r.siren = k.siren OR r.from_addr = k.email))
  AND NOT EXISTS (SELECT 1 FROM emails e2 JOIN suppression x ON x.value IN (e2.email, {at_e2})
                  WHERE e2.siren = k.siren AND x.reason != 'bounce')
ORDER BY c.created_at, c.siren, k.rn
"""


class Store:
    def __init__(self, path: str, clock: Optional[Callable[[], datetime]] = None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

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
            (url, source, confidence, self.now(), siren),
        )
        self.conn.commit()

    def mark(self, siren: str, column: str) -> None:
        assert column in {"discovery_done", "scrape_done"}
        self.conn.execute(f"UPDATE companies SET {column} = 1, updated_at = ? WHERE siren = ?", (self.now(), siren))
        self.conn.commit()

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
        self.conn.commit()
        return n

    # -- recipients ----------------------------------------------------------
    def _recipients_sql(self, rehearsal: bool) -> str:
        handled = "'sent', 'pending'" + (", 'dry_run'" if rehearsal else "")
        return RECIPIENTS_SQL.format(
            handled=handled, at_e=AT_DOMAIN.format(col="e.email"), at_e2=AT_DOMAIN.format(col="e2.email")
        )

    def _recipient_params(self, campaign: str, per_company: int, recontact_days: int) -> dict[str, Any]:
        # recontact_days <= 0 disables the rule ("9999" is later than any timestamp).
        since = (
            (self.clock() - timedelta(days=recontact_days)).astimezone(timezone.utc).isoformat(timespec="seconds")
            if recontact_days > 0 else "9999"
        )
        return {"campaign": campaign, "per_company": per_company, "recontact_since": since}

    def recipients(
        self, campaign: str, per_company: int = 1, rehearsal: bool = False, recontact_days: int = 180,
        limit: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        """Contacts that may be e-mailed now, best first. `rehearsal` also skips dry-run previews."""
        sql = self._recipients_sql(rehearsal) + (f" LIMIT {int(limit)}" if limit else "")
        return list(self.conn.execute(sql, self._recipient_params(campaign, per_company, recontact_days)))

    def count_ready(self, campaign: str, per_company: int = 1, rehearsal: bool = False, recontact_days: int = 180) -> int:
        sql = f"SELECT COUNT(*) FROM ({self._recipients_sql(rehearsal)})"
        return self.conn.execute(sql, self._recipient_params(campaign, per_company, recontact_days)).fetchone()[0]

    # -- sends ---------------------------------------------------------------
    def record_send(self, siren: str, email: str, campaign: str, status: str, message_id: str = "", error: str = "") -> None:
        self.conn.execute(
            "INSERT INTO sends (siren, email, campaign, status, message_id, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(email, campaign) DO UPDATE SET status = excluded.status, "
            "message_id = excluded.message_id, error = excluded.error, created_at = excluded.created_at",
            (siren, email.lower(), campaign, status, message_id, error, self.now()),
        )
        self.conn.commit()

    def delete_send(self, email: str, campaign: str) -> None:
        self.conn.execute("DELETE FROM sends WHERE email = ? AND campaign = ?", (email.lower(), campaign))
        self.conn.commit()

    def count_sends_since(self, iso_ts: str, statuses: tuple[str, ...] = ("sent",)) -> int:
        marks = ", ".join("?" for _ in statuses)
        return self.conn.execute(
            f"SELECT COUNT(*) FROM sends WHERE status IN ({marks}) AND created_at >= ?", (*statuses, iso_ts)
        ).fetchone()[0]

    def sending_days_before(self, iso_ts: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT substr(created_at, 1, 10)) FROM sends WHERE status = 'sent' AND created_at < ?",
            (iso_ts,),
        ).fetchone()[0]

    def clear_dry_runs(self, campaign: str) -> None:
        self.conn.execute("DELETE FROM sends WHERE campaign = ? AND status = 'dry_run'", (campaign,))
        self.conn.commit()

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

    # -- suppression (opt-out / bounces) ------------------------------------
    def suppress(self, value: str, reason: str = "manual") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO suppression (value, reason, created_at) VALUES (?, ?, ?)",
            (value.strip().lower(), reason, self.now()),
        )
        self.conn.commit()

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
        self.conn.commit()

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
                "AND EXISTS (SELECT 1 FROM sends s WHERE s.email = x.value AND s.status = 'sent')"
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
        self.conn.commit()

    def del_state(self, key: str) -> None:
        self.conn.execute("DELETE FROM agent_state WHERE key = ?", (key,))
        self.conn.commit()

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
            "sent": q("SELECT COUNT(*) FROM sends WHERE status = 'sent'"),
            "dry_run": q("SELECT COUNT(*) FROM sends WHERE status = 'dry_run'"),
            "failed": q("SELECT COUNT(*) FROM sends WHERE status = 'failed'"),
            "replies": q("SELECT COUNT(*) FROM inbox_events WHERE kind = 'reply'"),
            "optouts": q("SELECT COUNT(*) FROM inbox_events WHERE kind = 'optout'"),
            "suppressed": q("SELECT COUNT(*) FROM suppression"),
        }
