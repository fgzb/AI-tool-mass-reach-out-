"""E-mail extraction from HTML, filtering, ranking and domain validation."""
from __future__ import annotations

import html
import re
import socket
from typing import Iterable, Optional
from urllib.parse import unquote, urlsplit

from .models import EmailCandidate

EMAIL_RE = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63}@(?:[a-z0-9-]+\.)+[a-z]{2,24})(?![\w-])", re.I)
MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.I)
CFEMAIL_RE = re.compile(r"(?:data-cfemail=\"|/cdn-cgi/l/email-protection#)([0-9a-f]{10,})", re.I)
# "contact [at] societe [dot] fr", "contact(at)societe.fr", "contact arobase societe point fr"
OBFUSCATED_RE = re.compile(
    r"([a-z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+|\s+arobase\s+|\[arobase\]|\(arobase\)|\[@\]|\(@\))\s*"
    r"([a-z0-9-]+(?:\s*(?:\.|\[dot\]|\(dot\)|\s+dot\s+|\[point\]|\(point\)|\s+point\s+)\s*[a-z0-9-]+)+)",
    re.I,
)
DOT_RE = re.compile(r"\s*(?:\[dot\]|\(dot\)|\s+dot\s+|\[point\]|\(point\)|\s+point\s+)\s*", re.I)

ASSET_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "avif", "bmp", "tif", "tiff", "mp4", "pdf"}
BLOCKED_DOMAINS = {
    "example.com", "example.fr", "exemple.fr", "exemple.com", "domain.com", "domaine.fr", "email.com", "mail.com",
    "votredomaine.fr", "votresite.fr", "monsite.fr", "yourdomain.com", "sentry.io", "sentry-next.wixpress.com",
    "wixpress.com", "sentry.wixpress.com", "godaddy.com", "latofonts.com", "typekit.net", "w3.org", "schema.org",
    "cnil.fr", "ovh.net", "ovh.com", "o2switch.fr", "ionos.fr", "1and1.fr", "hostinger.com", "gandi.net",
}
FREEMAIL_DOMAINS = {
    "gmail.com", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "neuf.fr", "laposte.net", "hotmail.fr",
    "hotmail.com", "outlook.fr", "outlook.com", "live.fr", "yahoo.fr", "yahoo.com", "icloud.com", "bbox.fr",
    "aliceadsl.fr", "club-internet.fr", "numericable.fr", "gmx.fr", "protonmail.com", "proton.me",
}
# Role inboxes that reach a decision maker / the front desk first.
PREFERRED_LOCALS = {
    "contact": 40, "direction": 38, "dirigeant": 38, "info": 35, "infos": 35, "bonjour": 35, "hello": 35,
    "accueil": 30, "commercial": 30, "commerciale": 30, "business": 30, "ventes": 25, "vente": 25,
    "secretariat": 25, "administration": 20, "office": 20, "agence": 20,
}
# Addresses that must not be used for prospection or are pointless for it.
AVOID_LOCAL_PATTERNS = re.compile(
    r"^(no-?reply|do-?not-?reply|ne-?pas-?repondre|postmaster|abuse|mailer-daemon|webmaster|hostmaster|"
    r"dpo|rgpd|gdpr|privacy|donnees-?personnelles|cnil|legal|juridique|"
    r"rh|drh|recrutement|recruitment|jobs?|emploi|candidature|careers?|stage|"
    r"compta|comptabilite|factur\w*|invoice|billing|paie|"
    r"sav|support|helpdesk|press|presse|media)$",
    re.I,
)


def registrable_domain(host_or_url: str) -> str:
    """Rough eTLD+1 (good enough for .fr/.com/.eu/.co.uk style domains)."""
    host = urlsplit(host_or_url).hostname if "://" in host_or_url else host_or_url
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "gouv", "asso", "org", "net"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def decode_cfemail(hexstr: str) -> str:
    key = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i : i + 2], 16) ^ key) for i in range(2, len(hexstr) - 1, 2))


def clean(email: str) -> Optional[str]:
    email = unquote(email).strip().strip(".,;:()<>[]\"'").lower()
    if email.startswith("mailto:"):
        email = email[7:]
    if not EMAIL_RE.fullmatch(email):
        return None
    local, domain = email.rsplit("@", 1)
    tld = domain.rsplit(".", 1)[-1]
    if tld in ASSET_TLDS or re.search(r"@\d+x\.", email):  # logo@2x.png
        return None
    if domain in BLOCKED_DOMAINS or any(domain.endswith("." + d) for d in BLOCKED_DOMAINS):
        return None
    if re.fullmatch(r"[0-9a-f]{16,}", local):  # tracking hashes (sentry keys etc.)
        return None
    if ".." in email or local.startswith(".") or local.endswith("."):
        return None
    return email


def extract_emails(page_html: str) -> set[str]:
    found: set[str] = set()
    for hexstr in CFEMAIL_RE.findall(page_html):
        try:
            found.add(decode_cfemail(hexstr))
        except ValueError:
            pass
    text = html.unescape(page_html)
    found.update(MAILTO_RE.findall(text))
    found.update(m.group(1) for m in EMAIL_RE.finditer(text))
    # Obfuscated forms are only searched in visible text to limit false positives.
    visible = re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", text, flags=re.S | re.I)
    for local, dom in OBFUSCATED_RE.findall(visible):
        found.add(f"{local}@{DOT_RE.sub('.', dom)}")
    return {e for e in (clean(x) for x in found) if e}


def score_email(email: str, site_domain: str) -> tuple[int, str]:
    """Higher is better. Negative means "do not use"."""
    local, domain = email.split("@", 1)
    base_local = re.split(r"[.+_-]", local)[0]
    if AVOID_LOCAL_PATTERNS.match(local) or AVOID_LOCAL_PATTERNS.match(base_local):
        return -100, "avoid"
    same_domain = site_domain and registrable_domain(domain) == registrable_domain(site_domain)
    if domain in FREEMAIL_DOMAINS:
        score, kind = 20, "freemail"  # very common for French TPE/PME
    elif same_domain:
        score, kind = 50, "company-domain"
    else:
        return -50, "third-party"  # web agency, host, partner... not the company
    if local in PREFERRED_LOCALS:
        score += PREFERRED_LOCALS[local]
        kind += "/role"
    elif re.fullmatch(r"[a-z]+[.-][a-z]+", local):
        score += 15  # firstname.lastname: a named person
        kind += "/person"
    return score, kind


def rank(emails: Iterable[tuple[str, str]], site_domain: str) -> list[EmailCandidate]:
    """emails: iterable of (email, source_url). Returns usable candidates, best first."""
    best: dict[str, EmailCandidate] = {}
    for email, url in emails:
        score, kind = score_email(email, site_domain)
        if score < 0:
            continue
        if "mentions" in url or "contact" in url:
            score += 5
        if email not in best or best[email].score < score:
            best[email] = EmailCandidate(email=email, source_url=url, score=score, kind=kind)
    return sorted(best.values(), key=lambda c: (-c.score, c.email))


def domain_accepts_mail(domain: str) -> Optional[bool]:
    """True/False when we could check, None when unknown."""
    try:
        import dns.resolver  # type: ignore

        try:
            return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return False
        except Exception:
            return None
    except ImportError:
        try:
            socket.getaddrinfo(domain, 25)
            return True  # implicit MX via A record
        except socket.gaierror:
            return False
        except OSError:
            return None
