"""Autonomous mode: `fr-outreach agent` runs the whole pipeline on its own.

Every tick (default: once a minute) the agent
  1. reads the reply mailbox (every `inbox_sync_minutes`): opt-outs, bounces, replies;
  2. checks deliverability health and pauses itself if bounce / opt-out rates are too high;
  3. sends at most one e-mail, and only inside the sending window (weekdays, office hours,
     no French public holidays), spacing messages so the daily quota is spread over the day;
     the daily quota ramps up progressively (warm-up) up to `mail.max_per_day`;
  4. e-mails you a daily report after the window closes (and an alert if it pauses);
  5. tops up the stock of ready-to-send contacts: scrape -> discover -> collect the next
     page of the registry search, a small batch at a time.
Without `--live` it rehearses: same schedule, but messages are written to the outbox as .eml.
"""
from __future__ import annotations

import logging
import os
import random
import re
import signal
import smtplib
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any, Callable, Optional

from .db import Store
from .inbox import sync_inbox
from .mailer import Campaign, ComplianceError, SendBlocked, SmtpSender
from .pipeline import collect_incremental, discover_batch, scrape_batch, search_exhausted
from .schedule import iso

log = logging.getLogger(__name__)

LOCK_STALE_SECONDS = 1800


class AgentLocked(RuntimeError):
    pass


def pause(store: Store, reason: str) -> None:
    store.set_state("paused", reason)
    store.set_state("paused_at", store.now())


def resume(store: Store) -> None:
    for key in ("paused", "paused_at", "smtp_errors"):
        store.del_state(key)
    # Judge health again from now on, not on the messages that caused the pause.
    store.set_state("health_reset_at", store.now())


def _parse(ts: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(ts) if ts else None


class Agent:
    def __init__(
        self,
        store: Store,
        cfg: dict[str, Any],
        live: bool = False,
        sender_factory: Callable[[dict[str, Any]], Any] = SmtpSender,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        source: Optional[Any] = None,
        fetcher: Optional[Any] = None,
        imap_connect: Optional[Callable[[dict[str, Any]], Any]] = None,
    ):
        self.store = store
        self.cfg = cfg
        self.live = live
        self.campaign = Campaign(store, cfg, live)
        self.schedule = self.campaign.schedule
        self.acfg = cfg["agent"]
        self.mail = cfg["mail"]
        self.sender_factory = sender_factory
        self.clock = clock
        self.sleep = sleep
        self.source = source
        self.fetcher = fetcher
        self.imap_connect = imap_connect
        self.owner = f"{socket.gethostname()}:{os.getpid()}"
        self._last_idle_log = 0.0

    # -- checks -------------------------------------------------------------------
    def preflight(self) -> list[str]:
        """Raise ComplianceError on blockers, return warnings."""
        if self.live:
            if not self.mail["smtp"].get("host"):
                raise ComplianceError("mail.smtp.host is required to send")
            if not (self.mail["imap"].get("host") or self.imap_connect):
                raise ComplianceError(
                    "mail.imap.host is required in live mode: opt-outs and bounces must be processed automatically"
                )
        warnings = []
        if not self.acfg.get("report_to"):
            warnings.append("agent.report_to is empty: no daily report or pause alert will be e-mailed")
        if "example.com" in self.cfg["http"]["user_agent"]:
            warnings.append("http.user_agent still contains example.com: put a real contact in it")
        return warnings

    # -- one iteration -------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        if not self.store.try_lock(self.owner, LOCK_STALE_SECONDS):
            raise AgentLocked("another agent is already running on this database")
        now = self.clock()
        result: dict[str, Any] = {"inbox": self.maybe_sync_inbox(now)}
        result["paused"] = self.check_health(now)
        result["sent"] = 0 if result["paused"] else self.maybe_send(now)
        result["report"] = self.maybe_report(now)
        result["refill"] = self.refill()
        return result

    def maybe_sync_inbox(self, now: datetime) -> Optional[dict[str, int]]:
        if not (self.mail["imap"].get("host") or self.imap_connect):
            return None
        last = _parse(self.store.get_state("inbox:last_sync"))
        if last and now - last < timedelta(minutes=float(self.acfg["inbox_sync_minutes"])):
            return None
        self.store.set_state("inbox:last_sync", iso(now))
        own = frozenset(a.lower() for a in (self.mail.get("from_address"), self.acfg.get("report_to")) if a)
        try:
            counts = sync_inbox(self.store, self.mail["imap"], connect=self.imap_connect, own_addresses=own)
        except Exception as exc:
            log.warning("inbox sync failed: %s", exc)
            last_ok = _parse(self.store.get_state("inbox:last_ok"))
            # Opt-outs must be honoured: never keep sending blind to the replies.
            if self.live and (last_ok is None or now - last_ok > timedelta(hours=24)):
                self.pause(f"cannot read the reply mailbox, so opt-outs cannot be processed: {exc}")
            return {"error": 1}
        self.store.set_state("inbox:last_ok", iso(now))
        if counts["reply"] or counts["optout"] or counts["bounce"]:
            log.info("inbox: %d replies, %d opt-outs, %d bounces", counts["reply"], counts["optout"], counts["bounce"])
        return counts

    def check_health(self, now: datetime) -> Optional[str]:
        paused = self.store.get_state("paused")
        if paused:
            return paused
        since = now - timedelta(days=float(self.acfg["health_window_days"]))
        reset = _parse(self.store.get_state("health_reset_at"))
        if reset and reset > since:
            since = reset
        h = self.store.health(iso(since))
        min_sample = int(self.acfg["health_min_sample"])
        attempts = h["sent"] + h["failed"]
        reason = None
        if attempts >= min_sample and (h["failed"] + h["bounced"]) / attempts > float(self.acfg["max_bounce_rate"]):
            rate = (h["failed"] + h["bounced"]) / attempts
            reason = (
                f"bounce rate {rate:.1%} over the last {attempts} messages (limit {float(self.acfg['max_bounce_rate']):.0%}). "
                "Too many invalid addresses hurt the domain's reputation: check the e-mail sources."
            )
        elif h["sent"] >= min_sample and h["optouts"] / h["sent"] > float(self.acfg["max_optout_rate"]):
            rate = h["optouts"] / h["sent"]
            reason = (
                f"opt-out rate {rate:.1%} over the last {h['sent']} messages (limit {float(self.acfg['max_optout_rate']):.0%}). "
                "The targeting or the message is off: spam complaints usually follow."
            )
        if reason:
            self.pause(reason)
        return reason

    def pause(self, reason: str) -> None:
        pause(self.store, reason)
        log.error("AGENT PAUSED: %s", reason)
        self.notify(
            "[fr-outreach] Agent paused",
            f"The outreach agent paused itself:\n\n{reason}\n\n"
            "Nothing will be sent until you run:  fr-outreach resume\n\n" + self.report_text(self.clock()),
        )

    def maybe_send(self, now: datetime) -> int:
        if not self.schedule.in_window(now):
            return 0
        quota = self.campaign.quota_today(now)
        if self.campaign.done_today(now) >= quota:
            return 0
        interval = self.schedule.send_interval(quota)
        next_at = _parse(self.store.get_state("next_send_at"))
        window_start, _ = self.schedule.window(self.schedule.local(now).date())
        if next_at is None or next_at < window_start:
            # First message of the day: start at a random point of the first interval.
            next_at = window_start + interval * random.random()
            self.store.set_state("next_send_at", iso(next_at))
        if now < next_at:
            return 0
        recipients = self.campaign.recipients(limit=1)
        if not recipients:
            if time.monotonic() - self._last_idle_log > 3600:
                log.info("No contact ready to e-mail yet (the pipeline is filling up).")
                self._last_idle_log = time.monotonic()
            return 0
        company = recipients[0]
        if self.live:
            try:
                with self.sender_factory(self.mail["smtp"]) as sender:
                    self.campaign.deliver(sender, company, company["email"])
                self.store.del_state("smtp_errors")
            except SendBlocked as exc:
                self.pause(f"the mail provider refused the account: {exc}")
                return 0
            except (smtplib.SMTPException, OSError) as exc:
                errors = int(self.store.get_state("smtp_errors", "0") or 0) + 1
                self.store.set_state("smtp_errors", errors)
                log.warning("SMTP error (%d in a row): %s", errors, exc)
                if errors >= int(self.acfg["max_consecutive_smtp_errors"]):
                    self.pause(f"{errors} SMTP errors in a row, last one: {exc}")
                self.store.set_state("next_send_at", iso(now + timedelta(minutes=5)))
                return 0
        else:
            self.campaign.preview(company, company["email"])
            log.info("[rehearsal] would send to %s (%s)", company["email"], company["name"])
        self.store.set_state("next_send_at", iso(now + interval * random.uniform(0.7, 1.3)))
        return 1

    # -- reporting ------------------------------------------------------------------
    def maybe_report(self, now: datetime) -> bool:
        local = self.schedule.local(now)
        today = local.date()
        if not self.schedule.is_sending_day(today) or local < self.schedule.window(today)[1]:
            return False
        if self.store.get_state("report:last") == today.isoformat():
            return False
        self.store.set_state("report:last", today.isoformat())
        self.notify(f"[fr-outreach] Daily report {today.isoformat()}", self.report_text(now))
        return True

    def report_text(self, now: datetime) -> str:
        day_start = iso(self.schedule.day_start_utc(now))
        week = iso(now - timedelta(days=7))
        h = self.store.health(week)
        attempts = h["sent"] + h["failed"]
        replies = self.store.inbox_events_since(day_start, ("reply",))
        stats = self.store.stats()
        lines = [
            f"Mode: {'LIVE' if self.live else 'rehearsal (nothing is really sent)'}",
            f"Status: {'PAUSED - ' + self.store.get_state('paused', '') if self.store.get_state('paused') else 'running'}",
            "",
            "Today",
            f"  sent: {self.store.count_sends_since(day_start, ('sent',))}"
            f" / quota {self.campaign.quota_today(now)} (failed {self.store.count_sends_since(day_start, ('failed',))},"
            f" previews {self.store.count_sends_since(day_start, ('dry_run',))})",
            f"  replies: {len(replies)}, opt-outs: {len(self.store.inbox_events_since(day_start, ('optout',)))},"
            f" bounces: {len(self.store.inbox_events_since(day_start, ('bounce',)))}",
        ]
        lines += [f"    - {r['from_addr']} ({r['company_name'] or r['siren']}): {r['subject']}" for r in replies]
        lines += [
            "",
            "Last 7 days",
            f"  sent: {h['sent']}, replies: {h['replies']}, opt-outs: {h['optouts']},"
            f" bounces/rejections: {h['failed'] + h['bounced']}"
            + (f" ({(h['failed'] + h['bounced']) / attempts:.1%})" if attempts else ""),
            "",
            "Pipeline",
            f"  companies: {stats['companies']}, with website: {stats['with_website']},"
            f" with e-mail: {stats['with_email']}, ready to contact: {self.campaign.ready()}",
            f"  registry search exhausted: {'yes - widen the search filters' if self.store.get_state('search_exhausted') else 'no'}",
        ]
        return "\n".join(lines) + "\n"

    def notify(self, subject: str, body: str) -> None:
        log.info("%s\n%s", subject, body)
        reports = Path(self.mail.get("outbox_dir") or "outbox") / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:40]
        (reports / f"{self.clock():%Y-%m-%d_%H%M%S}_{slug}.txt").write_text(f"{subject}\n\n{body}", encoding="utf-8")
        to = self.acfg.get("report_to")
        if not (to and self.mail["smtp"].get("host")):
            return
        msg = EmailMessage()
        msg["From"] = formataddr((self.mail.get("from_name") or "fr-outreach", self.mail["from_address"]))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.mail["from_address"].split("@")[-1])
        msg.set_content(body)
        try:
            with self.sender_factory(self.mail["smtp"]) as sender:
                sender.send(msg)
        except Exception as exc:
            log.warning("could not e-mail the report to %s: %s", to, exc)

    # -- keeping the pipeline full ------------------------------------------------------
    def refill(self) -> Optional[dict[str, int]]:
        if self.campaign.ready() >= int(self.acfg["ready_buffer"]):
            return None
        with_email, n = scrape_batch(self.store, self.cfg, int(self.acfg["scrape_batch"]), self.fetcher)
        if n:
            return {"scraped": n, "with_email": with_email}
        found, n = discover_batch(self.store, self.cfg, int(self.acfg["discover_batch"]), self.fetcher)
        if n:
            return {"discovered": n, "websites": found}
        if self.store.get_state("search_exhausted"):
            return None
        new = collect_incremental(self.store, self.cfg, int(self.acfg["collect_batch"]), self.source)
        if new == 0 and search_exhausted(self.store, self.cfg, self.source):
            self.store.set_state("search_exhausted", iso(self.clock()))
            self.notify(
                "[fr-outreach] Registry search exhausted",
                "Every company matching the search filters has been collected. Widen `search` in config.yaml "
                "(more departments, NAF codes...) to keep the agent supplied.\n",
            )
        return {"collected": new}

    # -- main loop ---------------------------------------------------------------------
    def run(self, once: bool = False, force: bool = False) -> None:
        for warning in self.preflight():
            log.warning(warning)
        if force:
            self.store.del_state("lock")
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # release the lock on `docker stop`
        now = self.clock()
        log.info(
            "Agent started (%s). Window %s-%s %s, today's quota %d, %d contacts ready.",
            "LIVE" if self.live else "rehearsal", self.schedule.start.strftime("%H:%M"),
            self.schedule.end.strftime("%H:%M"), self.cfg["schedule"]["timezone"],
            self.campaign.quota_today(now), self.campaign.ready(),
        )
        try:
            while True:
                try:
                    self.tick()
                except AgentLocked:
                    raise
                except Exception:
                    log.exception("tick failed; retrying at the next tick")
                if once:
                    return
                self.sleep(float(self.acfg["tick_seconds"]))
        finally:
            self.store.release_lock(self.owner)
