"""Command line entry point: `fr-outreach <command>` (or `python -m fr_outreach`)."""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import load_config, secret
from .db import Store
from .discovery import WebsiteFinder
from .emails import domain_accepts_mail
from .http import Fetcher
from .inbox import sync_unsubscribes
from .mailer import ComplianceError, run_campaign
from .scraper import crawl_emails
from .sources import CsvSource, PappersSource, RechercheEntreprisesSource, SireneStockSource

log = logging.getLogger("fr_outreach")


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def search_filters(cfg: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    s = dict(cfg["search"])
    for arg, key in [
        ("departments", "departments"), ("regions", "regions"), ("naf", "naf_codes"),
        ("naf_sections", "naf_sections"), ("categories", "categories"), ("postal_codes", "postal_codes"),
    ]:
        if getattr(args, arg, None):
            s[key] = _split(getattr(args, arg))
    if getattr(args, "query", None):
        s["query"] = args.query
    if getattr(args, "max_results", None):
        s["max_results"] = args.max_results
    for arg in ("revenue_min", "revenue_max"):
        if getattr(args, arg, None) is not None:
            s[arg] = getattr(args, arg)
    return s


# -- commands -----------------------------------------------------------------
def cmd_collect(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    if args.source == "api":
        source: Any = RechercheEntreprisesSource(user_agent=cfg["http"]["user_agent"])
    elif args.source == "sirene":
        if not args.unites_legales:
            sys.exit("--unites-legales is required for --source sirene")
        source = SireneStockSource(args.unites_legales, args.etablissements)
    elif args.source == "pappers":
        source = PappersSource(secret(cfg["pappers"]["api_token_env"]))
    else:
        if not args.csv:
            sys.exit("--csv is required for --source csv")
        source = CsvSource(args.csv, label=args.label or "csv")
    new = total = 0
    for company in source.search(search_filters(cfg, args)):
        total += 1
        new += store.upsert_company(company)
        if total % 100 == 0:
            log.info("%d companies collected (%d new)", total, new)
    print(f"Collected {total} companies ({new} new) from {args.source}.")


def cmd_discover(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    fetcher = Fetcher.from_config(cfg["http"])
    finder = WebsiteFinder(fetcher, cfg["discovery"], secret(cfg["discovery"].get("brave_api_key_env")))
    rows = [dict(r) for r in store.companies("discovery_done = 0", limit=args.limit)]
    found = 0
    with ThreadPoolExecutor(max_workers=cfg["http"]["workers"]) as pool:
        futures = {pool.submit(finder.find, row): row for row in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                url, source, conf = fut.result()
            except Exception as exc:  # keep going on unexpected site errors
                log.warning("discovery failed for %s: %s", row["siren"], exc)
                store.mark(row["siren"], "discovery_done")
                continue
            if url:
                store.set_website(row["siren"], url, source, conf)
                found += 1
                log.info("%s -> %s (%s, %d%%)", row["name"], url, source, conf)
            else:
                store.mark(row["siren"], "discovery_done")
    print(f"Website found for {found}/{len(rows)} companies.")


def cmd_scrape(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    fetcher = Fetcher.from_config(cfg["http"])
    rows = store.companies("scrape_done = 0 AND website IS NOT NULL AND website != ''", limit=args.limit)
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
            added = store.add_emails(row["siren"], candidates, mx_cache)
            store.mark(row["siren"], "scrape_done")
            if candidates:
                with_email += 1
                log.info("%s: %s", row["name"], ", ".join(c.email for c in candidates[:3]))
            elif added == 0:
                log.debug("%s: no e-mail found", row["name"])
    print(f"E-mails found for {with_email}/{len(rows)} websites.")


def cmd_send(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    if args.campaign:
        cfg["mail"]["campaign"] = args.campaign
    if args.send and not args.yes:
        answer = input("This will send REAL e-mails. Type 'send' to continue: ")
        if answer.strip() != "send":
            sys.exit("Aborted.")
    try:
        counts = run_campaign(store, cfg["mail"], cfg["scraping"], really_send=args.send, limit=args.limit)
    except ComplianceError as exc:
        sys.exit(f"Refusing to send: {exc}")
    if args.send:
        print(f"Sent {counts['sent']}, failed {counts['failed']}.")
    else:
        print(
            f"DRY RUN: {counts['dry_run']} messages written to "
            f"{cfg['mail']['outbox_dir']}/{cfg['mail']['campaign']}/ - review them, then re-run with --send."
        )


def cmd_run(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    cmd_collect(cfg, store, args)
    cmd_discover(cfg, store, args)
    cmd_scrape(cfg, store, args)
    cmd_send(cfg, store, args)


def cmd_suppress(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    for value in args.values:
        store.suppress(value, args.reason)
    print(f"{len(args.values)} value(s) added to the suppression list.")


def cmd_sync_inbox(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    counts = sync_unsubscribes(store, cfg["mail"]["imap"], mark_seen=args.mark_seen)
    print(f"Opt-outs: {counts['optout']}, bounces: {counts['bounce']}, other replies: {counts['other']}.")


def cmd_export(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    rows = store.conn.execute(
        "SELECT c.siren, c.name, c.category, c.naf, c.headcount_band, c.city, c.postal_code, c.department, "
        "c.revenue, c.website, c.website_confidence, e.email, e.score, e.kind, e.source_url, "
        "(SELECT s.status FROM sends s WHERE s.email = e.email ORDER BY s.id DESC LIMIT 1) AS last_status "
        "FROM companies c LEFT JOIN emails e ON e.siren = c.siren ORDER BY c.siren, e.score DESC"
    ).fetchall()
    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(rows[0].keys() if rows else ["siren"])
        writer.writerows([tuple(r) for r in rows])
    print(f"Wrote {len(rows)} rows to {args.output}.")


def cmd_stats(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    for key, value in store.stats().items():
        print(f"{key:>18}: {value}")


# -- argument parsing -----------------------------------------------------------
def add_collect_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", choices=["api", "sirene", "pappers", "csv"], default="api",
                   help="api = API Recherche d'entreprises / Annuaire des Entreprises (default)")
    p.add_argument("--query", help="free-text search (name, activity...)")
    p.add_argument("--departments", help="e.g. 69,38,42")
    p.add_argument("--regions", help="INSEE region codes, e.g. 84 (Auvergne-Rhône-Alpes)")
    p.add_argument("--postal-codes")
    p.add_argument("--naf", help="NAF codes, e.g. 62.01Z,62.02A")
    p.add_argument("--naf-sections", help="NAF sections A..U, e.g. C,J,M")
    p.add_argument("--categories", help="PME,ETI,GE")
    p.add_argument("--revenue-min", type=int)
    p.add_argument("--revenue-max", type=int)
    p.add_argument("--max-results", type=int)
    p.add_argument("--unites-legales", help="SIRENE StockUniteLegale csv/zip")
    p.add_argument("--etablissements", help="SIRENE StockEtablissement csv/zip")
    p.add_argument("--csv", help="CSV export (Dealroom, Crunchbase, Pépites Tech, Diane...)")
    p.add_argument("--label", help="source label stored with CSV imports")


def add_send_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--send", action="store_true", help="really send (default is a dry run to the outbox)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--campaign")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fr-outreach", description=__doc__)
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="1. pull companies from a French registry/database")
    add_collect_args(p)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("discover", help="2. find and verify each company's website")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("scrape", help="3. crawl websites for professional e-mail addresses")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("send", help="4. send the campaign (dry run unless --send)")
    p.add_argument("--limit", type=int)
    add_send_args(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("run", help="collect + discover + scrape + send in one go")
    add_collect_args(p)
    add_send_args(p)
    p.add_argument("--limit", type=int, help="max companies per stage / messages")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("suppress", help="add e-mails or @domains to the do-not-contact list")
    p.add_argument("values", nargs="+")
    p.add_argument("--reason", default="manual")
    p.set_defaults(func=cmd_suppress)

    p = sub.add_parser("sync-inbox", help="read replies over IMAP; record opt-outs and bounces")
    p.add_argument("--mark-seen", action="store_true")
    p.set_defaults(func=cmd_sync_inbox)

    p = sub.add_parser("export", help="export companies + e-mails to CSV for review")
    p.add_argument("-o", "--output", default="export.csv")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("stats", help="pipeline counters")
    p.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # A missing default config.yaml is fine (built-in defaults); a missing explicit path is an error.
    cfg = load_config(args.config if os.path.exists(args.config) or args.config != "config.yaml" else None)
    store = Store(cfg["database"])
    try:
        args.func(cfg, store, args)
    finally:
        store.close()


if __name__ == "__main__":
    main()
