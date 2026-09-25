"""The data stages (collect, discover, scrape / enrich) as reusable batch functions."""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from .config import secret
from .db import Store
from .discovery import WebsiteFinder
from .emails import domain_accepts_mail
from .http import Fetcher
from .models import EmailCandidate
from .scraper import crawl_emails
from .sources import RechercheEntreprisesSource

log = logging.getLogger(__name__)


def collect_incremental(store: Store, cfg: dict[str, Any], max_new: int, source: Optional[Any] = None) -> int:
    """Pull the next pages of the configured registry search until `max_new` new companies.

    A cursor per query is kept in the database, so every call continues where the
    previous one stopped. Returns the number of new companies (0 once exhausted).
    """
    source = source or RechercheEntreprisesSource(user_agent=cfg["http"]["user_agent"])
    search = cfg["search"]
    exclude_individual = search.get("exclude_individual", True)
    new = 0
    for query in source.build_queries(search):
        key = "cursor:" + json.dumps(query, sort_keys=True, ensure_ascii=False)
        cursor = store.get_state(key, "1")
        if cursor == "done":
            continue
        page = int(cursor or 1)
        while new < max_new:
            companies, last = source.fetch_page(query, page, exclude_individual)
            new += store.upsert_companies(companies)[1]
            if last:
                store.set_state(key, "done")
                break
            page += 1
            store.set_state(key, page)
        if new >= max_new:
            break
    return new


def search_exhausted(store: Store, cfg: dict[str, Any], source: Optional[Any] = None) -> bool:
    queries = (source or RechercheEntreprisesSource).build_queries(cfg["search"])
    return all(
        store.get_state("cursor:" + json.dumps(q, sort_keys=True, ensure_ascii=False)) == "done" for q in queries
    )


class MxCache:
    """Thread-safe cache of "does this domain accept mail?" answers."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._known: dict[str, Optional[bool]] = {}
        self._lock = threading.Lock()

    def check(self, candidates: list[EmailCandidate]) -> dict[str, Optional[bool]]:
        if not self.enabled:
            return {}
        for c in candidates:
            domain = c.email.split("@", 1)[1]
            with self._lock:
                known = domain in self._known
            if not known:
                ok = domain_accepts_mail(domain)
                with self._lock:
                    self._known[domain] = ok
        with self._lock:
            return dict(self._known)


def _process(
    store: Store, cfg: dict[str, Any], rows: list[dict[str, Any]], work: Callable[[dict[str, Any]], Any],
    save: Callable[[dict[str, Any], Any], None],
) -> None:
    """Run `work` on each company in a thread pool, save results in the main thread (batched)."""
    if not rows:
        return
    with ThreadPoolExecutor(max_workers=cfg["http"]["workers"]) as pool, store.batch():
        futures = {pool.submit(work, row): row for row in rows}
        for n, fut in enumerate(as_completed(futures), 1):
            row = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # keep going on unexpected site errors
                log.warning("%s (%s): %s", row["name"], row["siren"], exc)
                result = None
            save(row, result)
            if n % 50 == 0:
                store.conn.commit()


def enrich_batch(store: Store, cfg: dict[str, Any], limit: Optional[int] = None, fetcher: Optional[Fetcher] = None) -> tuple[int, int, int]:
    """Website discovery + e-mail crawl in one pass per company.

    The pages loaded to verify the website (home, legal notice, contact) are reused by the
    e-mail crawl through the fetcher's cache: about 4 requests per company instead of 11.
    Returns (websites found, companies with an e-mail, companies processed).
    """
    fetcher = fetcher or Fetcher.from_config(cfg["http"])
    finder = WebsiteFinder(fetcher, cfg["discovery"], secret(cfg["discovery"].get("brave_api_key_env")))
    max_pages = cfg["scraping"]["max_pages_per_site"]
    mx = MxCache(cfg["scraping"]["check_mx"])
    rows = [dict(r) for r in store.companies(
        "discovery_done = 0 OR (scrape_done = 0 AND website IS NOT NULL AND website != '')", limit=limit
    )]
    counts = {"websites": 0, "emails": 0}

    def work(row: dict[str, Any]) -> tuple[str, str, int, list[EmailCandidate], dict[str, Optional[bool]]]:
        if row["discovery_done"]:
            url, source, conf = row["website"], row["website_source"] or "", row["website_confidence"] or 0
        else:
            url, source, conf = finder.find(row)
        candidates = crawl_emails(fetcher, url, max_pages) if url else []
        return url, source, conf, candidates, mx.check(candidates)

    def save(row: dict[str, Any], result: Any) -> None:
        url, source, conf, candidates, mx_ok = result or ("", "", 0, [], {})
        if url and not row["discovery_done"]:
            store.set_website(row["siren"], url, source, conf)
            counts["websites"] += 1
        if candidates:
            store.add_emails(row["siren"], candidates, mx_ok)
            counts["emails"] += 1
            log.info("%s -> %s: %s", row["name"], url, ", ".join(c.email for c in candidates[:3]))
        store.mark(row["siren"], "discovery_done", "scrape_done")

    _process(store, cfg, rows, work, save)
    return counts["websites"], counts["emails"], len(rows)


def discover_batch(store: Store, cfg: dict[str, Any], limit: Optional[int] = None, fetcher: Optional[Fetcher] = None) -> tuple[int, int]:
    """Find websites for companies not processed yet. Returns (found, processed)."""
    fetcher = fetcher or Fetcher.from_config(cfg["http"])
    finder = WebsiteFinder(fetcher, cfg["discovery"], secret(cfg["discovery"].get("brave_api_key_env")))
    rows = [dict(r) for r in store.companies("discovery_done = 0", limit=limit)]
    found = [0]

    def save(row: dict[str, Any], result: Any) -> None:
        url, source, conf = result or ("", "", 0)
        if url:
            store.set_website(row["siren"], url, source, conf)
            found[0] += 1
            log.info("%s -> %s (%s, %d%%)", row["name"], url, source, conf)
        else:
            store.mark(row["siren"], "discovery_done")

    _process(store, cfg, rows, finder.find, save)
    return found[0], len(rows)


def scrape_batch(store: Store, cfg: dict[str, Any], limit: Optional[int] = None, fetcher: Optional[Fetcher] = None) -> tuple[int, int]:
    """Crawl websites not scraped yet for e-mails. Returns (companies with e-mail, processed)."""
    fetcher = fetcher or Fetcher.from_config(cfg["http"])
    rows = [dict(r) for r in store.companies("scrape_done = 0 AND website IS NOT NULL AND website != ''", limit=limit)]
    max_pages = cfg["scraping"]["max_pages_per_site"]
    mx = MxCache(cfg["scraping"]["check_mx"])
    with_email = [0]

    def work(row: dict[str, Any]) -> tuple[list[EmailCandidate], dict[str, Optional[bool]]]:
        candidates = crawl_emails(fetcher, row["website"], max_pages)
        return candidates, mx.check(candidates)

    def save(row: dict[str, Any], result: Any) -> None:
        candidates, mx_ok = result or ([], {})
        store.add_emails(row["siren"], candidates, mx_ok)
        store.mark(row["siren"], "scrape_done")
        if candidates:
            with_email[0] += 1
            log.info("%s: %s", row["name"], ", ".join(c.email for c in candidates[:3]))

    _process(store, cfg, rows, work, save)
    return with_email[0], len(rows)
