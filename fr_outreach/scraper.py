"""Crawl a company website (a handful of relevant pages) and collect e-mails."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from .discovery import links, same_site
from .emails import extract_emails, rank
from .http import Fetcher
from .models import EmailCandidate

CONTACT_HINT = re.compile(
    r"contact|mentions?[-_ ]?l[eé]gales?|legal|a-propos|about|qui-sommes|equipe|team|societe|entreprise|agence|coordonn",
    re.I,
)
FALLBACK_PATHS = ["/contact", "/nous-contacter", "/contactez-nous", "/mentions-legales"]
# A role address on the company's own domain (contact@, direction@...): no need to look further.
GOOD_ENOUGH_SCORE = 90


def _priority(url: str, label: str) -> int:
    text = f"{urlsplit(url).path} {label}"
    if re.search(r"contact|coordonn", text, re.I):
        return 0
    if re.search(r"mention|l[ée]gal", text, re.I):
        return 1
    return 2


def crawl_emails(fetcher: Fetcher, website: str, max_pages: int = 6) -> list[EmailCandidate]:
    home = fetcher.get(website)
    if home is None:
        return []
    found: list[tuple[str, str]] = [(e, home.url) for e in extract_emails(home.text)]
    ranked: dict[str, int] = {}
    for url, label in links(home.text, home.url):
        url = url.split("#")[0]
        if url.rstrip("/") == home.url.rstrip("/") or not same_site(url, home.url):
            continue
        if CONTACT_HINT.search(urlsplit(url).path) or CONTACT_HINT.search(label):
            ranked[url] = min(_priority(url, label), ranked.get(url, 9))
    queue = sorted(ranked, key=ranked.__getitem__)
    if not any(p == 0 for p in ranked.values()):  # no contact link: try the usual paths
        queue += [u for u in (urljoin(home.url, p) for p in FALLBACK_PATHS) if u not in ranked]

    visited = {home.url}
    for url in queue:
        best = rank(found, home.url)
        if best and best[0].score >= GOOD_ENOUGH_SCORE:
            break
        if len(visited) >= max_pages:
            break
        if url in visited:
            continue
        visited.add(url)
        page = fetcher.get(url)
        if page is not None:
            found += [(e, page.url) for e in extract_emails(page.text)]
    return rank(found, home.url)
