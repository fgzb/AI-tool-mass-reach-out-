"""Read the reply inbox over IMAP and add opt-outs and bounces to the suppression list."""
from __future__ import annotations

import email
import imaplib
import logging
import re
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Any

from .config import secret
from .db import Store
from .emails import EMAIL_RE

log = logging.getLogger(__name__)

OPT_OUT_RE = re.compile(
    r"\b(stop|unsubscribe|d[ée]sinscri\w*|d[ée]sabonn\w*|retirez[- ]moi|ne plus (me )?(contacter|recevoir)|pas int[ée]ress[ée])\b",
    re.I,
)
BOUNCE_SENDER_RE = re.compile(r"mailer-daemon|postmaster", re.I)
BOUNCE_FAILED_RE = re.compile(r"(?:Final-Recipient|Original-Recipient):\s*rfc822;\s*(\S+)", re.I)


def _text(msg: email.message.Message) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "message/delivery-status") or part.get_content_maintype() == "text":
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            elif isinstance(part.get_payload(), list):
                parts += [str(p) for p in part.get_payload()]
    return "\n".join(parts)


def classify(msg: email.message.Message) -> tuple[str, list[str]]:
    """Return ("optout"|"bounce"|"other", addresses to suppress)."""
    sender = parseaddr(msg.get("From", ""))[1].lower()
    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    body = _text(msg)
    if BOUNCE_SENDER_RE.search(sender) or msg.get_content_type() == "multipart/report":
        failed = [a.lower().strip("<>;") for a in BOUNCE_FAILED_RE.findall(body)]
        return "bounce", [a for a in failed if EMAIL_RE.fullmatch(a)]
    # Only look at the reply itself, not the quoted original (which contains our "STOP" footer).
    reply = re.split(r"\n\s*(?:>|--\s*\n|Le .{5,80} a [ée]crit|On .{5,80} wrote|De\s*:|From\s*:)", body, maxsplit=1)[0]
    if OPT_OUT_RE.search(subject) or OPT_OUT_RE.search(reply):
        return "optout", [sender] if sender else []
    return "other", []


def sync_unsubscribes(store: Store, imap_cfg: dict[str, Any], mark_seen: bool = False) -> dict[str, int]:
    counts = {"optout": 0, "bounce": 0, "other": 0}
    if not imap_cfg.get("host"):
        raise ValueError("mail.imap.host is not configured")
    conn = imaplib.IMAP4_SSL(imap_cfg["host"], int(imap_cfg.get("port") or 993))
    try:
        conn.login(secret(imap_cfg.get("username_env")), secret(imap_cfg.get("password_env")))
        conn.select(imap_cfg.get("folder") or "INBOX", readonly=not mark_seen)
        _, data = conn.search(None, "UNSEEN")
        for num in data[0].split():
            _, fetched = conn.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(fetched[0][1])
            kind, addresses = classify(msg)
            counts[kind] += 1
            for addr in addresses:
                store.suppress(addr, kind)
                log.info("suppressed %s (%s)", addr, kind)
    finally:
        try:
            conn.logout()
        except imaplib.IMAP4.error:
            pass
    return counts
