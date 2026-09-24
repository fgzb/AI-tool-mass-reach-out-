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

import logging
import random
import re
import smtplib
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from string import Template
from typing import Any, Callable, Optional

from .config import secret
from .db import Store
from .models import HEADCOUNT_BANDS
from .schedule import Schedule, daily_quota

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


def build_message(mail_cfg: dict[str, Any], subject_t: str, body_t: str, company: Any, to_addr: str) -> EmailMessage:
    variables = template_vars(company, to_addr)
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
    """One outreach campaign: template, recipients, delivery and bookkeeping."""

    def __init__(self, store: Store, cfg: dict[str, Any], live: bool, strict: bool = True):
        self.store = store
        self.mail = cfg["mail"]
        self.live = live
        self.name = self.mail.get("campaign") or "default"
        self.per_company = int(cfg["scraping"].get("max_emails_per_company") or 1)
        self.recontact_days = int(self.mail.get("recontact_after_days") or 0)
        self.schedule = Schedule(cfg["schedule"])
        check_compliance(self.mail)
        self.subject_t, self.body_t = load_template(self.mail["template"])
        self.has_placeholders = bool(PLACEHOLDER_RE.search(self.subject_t + self.body_t))
        if live and strict and self.has_placeholders:
            raise ComplianceError(f"template {self.mail['template']} still contains [PLACEHOLDERS]; edit it first")
        self.outbox = Path(self.mail.get("outbox_dir") or "outbox") / self.name

    # -- quota ------------------------------------------------------------------
    def quota_today(self, now_utc: datetime) -> int:
        return daily_quota(self.store, self.mail, self.schedule.day_start_utc(now_utc))

    def done_today(self, now_utc: datetime) -> int:
        statuses = ("sent", "failed") if self.live else ("dry_run",)
        start = self.schedule.day_start_utc(now_utc).isoformat(timespec="seconds")
        return self.store.count_sends_since(start, statuses)

    def remaining_today(self, now_utc: datetime) -> int:
        return max(0, self.quota_today(now_utc) - self.done_today(now_utc))

    # -- recipients ----------------------------------------------------------------
    def recipients(self, limit: Optional[int] = None) -> list[Any]:
        return self.store.recipients(self.name, self.per_company, not self.live, self.recontact_days, limit)

    def ready(self) -> int:
        return self.store.count_ready(self.name, self.per_company, not self.live, self.recontact_days)

    # -- delivery ---------------------------------------------------------------------
    def preview(self, company: Any, to_addr: str) -> str:
        msg = build_message(self.mail, self.subject_t, self.body_t, company, to_addr)
        self.outbox.mkdir(parents=True, exist_ok=True)
        (self.outbox / f"{company['siren']}_{to_addr.replace('@', '_at_')}.eml").write_bytes(bytes(msg))
        self.store.record_send(company["siren"], to_addr, self.name, "dry_run", msg["Message-ID"])
        return "dry_run"

    def deliver(self, sender: Any, company: Any, to_addr: str) -> str:
        """Send one message. Returns "sent" or "failed"; raises SendBlocked or transient errors."""
        msg = build_message(self.mail, self.subject_t, self.body_t, company, to_addr)
        siren = company["siren"]
        # Reserve first: if we crash mid-send we will not e-mail this person twice.
        self.store.record_send(siren, to_addr, self.name, "pending", msg["Message-ID"])
        try:
            sender.send(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            detail = str(exc.recipients)
            if ACCOUNT_BLOCK_RE.search(detail):
                self.store.delete_send(to_addr, self.name)
                raise SendBlocked(detail) from exc
            self.store.record_send(siren, to_addr, self.name, "failed", msg["Message-ID"], detail)
            self.store.suppress(to_addr, "bounce")
            return "failed"
        except smtplib.SMTPSenderRefused as exc:
            self.store.delete_send(to_addr, self.name)
            raise SendBlocked(f"sender refused: {exc}") from exc
        except smtplib.SMTPResponseException as exc:
            detail = f"{exc.smtp_code} {exc.smtp_error!r}"
            if exc.smtp_code >= 500 and not ACCOUNT_BLOCK_RE.search(detail):
                self.store.record_send(siren, to_addr, self.name, "failed", msg["Message-ID"], detail)
                return "failed"  # rejected by the recipient's server
            self.store.delete_send(to_addr, self.name)
            if exc.smtp_code >= 500:
                raise SendBlocked(detail) from exc
            raise  # 4xx: temporary, retry later
        except BaseException:
            self.store.delete_send(to_addr, self.name)  # not sent: free the reservation
            raise
        self.store.record_send(siren, to_addr, self.name, "sent", msg["Message-ID"])
        log.info("sent to %s (%s)", to_addr, company["name"])
        return "sent"


def run_campaign(
    store: Store,
    cfg: dict[str, Any],
    really_send: bool = False,
    limit: Optional[int] = None,
    sender_factory: Callable[[dict[str, Any]], Any] = SmtpSender,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Manual one-off run (`fr-outreach send`)."""
    campaign = Campaign(store, cfg, live=really_send)
    mail_cfg = cfg["mail"]
    counts = {"sent": 0, "dry_run": 0, "failed": 0}
    caps = [int(x) for x in (limit, mail_cfg.get("max_per_run")) if x]
    if really_send:
        caps.append(campaign.remaining_today(store.clock()))
        if caps[-1] == 0:
            log.warning("Today's quota is used up (warm-up / max_per_day).")
            return counts
    else:
        store.clear_dry_runs(campaign.name)  # previews can be regenerated any time
    recipients = campaign.recipients(min(caps) if caps else None)
    if not really_send:
        for company in recipients:
            counts[campaign.preview(company, company["email"])] += 1
        return counts
    with sender_factory(mail_cfg["smtp"]) as sender:
        for i, company in enumerate(recipients):
            if i:
                sleep(float(mail_cfg.get("delay_seconds") or 0) + random.uniform(0, float(mail_cfg.get("jitter_seconds") or 0)))
            try:
                counts[campaign.deliver(sender, company, company["email"])] += 1
            except SendBlocked:
                raise
            except (smtplib.SMTPException, OSError) as exc:
                log.error("send to %s failed temporarily: %s", company["email"], exc)
    return counts
