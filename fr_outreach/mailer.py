"""Render and send the outreach e-mails.

Built-in safeguards for French/EU B2B prospecting rules (CNIL, RGPD, art. L34-5 CPCE):
  * the sender (company name + address) is identified in every message;
  * every message carries an opt-out address/link + List-Unsubscribe headers;
  * every message says where the address was obtained;
  * the suppression list (opt-outs, bounces, manual) is checked before each send;
  * one message per company per campaign, no re-contact within `recontact_after_days`;
  * a daily quota with a warm-up ramp, and pacing between messages;
  * dry run by default: messages are written to the outbox folder as .eml files.
"""
from __future__ import annotations

import hashlib
import logging
import math
import random
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from string import Template
from typing import Any, Callable, Optional

from .config import secret
from .db import Store
from .models import HEADCOUNT_BANDS
from .schedule import Schedule, daily_quota, iso

log = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\[[A-ZÀ-Ý][A-ZÀ-Ý ,.'-]{2,}")
REQUIRED_MAIL_FIELDS = ("from_address", "from_name", "company_legal_name", "company_address")
# Provider-side quota / policy blocks: the whole account is refused, not one recipient.
ACCOUNT_BLOCK_RE = re.compile(r"5\.4\.5|5\.7\.708|limit exceeded|quota exceeded|too many messages|sending limit", re.I)


class ComplianceError(ValueError):
    pass


class SendBlocked(RuntimeError):
    """The provider refuses our account (auth failure, quota exceeded, flagged as spam...)."""


def load_template(path: str) -> tuple[str, str]:
    """Template format: first line 'Subject: ...', blank line, then the body."""
    raw = Path(path).read_text(encoding="utf-8")
    head, _, body = raw.partition("\n\n")
    match = re.match(r"(?i)subject:\s*(.+)", head.strip())
    if not match:
        raise ValueError(f"{path}: the first line must be 'Subject: ...'")
    return match.group(1).strip(), body.strip() + "\n"


def template_vars(company: Any, email: str) -> dict[str, str]:
    c = dict(company)
    first = c.get("director_first_name") or ""
    last = c.get("director_last_name") or ""
    return {
        "company_name": (c.get("name") or "").strip(),
        "city": (c.get("city") or "").title(),
        "department": c.get("department") or "",
        "naf": c.get("naf") or "",
        "headcount": HEADCOUNT_BANDS.get(c.get("headcount_band") or "", ""),
        "director_first_name": first,
        "director_last_name": last,
        "greeting": f"Bonjour {first} {last}".strip() if (first and last) else "Bonjour",
        "website": c.get("website") or "",
        "email": email,
        "siren": c.get("siren") or "",
    }


def opt_out_mailto(mail_cfg: dict[str, Any]) -> str:
    # Opt-outs sent to the sending mailbox itself are picked up by `sync-inbox` / the agent.
    return mail_cfg.get("unsubscribe_mailto") or mail_cfg.get("from_address") or ""


def check_compliance(mail_cfg: dict[str, Any]) -> None:
    missing = [f for f in REQUIRED_MAIL_FIELDS if not mail_cfg.get(f)]
    if missing:
        raise ComplianceError("mail config is missing required fields: " + ", ".join(missing))


def footer(mail_cfg: dict[str, Any]) -> str:
    opt_out = mail_cfg.get("unsubscribe_url") or f"mailto:{opt_out_mailto(mail_cfg)}?subject=STOP"
    return (
        "\n--\n"
        f"{mail_cfg['company_legal_name']} - {mail_cfg['company_address']}\n"
        f"{mail_cfg['data_source_notice']}\n"
        "Conformément au RGPD, vous pouvez vous opposer à tout moment à la réception de nos messages : "
        f"répondez STOP à cet e-mail ou utilisez ce lien : {opt_out}\n"
    )


def build_message(
    mail_cfg: dict[str, Any], subject_t: str, body_t: str, company: Any, to_addr: str,
    extra_vars: Optional[dict[str, str]] = None, in_reply_to: str = "",
) -> EmailMessage:
    variables = {**template_vars(company, to_addr), **(extra_vars or {})}
    msg = EmailMessage()
    msg["From"] = formataddr((mail_cfg["from_name"], mail_cfg["from_address"]))
    msg["To"] = to_addr
    msg["Subject"] = Template(subject_t).safe_substitute(variables)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=mail_cfg["from_address"].split("@")[-1])
    if mail_cfg.get("reply_to"):
        msg["Reply-To"] = mail_cfg["reply_to"]
    unsub = [f"<mailto:{opt_out_mailto(mail_cfg)}?subject=unsubscribe>"]
    if mail_cfg.get("unsubscribe_url"):
        unsub.append(f"<{mail_cfg['unsubscribe_url']}>")
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["List-Unsubscribe"] = ", ".join(unsub)
    msg["X-Campaign"] = mail_cfg.get("campaign", "default")
    if in_reply_to:  # follow-up: same thread as the first e-mail
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(Template(body_t).safe_substitute(variables) + footer(mail_cfg))
    return msg


class SmtpSender:
    def __init__(self, smtp_cfg: dict[str, Any]):
        self.cfg = smtp_cfg
        self.conn: Optional[smtplib.SMTP] = None

    def __enter__(self) -> "SmtpSender":
        host, port, security = self.cfg["host"], int(self.cfg["port"]), self.cfg.get("security", "starttls")
        if not host:
            raise ComplianceError("mail.smtp.host is not configured")
        ctx = ssl.create_default_context()
        if security == "ssl":
            self.conn = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            self.conn = smtplib.SMTP(host, port, timeout=30)
            if security == "starttls":
                self.conn.starttls(context=ctx)
        user, pwd = secret(self.cfg.get("username_env")), secret(self.cfg.get("password_env"))
        if user:
            try:
                self.conn.login(user, pwd)
            except smtplib.SMTPAuthenticationError as exc:
                raise SendBlocked(f"SMTP login refused: {exc}") from exc
        return self

    def send(self, msg: EmailMessage) -> None:
        assert self.conn is not None
        self.conn.send_message(msg)

    def __exit__(self, *exc: Any) -> None:
        if self.conn is not None:
            try:
                self.conn.quit()
            except (smtplib.SMTPException, OSError):
                pass


class Campaign:
    """One outreach campaign: templates (A/B variants), follow-up, recipients, delivery, bookkeeping."""

    def __init__(self, store: Store, cfg: dict[str, Any], live: bool, strict: bool = True):
        self.store = store
        self.mail = cfg["mail"]
        self.live = live
        self.name = self.mail.get("campaign") or "default"
        self.per_company = int(cfg["scraping"].get("max_emails_per_company") or 1)
        self.recontact_days = int(self.mail.get("recontact_after_days") or 0)
        self.schedule = Schedule(cfg["schedule"])
        check_compliance(self.mail)
        # A/B testing: each company always gets the same variant (stable hash of its SIREN).
        paths = self.mail.get("templates") or [self.mail["template"]]
        self.variants = [(Path(p).stem, *load_template(p)) for p in paths]
        fu = self.mail.get("followup") or {}
        self.followup = load_template(fu["template"]) if fu.get("enabled") else None
        self.followup_days = int(fu.get("after_business_days", 4))
        self.followup_expire_days = int(fu.get("expire_after_days", 14))
        self.followup_share = float(fu.get("max_share", 0.5))
        texts = [subject + body for _, subject, body in self.variants] + (list(self.followup) if self.followup else [])
        self.has_placeholders = any(PLACEHOLDER_RE.search(t) for t in texts)
        if live and strict and self.has_placeholders:
            raise ComplianceError("a template still contains [PLACEHOLDERS]; edit it first")
        self.outbox = Path(self.mail.get("outbox_dir") or "outbox") / self.name

    # -- quota ------------------------------------------------------------------
    def quota_today(self, now_utc: datetime) -> int:
        return daily_quota(self.store, self.mail, self.schedule.day_start_utc(now_utc))

    def _statuses(self) -> tuple[str, ...]:
        return ("sent", "failed") if self.live else ("dry_run",)

    def done_today(self, now_utc: datetime, step: Optional[int] = None) -> int:
        start = self.schedule.day_start_utc(now_utc).isoformat(timespec="seconds")
        return self.store.count_sends_since(start, self._statuses(), step)

    def remaining_today(self, now_utc: datetime) -> int:
        return max(0, self.quota_today(now_utc) - self.done_today(now_utc))

    def warming_up(self, now_utc: datetime) -> bool:
        return self.quota_today(now_utc) < int(self.mail.get("max_per_day") or 10**9)

    # -- recipients ----------------------------------------------------------------
    def recipients(self, limit: Optional[int] = None, best_first: bool = False) -> list[Any]:
        return self.store.recipients(
            self.name, self.per_company, not self.live, self.recontact_days, limit, best_first
        )

    def ready(self) -> int:
        return self.store.count_ready(self.name, self.per_company, not self.live, self.recontact_days)

    def followups_due(self, now_utc: datetime, limit: Optional[int] = None) -> list[Any]:
        if not self.followup:
            return []
        cutoff = self.schedule.business_days_cutoff(now_utc, self.followup_days)
        expire = cutoff - timedelta(days=self.followup_expire_days)
        return self.store.followups_due(self.name, iso(cutoff), iso(expire), not self.live, limit)

    def plan(self, now_utc: datetime, n: int) -> list[tuple[Any, int]]:
        """The next `n` messages as (row, step): follow-ups interleaved with first e-mails.

        Follow-ups take at most `followup.max_share` of the day (unless there is nothing else
        to send), so new companies keep being contacted every day.
        """
        if n <= 0:
            return []
        due = self.followups_due(now_utc, limit=n)
        allowed = max(0, math.ceil(self.followup_share * (self.done_today(now_utc) + n)) - self.done_today(now_utc, 2))
        followups = due[:allowed]
        new = self.recipients(n - len(followups), best_first=self.warming_up(now_utc)) if n > len(followups) else []
        missing = n - len(followups) - len(new)
        if missing > 0:  # nothing new to send: the rest of the quota can go to follow-ups
            followups += due[len(followups):len(followups) + missing]
        return [(row, 2) for row in followups] + [(row, 1) for row in new]

    # -- rendering -----------------------------------------------------------------------
    def variant(self, siren: str, name: Optional[str] = None) -> tuple[str, str, str]:
        for variant in self.variants:
            if variant[0] == name:
                return variant
        return self.variants[int(hashlib.sha1(siren.encode()).hexdigest(), 16) % len(self.variants)]

    def render(self, row: Any, step: int = 1, parent_message_id: str = "", variant_name: Optional[str] = None) -> tuple[EmailMessage, str]:
        to_addr = row["email"]
        name, subject_t, body_t = self.variant(row["siren"], variant_name)
        if step == 1:
            return build_message(self.mail, subject_t, body_t, row, to_addr), name
        assert self.followup is not None
        original_subject = Template(subject_t).safe_substitute(template_vars(row, to_addr))
        followup_subject, followup_body = self.followup
        msg = build_message(
            self.mail, followup_subject, followup_body, row, to_addr,
            extra_vars={"original_subject": original_subject}, in_reply_to=parent_message_id,
        )
        return msg, name

    @staticmethod
    def _parent(row: Any, step: int) -> tuple[str, Optional[str]]:
        if step == 1:
            return "", None
        return row["parent_message_id"] or "", row["variant"]

    # -- delivery ---------------------------------------------------------------------
    def preview(self, row: Any, step: int = 1) -> str:
        msg, variant = self.render(row, step, *self._parent(row, step))
        self.outbox.mkdir(parents=True, exist_ok=True)
        stem = f"{row['siren']}_{row['email'].replace('@', '_at_')}"
        (self.outbox / f"{stem}{'' if step == 1 else '_relance'}.eml").write_bytes(bytes(msg))
        if step == 1 and self.followup:  # also show what the follow-up will look like
            follow, _ = self.render(row, 2, msg["Message-ID"], variant)
            (self.outbox / f"{stem}_relance.eml").write_bytes(bytes(follow))
        self.store.record_send(row["siren"], row["email"], self.name, "dry_run", msg["Message-ID"], step=step, variant=variant)
        return "dry_run"

    def deliver(self, sender: Any, row: Any, step: int = 1) -> str:
        """Send one message. Returns "sent" or "failed"; raises SendBlocked or transient errors."""
        msg, variant = self.render(row, step, *self._parent(row, step))
        siren, to_addr, mid = row["siren"], row["email"], msg["Message-ID"]

        def record(status: str, error: str = "") -> None:
            self.store.record_send(siren, to_addr, self.name, status, mid, error, step=step, variant=variant)

        # Reserve first: if we crash mid-send we will not e-mail this person twice.
        record("pending")
        try:
            sender.send(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            detail = str(exc.recipients)
            if ACCOUNT_BLOCK_RE.search(detail):
                self.store.delete_send(to_addr, self.name, step)
                raise SendBlocked(detail) from exc
            record("failed", detail)
            self.store.suppress(to_addr, "bounce")
            return "failed"
        except smtplib.SMTPSenderRefused as exc:
            self.store.delete_send(to_addr, self.name, step)
            raise SendBlocked(f"sender refused: {exc}") from exc
        except smtplib.SMTPResponseException as exc:
            detail = f"{exc.smtp_code} {exc.smtp_error!r}"
            if exc.smtp_code >= 500 and not ACCOUNT_BLOCK_RE.search(detail):
                record("failed", detail)
                return "failed"  # rejected by the recipient's server
            self.store.delete_send(to_addr, self.name, step)
            if exc.smtp_code >= 500:
                raise SendBlocked(detail) from exc
            raise  # 4xx: temporary, retry later
        except BaseException:
            self.store.delete_send(to_addr, self.name, step)  # not sent: free the reservation
            raise
        record("sent")
        log.info("sent %s to %s (%s)", "follow-up" if step == 2 else "e-mail", to_addr, row["name"])
        return "sent"


def run_campaign(
    store: Store,
    cfg: dict[str, Any],
    really_send: bool = False,
    limit: Optional[int] = None,
    sender_factory: Callable[[dict[str, Any]], Any] = SmtpSender,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Manual one-off run (`fr-outreach send`): due follow-ups + first e-mails, within today's quota."""
    campaign = Campaign(store, cfg, live=really_send)
    mail_cfg = cfg["mail"]
    now = store.clock()
    counts = {"sent": 0, "dry_run": 0, "failed": 0}
    caps = [int(x) for x in (limit, mail_cfg.get("max_per_run")) if x]
    if really_send:
        caps.append(campaign.remaining_today(now))
        if caps[-1] == 0:
            log.warning("Today's quota is used up (warm-up / max_per_day).")
            return counts
    else:
        store.clear_dry_runs(campaign.name)  # previews can be regenerated any time
    plan = campaign.plan(now, min(caps) if caps else 10**6)
    if not really_send:
        for row, step in plan:
            counts[campaign.preview(row, step)] += 1
        return counts
    with sender_factory(mail_cfg["smtp"]) as sender:
        for i, (row, step) in enumerate(plan):
            if i:
                sleep(float(mail_cfg.get("delay_seconds") or 0) + random.uniform(0, float(mail_cfg.get("jitter_seconds") or 0)))
            try:
                counts[campaign.deliver(sender, row, step)] += 1
            except SendBlocked:
                raise
            except (smtplib.SMTPException, OSError) as exc:
                log.error("send to %s failed temporarily: %s", row["email"], exc)
    return counts
