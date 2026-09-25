"""Read the reply mailbox over IMAP: record replies, opt-outs and bounces.

The mailbox is opened read-only (flags are never changed) and only messages that
arrived since the last sync are fetched, so the agent can run this every few minutes.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from typing import Any, Callable, Optional

from .config import secret
from .db import Store
from .emails import EMAIL_RE, FREEMAIL_DOMAINS

log = logging.getLogger(__name__)

OPT_OUT_RE = re.compile(
    r"\b(stop|unsubscribe|d[ée]sinscri\w*|d[ée]sabonn\w*|retirez[- ]moi|retirer (mon|notre) adresse|"
    r"ne (plus|pas) (me |nous )?(contacter|recontacter|[ée]crire|solliciter)|ne plus recevoir|"
    r"pas int[ée]ress[ée]e?s?|non merci)\b",
    re.I,
)
AUTO_SUBJECT_RE = re.compile(
    r"absen(t|ce)|out of (the )?office|r[ée]ponse automatique|automatic reply|auto[- ]?reply|autoreply|"
    r"cong[ée]s|vacances|accus[ée] de r[ée]ception",
    re.I,
)
BOUNCE_SENDER_RE = re.compile(r"mailer-daemon|postmaster", re.I)
BOUNCE_FAILED_RE = re.compile(r"(?:Final-Recipient|Original-Recipient):\s*rfc822;\s*<?([^\s>;]+)", re.I)
QUOTE_SPLIT_RE = re.compile(
    r"\n\s*(?:>|-{2,}\s*(?:Original|Message d'origine|Message transf)|Le .{5,120}a [ée]crit|On .{5,120}wrote|"
    r"De\s*:|From\s*:|Envoy[ée]\s*:|Sent\s*:)",
    re.I,
)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def reply_text(msg: Message) -> str:
    """The text the person actually wrote: first text part, quoted history removed."""
    text, html = "", ""
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            break  # an attached/forwarded original: stop there
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() == "text/plain" and not text:
            text = _decode(part)
        elif part.get_content_type() == "text/html" and not html:
            html = _decode(part)
    if not text and html:
        html = re.sub(r"(?is)<(blockquote|style|script)\b.*?</\1>", " ", html)
        html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
        text = re.sub(r"<[^>]+>", " ", html)
    return QUOTE_SPLIT_RE.split("\n" + text, maxsplit=1)[0]


def full_text(msg: Message) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            parts.append(_decode(part))
        elif part.get_content_type() == "message/delivery-status":
            parts += [str(p) for p in part.get_payload()]
    return "\n".join(parts)


def header_text(msg: Message, name: str) -> str:
    value = msg.get(name, "")
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def classify(msg: Message) -> tuple[str, list[str]]:
    """Return (kind, addresses): kind is optout | bounce | auto | reply.

    `addresses` are the bounced recipients for a bounce, the sender otherwise.
    """
    sender = parseaddr(header_text(msg, "From"))[1].lower()
    subject = header_text(msg, "Subject")
    if BOUNCE_SENDER_RE.search(sender) or msg.get_content_type() == "multipart/report":
        failed = [a.lower() for a in BOUNCE_FAILED_RE.findall(full_text(msg))]
        return "bounce", sorted({a for a in failed if EMAIL_RE.fullmatch(a)})
    auto = msg.get("Auto-Submitted", "no").lower() != "no" or msg.get("X-Autoreply") or msg.get("X-Autorespond")
    if auto or AUTO_SUBJECT_RE.search(subject):
        return "auto", [sender]
    if OPT_OUT_RE.search(subject) or OPT_OUT_RE.search(reply_text(msg)):
        return "optout", [sender]
    return "reply", [sender]


def process_message(store: Store, raw: bytes, own_addresses: frozenset[str] = frozenset()) -> str:
    """Classify one message and update the database. Returns the kind recorded."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    message_id = str(msg.get("Message-ID") or "").strip() or f"<sha1-{hashlib.sha1(raw).hexdigest()}>"
    if store.has_inbox_event(message_id):
        return "duplicate"
    if parseaddr(header_text(msg, "From"))[1].lower() in own_addresses:
        store.record_inbox_event(message_id, "other")  # our own report / alert
        return "other"
    kind, addresses = classify(msg)
    subject = header_text(msg, "Subject")

    if kind == "bounce":
        if not addresses:  # non-standard bounce: look for one of our recipients in the text
            found = {a.lower() for a in EMAIL_RE.findall(full_text(msg))}
            addresses = sorted(a for a in found if store.send_by_address(a, domain_fallback=False))
        sirens = []
        for addr in addresses:
            store.suppress(addr, "bounce")
            send = store.send_by_address(addr, domain_fallback=False)
            sirens.append(send["siren"] if send else "")
        store.record_inbox_event(message_id, "bounce", "", subject[:200], sirens[0] if sirens else "", ",".join(addresses))
        return "bounce"

    sender = addresses[0] if addresses else ""
    # Which of our e-mails is this about? Threading headers first, then the address / company domain.
    refs = re.findall(r"<[^>]+>", " ".join(str(h) for h in msg.get_all("In-Reply-To", []) + msg.get_all("References", [])))
    send = store.send_by_message_ids(refs)
    if send is None and sender:
        freemail = sender.split("@")[-1] in FREEMAIL_DOMAINS
        send = store.send_by_address(sender, domain_fallback=not freemail)
    if send is None:
        # Not a reaction to our outreach (colleague, newsletter...): remember only that we saw it.
        store.record_inbox_event(message_id, "other")
        return "other"

    if kind == "optout":
        for addr in {sender, send["email"]}:
            store.suppress(addr, "optout")
        domain = send["email"].split("@")[-1]
        if domain not in FREEMAIL_DOMAINS:
            store.suppress("@" + domain, "optout")  # the company asked us to stop
    store.record_inbox_event(message_id, kind, sender, subject[:200], send["siren"], send["email"])
    return kind


def imap_date(dt: datetime) -> str:
    return f"{dt.day:02d}-{MONTHS[dt.month - 1]}-{dt.year}"


def sync_inbox(
    store: Store,
    imap_cfg: dict[str, Any],
    days: int = 14,
    connect: Optional[Callable[[dict[str, Any]], Any]] = None,
    own_addresses: frozenset[str] = frozenset(),
) -> dict[str, int]:
    """Fetch new messages since the last sync and process them."""
    if not imap_cfg.get("host") and connect is None:
        raise ValueError("mail.imap.host is not configured")
    counts = {"reply": 0, "optout": 0, "bounce": 0, "auto": 0, "other": 0, "duplicate": 0}
    conn = connect(imap_cfg) if connect else imaplib.IMAP4_SSL(imap_cfg["host"], int(imap_cfg.get("port") or 993))
    try:
        if connect is None:
            conn.login(secret(imap_cfg.get("username_env")), secret(imap_cfg.get("password_env")))
        folder = imap_cfg.get("folder") or "INBOX"
        conn.select(folder, readonly=True)
        raw_validity = (conn.response("UIDVALIDITY")[1] or [None])[0]
        validity = raw_validity.decode() if isinstance(raw_validity, bytes) else str(raw_validity or "")
        last_uid = int(store.get_state(f"imap:{folder}:last_uid", "0") or 0)
        if store.get_state(f"imap:{folder}:validity") != validity or not last_uid:
            last_uid = 0
            since = imap_date(datetime.now(timezone.utc) - timedelta(days=days))
            _, data = conn.uid("SEARCH", None, "SINCE", since)
        else:
            _, data = conn.uid("SEARCH", None, "UID", f"{last_uid + 1}:*")
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid)
        for uid in uids:
            _, fetched = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((item[1] for item in fetched or [] if isinstance(item, tuple)), None)
            if raw:
                kind = process_message(store, raw, own_addresses)
                counts[kind] += 1
                if kind in ("optout", "bounce"):
                    log.info("inbox: %s recorded", kind)
            store.set_state(f"imap:{folder}:last_uid", uid)
        store.set_state(f"imap:{folder}:validity", validity)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return counts
