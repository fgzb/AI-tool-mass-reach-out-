"""Render and send the outreach e-mails.

Built-in safeguards for French/EU B2B prospecting rules (CNIL, RGPD, LCEN art. L34-5 CPCE):
  * the sender (company name + address) is identified in every message;
  * every message carries an unsubscribe link/address + List-Unsubscribe headers;
  * every message says where the address was obtained;
  * the suppression list (opt-outs, bounces, manual) is checked before each send;
  * one message per company per campaign, with per-run / per-day caps and pacing;
  * dry-run by default: messages are written to the outbox folder as .eml files.
"""
from __future__ import annotations

import logging
import random
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from string import Template
from typing import Any, Callable, Optional

from .config import secret
from .db import Store
from .models import HEADCOUNT_BANDS

log = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\[[A-ZÀ-Ý][A-ZÀ-Ý ,.'-]{2,}")
REQUIRED_MAIL_FIELDS = ("from_address", "from_name", "company_legal_name", "company_address")


class ComplianceError(ValueError):
    pass


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


def check_compliance(mail_cfg: dict[str, Any]) -> None:
    missing = [f for f in REQUIRED_MAIL_FIELDS if not mail_cfg.get(f)]
    if not (mail_cfg.get("unsubscribe_url") or mail_cfg.get("unsubscribe_mailto")):
        missing.append("unsubscribe_url or unsubscribe_mailto")
    if missing:
        raise ComplianceError("mail config is missing required fields: " + ", ".join(missing))


def footer(mail_cfg: dict[str, Any]) -> str:
    opt_out = mail_cfg.get("unsubscribe_url") or f"mailto:{mail_cfg['unsubscribe_mailto']}?subject=STOP"
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
    unsub = []
    if mail_cfg.get("unsubscribe_mailto"):
        unsub.append(f"<mailto:{mail_cfg['unsubscribe_mailto']}?subject=unsubscribe>")
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
            self.conn.login(user, pwd)
        return self

    def send(self, msg: EmailMessage) -> None:
        assert self.conn is not None
        self.conn.send_message(msg)

    def __exit__(self, *exc: Any) -> None:
        if self.conn is not None:
            try:
                self.conn.quit()
            except smtplib.SMTPException:
                pass


def run_campaign(
    store: Store,
    mail_cfg: dict[str, Any],
    scraping_cfg: dict[str, Any],
    really_send: bool = False,
    limit: Optional[int] = None,
    sender_factory: Callable[[dict[str, Any]], Any] = SmtpSender,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    check_compliance(mail_cfg)
    campaign = mail_cfg.get("campaign", "default")
    subject_t, body_t = load_template(mail_cfg["template"])
    if really_send and PLACEHOLDER_RE.search(subject_t + body_t):
        raise ComplianceError(f"template {mail_cfg['template']} still contains [PLACEHOLDERS]; edit it first")
    outbox = Path(mail_cfg.get("outbox_dir") or "outbox") / campaign
    outbox.mkdir(parents=True, exist_ok=True)

    caps = [int(x) for x in (limit, mail_cfg.get("max_per_run")) if x]
    run_cap = min(caps) if caps else None
    day_start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    day_left = max(0, int(mail_cfg.get("max_per_day") or 10**9) - store.sent_since(day_start))
    if really_send and day_left == 0:
        log.warning("Daily cap reached (%s in the last 24h).", mail_cfg.get("max_per_day"))
        return {"sent": 0, "dry_run": 0, "failed": 0}

    if not really_send:
        store.clear_dry_runs(campaign)  # previews can be regenerated any time
    counts = {"sent": 0, "dry_run": 0, "failed": 0}
    per_company = int(scraping_cfg.get("max_emails_per_company") or 1)
    recipients = list(store.pending_recipients(campaign, per_company))

    def process(sender: Any) -> None:
        for company, email_row in recipients:
            done = counts["sent"] + counts["dry_run"]
            if run_cap and done >= run_cap:
                break
            if really_send and counts["sent"] >= day_left:
                log.info("Daily cap reached, stopping.")
                break
            to_addr = email_row["email"]
            msg = build_message(mail_cfg, subject_t, body_t, company, to_addr)
            if not really_send:
                (outbox / f"{company['siren']}_{to_addr.replace('@', '_at_')}.eml").write_bytes(bytes(msg))
                store.record_send(company["siren"], to_addr, campaign, "dry_run", msg["Message-ID"])
                counts["dry_run"] += 1
                continue
            try:
                sender.send(msg)
                store.record_send(company["siren"], to_addr, campaign, "sent", msg["Message-ID"])
                counts["sent"] += 1
                log.info("sent to %s (%s)", to_addr, company["name"])
            except smtplib.SMTPRecipientsRefused as exc:
                store.record_send(company["siren"], to_addr, campaign, "failed", error=str(exc))
                store.suppress(to_addr, "bounce")
                counts["failed"] += 1
            except smtplib.SMTPException as exc:
                store.record_send(company["siren"], to_addr, campaign, "failed", error=str(exc))
                counts["failed"] += 1
                log.error("send to %s failed: %s", to_addr, exc)
            sleep(float(mail_cfg.get("delay_seconds") or 0) + random.uniform(0, float(mail_cfg.get("jitter_seconds") or 0)))

    if really_send:
        with sender_factory(mail_cfg["smtp"]) as sender:
            process(sender)
    else:
        process(None)
    return counts
