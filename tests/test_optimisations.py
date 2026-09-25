"""Tests for follow-ups, A/B variants, the faster DB layer and the leaner crawl."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from fr_outreach.db import Store
from fr_outreach.discovery import WebsiteFinder
from fr_outreach.http import Fetcher, Page
from fr_outreach.mailer import Campaign, run_campaign
from fr_outreach.models import EmailCandidate
from fr_outreach.pipeline import enrich_batch
from fr_outreach.schedule import Schedule
from fr_outreach.scraper import crawl_emails

from test_pipeline import FakeFetcher, FakeSender, add_company, make_cfg

UTC = timezone.utc
MONDAY = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
FRIDAY = MONDAY + timedelta(days=4)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.cfg["mail"]["max_per_run"] = 0
        self.now = MONDAY
        self.store = Store(":memory:", clock=lambda: self.now)
        self.sent: list = []

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def send(self) -> dict:
        return run_campaign(self.store, self.cfg, really_send=True, sender_factory=FakeSender(self.sent), sleep=lambda s: None)


class FollowUpTests(Base):
    def test_business_day_cutoff(self):
        s = Schedule({"timezone": "Europe/Paris"})
        # Friday, 4 business days back -> anything sent on Monday (Paris time) or before is due.
        self.assertEqual(s.business_days_cutoff(FRIDAY, 4), datetime(2026, 9, 28, 22, 0, tzinfo=UTC))

    def test_threaded_followup_only_without_reply(self):
        add_company(self.store, "111111111", ["contact@alpha.fr"], "ALPHA")
        add_company(self.store, "222222222", ["contact@beta.fr"], "BETA")
        self.assertEqual(self.send()["sent"], 2)
        first = {m["To"]: m for m in self.sent}
        self.now = MONDAY + timedelta(days=1)
        self.assertEqual(self.send()["sent"], 0)  # not due yet
        self.now = FRIDAY
        self.store.record_inbox_event("<r@alpha.fr>", "reply", "jean@alpha.fr", "Re", "111111111", "contact@alpha.fr")
        self.assertEqual(self.send()["sent"], 1)
        follow = self.sent[-1]
        self.assertEqual(follow["To"], "contact@beta.fr")
        self.assertEqual(follow["Subject"], "Re: BETA : une question")
        self.assertEqual(follow["In-Reply-To"], first["contact@beta.fr"]["Message-ID"])
        self.assertIn("Petite relance", follow.get_content())
        self.assertIn("STOP", follow.get_content())  # compliance footer on follow-ups too
        self.now = FRIDAY + timedelta(days=3)
        self.assertEqual(self.send()["sent"], 0)  # only one follow-up

    def test_stale_followups_expire(self):
        add_company(self.store, "111111111", ["contact@alpha.fr"])
        self.send()
        self.now = MONDAY + timedelta(days=30)
        self.assertEqual(Campaign(self.store, self.cfg, live=True).followups_due(self.now), [])

    def test_followups_share_the_quota_with_new_companies(self):
        for i in range(10):
            add_company(self.store, f"1{i:08d}", [f"contact@old{i}.fr"])
        self.cfg["mail"]["max_per_day"] = 10
        self.assertEqual(self.send()["sent"], 10)
        for i in range(10):
            add_company(self.store, f"2{i:08d}", [f"contact@new{i}.fr"])
        self.now = FRIDAY
        campaign = Campaign(self.store, self.cfg, live=True)
        plan = campaign.plan(self.now, 10)
        self.assertEqual(sorted(step for _, step in plan), [1] * 5 + [2] * 5)
        # one message at a time (the agent): follow-ups and first e-mails alternate
        steps = []
        for _ in range(6):
            row, step = campaign.plan(self.now, 1)[0]
            campaign.deliver(FakeSender([]), row, step)
            steps.append(step)
        self.assertEqual(steps, [2, 1, 2, 1, 2, 1])

    def test_followups_fill_the_quota_when_nothing_new(self):
        for i in range(6):
            add_company(self.store, f"1{i:08d}", [f"contact@old{i}.fr"])
        self.send()
        self.now = FRIDAY
        plan = Campaign(self.store, self.cfg, live=True).plan(self.now, 6)
        self.assertEqual([step for _, step in plan], [2] * 6)


class VariantTests(Base):
    def test_ab_variants_are_stable_and_reported(self):
        for name, subject in (("court", "Question rapide"), ("long", "$company_name : une idée")):
            Path(self.tmp.name, f"{name}.txt").write_text(f"Subject: {subject}\n\nBonjour,\nTexte.\n", encoding="utf-8")
        self.cfg["mail"]["templates"] = [str(Path(self.tmp.name, n + ".txt")) for n in ("court", "long")]
        for i in range(40):
            add_company(self.store, f"{i:09d}", [f"contact@s{i}.fr"])
        self.cfg["mail"]["max_per_day"] = 40
        self.cfg["mail"]["warmup"] = {}
        self.assertEqual(self.send()["sent"], 40)
        variants = dict(self.store.conn.execute("SELECT siren, variant FROM sends").fetchall())
        self.assertEqual(set(variants.values()), {"court", "long"})
        campaign = Campaign(self.store, self.cfg, live=True)
        self.assertTrue(all(campaign.variant(s)[0] == v for s, v in variants.items()))  # stable per company
        replied = next(s for s, v in variants.items() if v == "court")
        self.store.record_inbox_event("<r1>", "reply", "a@b.fr", "Re", replied, "")
        stats = {r["variant"]: dict(r) for r in self.store.variant_stats(campaign.name, "2000")}
        self.assertEqual(stats["court"]["replies"], 1)
        self.assertEqual(stats["long"]["replies"], 0)
        self.assertEqual(stats["court"]["sent"] + stats["long"]["sent"], 40)


class StoreTests(Base):
    def test_best_addresses_first_during_warmup(self):
        add_company(self.store, "111111111", [])
        self.store.add_emails("111111111", [EmailCandidate("acme.lyon@orange.fr", "u", 55)], {})
        add_company(self.store, "222222222", [])
        self.store.add_emails("222222222", [EmailCandidate("contact@beta.fr", "u", 95)], {})
        fifo = [r["email"] for r in self.store.recipients("c")]
        best = [r["email"] for r in self.store.recipients("c", best_first=True)]
        self.assertEqual(fifo, ["acme.lyon@orange.fr", "contact@beta.fr"])
        self.assertEqual(best, ["contact@beta.fr", "acme.lyon@orange.fr"])

    def test_old_database_is_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.sqlite")
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE sends (id INTEGER PRIMARY KEY AUTOINCREMENT, siren TEXT NOT NULL, email TEXT NOT NULL,"
                " campaign TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT, error TEXT, created_at TEXT,"
                " UNIQUE(email, campaign));"
                "INSERT INTO sends (siren, email, campaign, status, created_at) VALUES ('1', 'a@b.fr', 'c', 'sent', 'x');"
            )
            conn.commit()
            conn.close()
            store = Store(path)
            row = store.conn.execute("SELECT email, step, status FROM sends").fetchone()
            self.assertEqual(tuple(row), ("a@b.fr", 1, "sent"))
            store.record_send("1", "a@b.fr", "c", "sent", step=2)  # the follow-up fits next to it
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0], 2)
            store.close()

    def test_batch_commits_once(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "db.sqlite"))
            with store.batch():
                add_company(store, "111111111", ["contact@alpha.fr"])
                self.assertTrue(store.conn.in_transaction)
            self.assertFalse(store.conn.in_transaction)
            store.close()


class FakeResponse:
    def __init__(self, url, body=b"<html>ok</html>", status=200, ctype="text/html; charset=utf-8"):
        self.url, self.status_code, self._body = url, status, body
        self.headers = {"Content-Type": ctype}
        self.text = body.decode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def iter_content(self, size):
        yield self._body


class FakeSession:
    def __init__(self, fail_hosts=()):
        self.headers, self.calls, self.fail_hosts = {}, [], set(fail_hosts)

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        if any(h in url for h in self.fail_hosts):
            raise requests.ConnectionError("connection refused")
        return FakeResponse(url, status=404 if url.endswith("robots.txt") else 200)


class CrawlTests(unittest.TestCase):
    def test_fetcher_caches_pages_and_skips_dead_sites(self):
        session = FakeSession(fail_hosts={"dead.fr"})
        f = Fetcher("bot", per_host_delay=0, session=session)
        self.assertIsNotNone(f.get("https://acme.fr/"))
        self.assertIsNotNone(f.get("https://acme.fr/"))
        self.assertEqual(session.calls, ["https://acme.fr/robots.txt", "https://acme.fr/"])
        self.assertIsNone(f.get("https://dead.fr/"))
        self.assertIsNone(f.get("https://dead.fr/contact"))
        self.assertEqual(session.calls[2:], ["https://dead.fr/robots.txt"])  # nothing more tried on a dead site
        self.assertEqual(f.requests, 3)

    def test_crawl_stops_once_a_good_address_is_found(self):
        fetcher = FakeFetcher({
            "https://acme.fr/": '<a href="/nous-contacter">Contact</a> <a href="/equipe">Equipe</a> contact@acme.fr',
            "https://acme.fr/nous-contacter": "direction@acme.fr",
        })
        self.assertEqual([c.email for c in crawl_emails(fetcher, "https://acme.fr/")], ["contact@acme.fr"])
        self.assertEqual(fetcher.requested, ["https://acme.fr/"])

    def test_fallback_paths_only_without_contact_link(self):
        fetcher = FakeFetcher({"https://acme.fr/": '<a href="/contactez-nous">Nous écrire</a>',
                               "https://acme.fr/contactez-nous": "hello@acme.fr"})
        crawl_emails(fetcher, "https://acme.fr/")
        self.assertNotIn("https://acme.fr/contact", fetcher.requested)

    def test_parked_domain_is_rejected(self):
        page = Page("https://www.acme.fr/", 200, "<title>ACME</title> acme.fr - Ce nom de domaine est à vendre ! 552100554")
        self.assertEqual(WebsiteFinder.confidence([page], {"siren": "552100554", "name": "ACME"}), 0)

    def test_enrich_finds_website_and_emails_in_one_pass(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(d)
            cfg["discovery"]["guess_domains"] = False
            cfg["scraping"]["check_mx"] = False
            store = Store(":memory:")
            add_company(store, "552100554", [])  # website https://acme.fr from the source, not verified yet
            fetcher = FakeFetcher({
                "https://acme.fr": '<a href="/mentions-legales">Mentions légales</a><a href="/contact">Contact</a>',
                "https://acme.fr/mentions-legales": "ACME INDUSTRIE - SIREN 552 100 554",
                "https://acme.fr/contact": "contact@acme.fr",
            })
            self.assertEqual(enrich_batch(store, cfg, fetcher=fetcher), (1, 1, 1))
            row = store.conn.execute("SELECT discovery_done, scrape_done, website_confidence FROM companies").fetchone()
            self.assertEqual(tuple(row)[:2], (1, 1))
            self.assertGreaterEqual(row[2], 80)
            self.assertEqual([r["email"] for r in store.recipients("c")], ["contact@acme.fr"])
            store.close()


if __name__ == "__main__":
    unittest.main()
