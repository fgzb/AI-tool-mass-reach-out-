"""Command line entry point: `fr-outreach <command>` (or `python -m fr_outreach`)."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from .agent import Agent, AgentLocked, pause, resume
from .config import load_config, load_dotenv, secret
from .db import Store
from .domain_check import check_domain, format_report
from .inbox import sync_inbox
from .mailer import Campaign, ComplianceError, SendBlocked, run_campaign
from .pipeline import discover_batch, enrich_batch, scrape_batch
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


def confirm_live(args: argparse.Namespace, what: str) -> None:
    if args.yes or not sys.stdin.isatty():
        return
    if input(f"This will {what}. Type 'send' to continue: ").strip() != "send":
        sys.exit("Aborted.")


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
    total, new = store.upsert_companies(source.search(search_filters(cfg, args)))
    print(f"Collected {total} companies ({new} new) from {args.source}.")


def cmd_discover(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    found, n = discover_batch(store, cfg, args.limit)
    print(f"Website found for {found}/{n} companies.")


def cmd_scrape(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    with_email, n = scrape_batch(store, cfg, args.limit)
    print(f"E-mails found for {with_email}/{n} websites.")


def cmd_enrich(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    websites, with_email, n = enrich_batch(store, cfg, args.limit)
    print(f"{n} companies processed: {websites} new websites, e-mails found for {with_email}.")


def cmd_send(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    if args.campaign:
        cfg["mail"]["campaign"] = args.campaign
    if args.send:
        confirm_live(args, "send REAL e-mails")
    try:
        counts = run_campaign(store, cfg, really_send=args.send, limit=args.limit)
    except (ComplianceError, SendBlocked) as exc:
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
    cmd_enrich(cfg, store, args)
    cmd_send(cfg, store, args)


def cmd_agent(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    if args.campaign:
        cfg["mail"]["campaign"] = args.campaign
    try:
        agent = Agent(store, cfg, live=args.live)
        agent.preflight()
    except ComplianceError as exc:
        sys.exit(f"Refusing to start: {exc}")
    if args.live:
        confirm_live(args, "start the agent in LIVE mode (it sends real e-mails every day)")
    try:
        agent.run(once=args.once, force=args.force)
    except AgentLocked as exc:
        sys.exit(f"{exc}. Use --force if you are sure it is not running.")
    except KeyboardInterrupt:
        print("Agent stopped.")


def cmd_status(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    now = datetime.now(timezone.utc)
    try:
        campaign = Campaign(store, cfg, live=True, strict=False)
    except (ComplianceError, OSError) as exc:
        print(f"(mail settings incomplete: {exc})")
        campaign = None
    paused = store.get_state("paused")
    print(f"{'agent':>22}: {'PAUSED - ' + paused if paused else 'not paused'}")
    if campaign:
        sched = campaign.schedule
        local = sched.local(now)
        print(f"{'campaign':>22}: {campaign.name}"
              + (" (template still has [PLACEHOLDERS]: live sending is blocked)" if campaign.has_placeholders else ""))
        print(f"{'now (local)':>22}: {local:%a %d/%m %H:%M}, {'inside' if sched.in_window(now) else 'outside'} "
              f"the sending window ({sched.start:%H:%M}-{sched.end:%H:%M}, sending day: {sched.is_sending_day(local.date())})")
        print(f"{'quota today':>22}: {campaign.quota_today(now)} (sent/failed today: {campaign.done_today(now)})")
        print(f"{'ready to contact':>22}: {campaign.ready()}")
        if campaign.followup:
            print(f"{'follow-ups due':>22}: {len(campaign.followups_due(now))}")
        if len(campaign.variants) > 1:
            print(f"{'A/B variants':>22}: {', '.join(v[0] for v in campaign.variants)}")
    print(f"{'next send after':>22}: {store.get_state('next_send_at', '-')}")
    print(f"{'last inbox sync':>22}: {store.get_state('inbox:last_sync', '-')}")
    cursors = store.states("cursor:")
    done = sum(1 for v in cursors.values() if v == "done")
    print(f"{'registry queries':>22}: {done}/{len(cursors)} finished"
          + (" - search exhausted" if store.get_state("search_exhausted") else ""))
    for key, value in store.stats().items():
        print(f"{key:>22}: {value}")


def cmd_pause(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    pause(store, args.reason)
    print("Agent paused: nothing will be sent until `fr-outreach resume`.")


def cmd_resume(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    resume(store)
    print("Agent resumed.")


def cmd_suppress(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    for value in args.values:
        store.suppress(value, args.reason)
    print(f"{len(args.values)} value(s) added to the suppression list.")


def cmd_sync_inbox(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    c = sync_inbox(store, cfg["mail"]["imap"], days=args.days)
    print(f"Replies: {c['reply']}, opt-outs: {c['optout']}, bounces: {c['bounce']}, "
          f"auto-replies: {c['auto']}, unrelated: {c['other']}.")


def cmd_check_domain(cfg: dict[str, Any], store: Store, args: argparse.Namespace) -> None:
    target = args.domain or cfg["mail"].get("from_address")
    if not target:
        sys.exit("Give a domain or an e-mail address (or set mail.from_address).")
    try:
        result = check_domain(target, dkim_selectors=_split(args.dkim_selector) or None)
    except Exception as exc:  # DNS / network failure
        sys.exit(f"DNS lookup failed ({exc}). Install dnspython or check the network, then retry.")
    print(format_report(result))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


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

    p = sub.add_parser("agent", help="run everything automatically, every day (rehearsal unless --live)")
    p.add_argument("--live", action="store_true", help="really send e-mails")
    p.add_argument("--once", action="store_true", help="run a single iteration (for cron)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--force", action="store_true", help="take over a lock left by a crashed agent")
    p.add_argument("--campaign")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("status", help="agent state, today's quota, pipeline counters")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pause", help="stop the agent from sending")
    p.add_argument("--reason", default="paused manually")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="let the agent send again")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("check-domain", help="check SPF / DKIM / DMARC / MX of the sending domain")
    p.add_argument("domain", nargs="?", help="domain or e-mail address (default: mail.from_address)")
    p.add_argument("--dkim-selector", help="DKIM selector(s) to look up, comma-separated")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check_domain)

    p = sub.add_parser("collect", help="1. pull companies from a French registry/database")
    add_collect_args(p)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("enrich", help="2+3. find each company's website and its e-mails in one pass (fastest)")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("discover", help="2. find and verify each company's website")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("scrape", help="3. crawl websites for professional e-mail addresses")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("send", help="4. send the campaign once (dry run unless --send)")
    p.add_argument("--limit", type=int)
    add_send_args(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("run", help="collect + enrich + send, once")
    add_collect_args(p)
    add_send_args(p)
    p.add_argument("--limit", type=int, help="max companies per stage / messages")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("suppress", help="add e-mails or @domains to the do-not-contact list")
    p.add_argument("values", nargs="+")
    p.add_argument("--reason", default="manual")
    p.set_defaults(func=cmd_suppress)

    p = sub.add_parser("sync-inbox", help="read replies over IMAP; record opt-outs, bounces and replies")
    p.add_argument("--days", type=int, default=14, help="look back this many days on the first sync")
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
    load_dotenv()
    # A missing default config.yaml is fine (built-in defaults); a missing explicit path is an error.
    cfg = load_config(args.config if os.path.exists(args.config) or args.config != "config.yaml" else None)
    store = Store(cfg["database"])
    try:
        args.func(cfg, store, args)
    finally:
        store.close()


if __name__ == "__main__":
    main()
