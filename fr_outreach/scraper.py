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
FALLBACK_PATHS = ["/contact", "/nous-contacter", "/contactez-nous", "/mentions-legales", "/a-propos"]


def crawl_emails(fetcher: Fetcher, website: str, max_pages: int = 6) -> list[EmailCandidate]:
    home = fetcher.get(website)
    if home is None:
        return []
    found: list[tuple[str, str]] = [(e, home.url) for e in extract_emails(home.text)]
    queue: list[str] = []
    for url, label in links(home.text, home.url):
        url = url.split("#")[0]
        if same_site(url, home.url) and (CONTACT_HINT.search(urlsplit(url).path) or CONTACT_HINT.search(label)):
            if url not in queue and url.rstrip("/") != home.url.rstrip("/"):
                queue.append(url)
    for path in FALLBACK_PATHS:
        url = urljoin(home.url, path)
        if url not in queue:
            queue.append(url)

    visited = {home.url}
    for url in queue:
        if len(visited) >= max_pages:
            break
        if url in visited:
            continue
        visited.add(url)
        page = fetcher.get(url)
        if page is not None:
            found += [(e, page.url) for e in extract_emails(page.text)]
    return rank(found, home.url)
