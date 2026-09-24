"""The data stages (collect, discover, scrape) as reusable batch functions."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from .config import secret
from .db import Store
from .discovery import WebsiteFinder
from .emails import domain_accepts_mail
from .http import Fetcher
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
            new += sum(store.upsert_company(c) for c in companies)
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


def discover_batch(store: Store, cfg: dict[str, Any], limit: Optional[int] = None, fetcher: Optional[Fetcher] = None) -> tuple[int, int]:
    """Find websites for companies not processed yet. Returns (found, processed)."""
    fetcher = fetcher or Fetcher.from_config(cfg["http"])
    finder = WebsiteFinder(fetcher, cfg["discovery"], secret(cfg["discovery"].get("brave_api_key_env")))
    rows = [dict(r) for r in store.companies("discovery_done = 0", limit=limit)]
    found = 0
    with ThreadPoolExecutor(max_workers=cfg["http"]["workers"]) as pool:
        futures = {pool.submit(finder.find, row): row for row in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                url, source, conf = fut.result()
            except Exception as exc:  # keep going on unexpected site errors
                log.warning("discovery failed for %s: %s", row["siren"], exc)
                url, source, conf = "", "", 0
            if url:
                store.set_website(row["siren"], url, source, conf)
                found += 1
                log.info("%s -> %s (%s, %d%%)", row["name"], url, source, conf)
            else:
                store.mark(row["siren"], "discovery_done")
    return found, len(rows)


def scrape_batch(store: Store, cfg: dict[str, Any], limit: Optional[int] = None, fetcher: Optional[Fetcher] = None) -> tuple[int, int]:
    """Crawl websites not scraped yet for e-mails. Returns (companies with e-mail, processed)."""
    fetcher = fetcher or Fetcher.from_config(cfg["http"])
    rows = store.companies("scrape_done = 0 AND website IS NOT NULL AND website != ''", limit=limit)
    max_pages = cfg["scraping"]["max_pages_per_site"]
    mx_cache: dict[str, Any] = {}
    with_email = 0
    with ThreadPoolExecutor(max_workers=cfg["http"]["workers"]) as pool:
        futures = {pool.submit(crawl_emails, fetcher, r["website"], max_pages): r for r in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                candidates = fut.result()
            except Exception as exc:
                log.warning("scrape failed for %s: %s", row["website"], exc)
                candidates = []
            if cfg["scraping"]["check_mx"]:
                for c in candidates:
                    dom = c.email.split("@", 1)[1]
                    if dom not in mx_cache:
                        mx_cache[dom] = domain_accepts_mail(dom)
            store.add_emails(row["siren"], candidates, mx_cache)
            store.mark(row["siren"], "scrape_done")
            if candidates:
                with_email += 1
                log.info("%s: %s", row["name"], ", ".join(c.email for c in candidates[:3]))
    return with_email, len(rows)
