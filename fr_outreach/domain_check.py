"""Is a sending domain ready for outreach? Checks MX, SPF, DKIM, DMARC and detects the provider.

Gmail and Yahoo reject or spam-folder mail from domains without SPF + DKIM + DMARC,
and Orange / Outlook are just as strict with new senders, so fix every FAIL before sending.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

import requests

COMMON_DKIM_SELECTORS = [
    "google", "selector1", "selector2", "default", "dkim", "mail", "k1", "k2", "s1", "s2",
    "key1", "smtp", "mx", "zoho", "zmail", "ionos", "gandi", "ovh",
]

PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "Google Workspace", "mx": ("google.com", "googlemail.com"), "spf": "_spf.google.com",
        "smtp": ("smtp.gmail.com", 587, "starttls"), "imap": "imap.gmail.com",
        "note": "Limit: 2,000 messages/day per user (fewer on new or trial accounts). "
                "Log in with an app password (needs 2-step verification).",
    },
    {
        "name": "Microsoft 365", "mx": ("protection.outlook.com",), "spf": "spf.protection.outlook.com",
        "smtp": ("smtp.office365.com", 587, "starttls"), "imap": "outlook.office365.com",
        "note": "Limits: 10,000 recipients/day, 30 messages/minute. Microsoft blocks password logins for IMAP "
                "and is retiring them for SMTP, so this tool needs OAuth support for this provider.",
    },
    {
        "name": "OVHcloud", "mx": ("ovh.net",), "spf": None,
        "smtp": ("ssl0.ovh.net", 465, "ssl"), "imap": "ssl0.ovh.net",
        "note": "Settings for MX Plan mailboxes; Email Pro / Exchange plans use other servers (see the OVH control panel).",
    },
    {
        "name": "IONOS", "mx": ("ionos.fr", "ionos.com", "kundenserver.de"), "spf": None,
        "smtp": ("smtp.ionos.fr", 587, "starttls"), "imap": "imap.ionos.fr", "note": "",
    },
    {
        "name": "Gandi", "mx": ("gandi.net",), "spf": None,
        "smtp": ("mail.gandi.net", 587, "starttls"), "imap": "mail.gandi.net", "note": "",
    },
    {
        "name": "Zoho Mail", "mx": ("zoho.eu", "zoho.com"), "spf": None,
        "smtp": ("smtp.zoho.eu", 587, "starttls"), "imap": "imap.zoho.eu",
        "note": "EU data-centre servers; accounts on zoho.com use smtp.zoho.com / imap.zoho.com.",
    },
]

Resolver = Callable[[str, str], list[str]]


def resolve(name: str, rtype: str) -> list[str]:
    """TXT or MX records as strings; [] when the name has no such record."""
    try:
        import dns.resolver  # type: ignore
    except ImportError:
        return _resolve_doh(name, rtype)
    try:
        answers = dns.resolver.resolve(name, rtype, lifetime=10)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    if rtype == "TXT":
        return [b"".join(r.strings).decode(errors="replace") for r in answers]
    return [r.to_text() for r in answers]


def _resolve_doh(name: str, rtype: str) -> list[str]:
    """DNS over HTTPS (Cloudflare JSON API), used when dnspython is not installed."""
    resp = requests.get(
        "https://cloudflare-dns.com/dns-query",
        params={"name": name, "type": rtype},
        headers={"accept": "application/dns-json"},
        timeout=10,
    )
    resp.raise_for_status()
    code = {"TXT": 16, "MX": 15}[rtype]
    out = []
    for answer in resp.json().get("Answer") or []:
        if answer.get("type") != code:
            continue
        data = answer.get("data", "")
        if rtype == "TXT":  # '"part one" "part two"' -> 'part onepart two'
            data = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', data)) or data
        out.append(data)
    return out


def detect_provider(mx_records: list[str]) -> Optional[dict[str, Any]]:
    hosts = " ".join(mx_records).lower()
    return next((p for p in PROVIDERS if any(m in hosts for m in p["mx"])), None)


def check_domain(domain: str, resolver: Resolver = resolve, dkim_selectors: Optional[list[str]] = None) -> dict[str, Any]:
    domain = domain.split("@")[-1].strip().lower()
    checks: list[tuple[str, str, str]] = []  # (OK|WARN|FAIL, what, detail)

    mx = resolver(domain, "MX")
    provider = detect_provider(mx)
    if mx:
        checks.append(("OK", "MX", ", ".join(sorted(mx))))
    else:
        checks.append(("FAIL", "MX", "no MX record: this domain cannot receive replies, opt-outs or bounces"))

    spf = [t for t in resolver(domain, "TXT") if t.lower().startswith("v=spf1")]
    suggested_include = f" include:{provider['spf']}" if provider and provider.get("spf") else " include:<your provider>"
    if not spf:
        checks.append(("FAIL", "SPF", f'missing. Add a TXT record on {domain}: "v=spf1{suggested_include} ~all"'))
    elif len(spf) > 1:
        checks.append(("FAIL", "SPF", f"{len(spf)} SPF records found; merge them into one: {spf}"))
    else:
        record = spf[0]
        if re.search(r"[+]all\b", record) or record.rstrip().endswith(" all"):
            checks.append(("FAIL", "SPF", f"{record} lets anyone send as you; end it with ~all or -all"))
        elif provider and provider.get("spf") and provider["spf"] not in record:
            checks.append(("WARN", "SPF", f"{record} does not include {provider['spf']} ({provider['name']})"))
        else:
            checks.append(("OK", "SPF", record))

    selectors = dkim_selectors or COMMON_DKIM_SELECTORS
    found = [s for s in selectors if any("p=" in r for r in resolver(f"{s}._domainkey.{domain}", "TXT"))]
    if found:
        checks.append(("OK", "DKIM", "key published for selector(s): " + ", ".join(found)))
    else:
        checks.append((
            "WARN", "DKIM",
            "no key found under the usual selectors. Turn DKIM signing on in your mail provider's admin console "
            "(or re-run with --dkim-selector if you know yours).",
        ))

    dmarc = [t for t in resolver(f"_dmarc.{domain}", "TXT") if t.lower().startswith("v=dmarc1")]
    if not dmarc:
        checks.append((
            "FAIL", "DMARC",
            f'missing (Gmail and Yahoo require it). Add a TXT record on _dmarc.{domain}: '
            f'"v=DMARC1; p=none; rua=mailto:dmarc@{domain}"',
        ))
    else:
        policy = re.search(r"\bp=(\w+)", dmarc[0])
        checks.append(("OK", "DMARC", f"{dmarc[0]}" + (" (p=none is fine to start)" if policy and policy.group(1) == "none" else "")))

    return {"domain": domain, "provider": provider, "checks": checks}


def format_report(result: dict[str, Any]) -> str:
    lines = [f"Domain: {result['domain']}"]
    provider = result["provider"]
    lines.append(f"Mail provider: {provider['name'] if provider else 'not recognised from the MX records'}")
    for status, what, detail in result["checks"]:
        lines.append(f"  [{status:<4}] {what:<5} {detail}")
    if provider:
        host, port, security = provider["smtp"]
        lines += [
            "",
            "Suggested config.yaml settings:",
            f"  mail.smtp: host {host}, port {port}, security {security}",
            f"  mail.imap: host {provider['imap']}, port 993",
        ]
        if provider["note"]:
            lines.append(f"  Note: {provider['note']}")
    fails = sum(1 for s, _, _ in result["checks"] if s == "FAIL")
    lines += ["", f"{fails} blocking issue(s): fix them before sending." if fails else "No blocking issue found."]
    return "\n".join(lines)
