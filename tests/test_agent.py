"""Offline tests for the autonomous agent: schedule, quotas, health, inbox, refill, domain check."""
from __future__ import annotations

import os
import smtplib
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fr_outreach.agent import Agent, resume
from fr_outreach.db import Store
from fr_outreach.domain_check import check_domain
from fr_outreach.inbox import sync_inbox
from fr_outreach.models import Company
from fr_outreach.pipeline import collect_incremental, search_exhausted
from fr_outreach.schedule import Schedule, daily_quota, easter

from test_pipeline import FakeSender, add_company, make_cfg

UTC = timezone.utc
MONDAY = datetime(2026, 9, 28, tzinfo=UTC)  # 09:00 in Paris is 07:00 UTC in September


def at(day: datetime, hh: int, mm: int = 0) -> datetime:
    return day.replace(hour=hh, minute=mm)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.s = Schedule({"timezone": "Europe/Paris", "start": "09:00", "end": "17:00",
                           "skip_periods": ["12-24..01-02"]})

    def test_easter(self):
        self.assertEqual(easter(2025), date(2025, 4, 20))
        self.assertEqual(easter(2026), date(2026, 4, 5))
        self.assertEqual(easter(2027), date(2027, 3, 28))

    def test_sending_days(self):
        self.assertTrue(self.s.is_sending_day(date(2026, 9, 28)))    # Monday
        self.assertFalse(self.s.is_sending_day(date(2026, 9, 26)))   # Saturday
        self.assertFalse(self.s.is_sending_day(date(2026, 7, 14)))   # Fête nationale (Tuesday)
        self.assertFalse(self.s.is_sending_day(date(2026, 5, 14)))   # Ascension
        self.assertFalse(self.s.is_sending_day(date(2026, 4, 6)))    # lundi de Pâques
        self.assertFalse(self.s.is_sending_day(date(2026, 12, 31)))  # skip period over new year
        self.assertTrue(self.s.is_sending_day(date(2027, 1, 4)))

    def test_window_uses_paris_time(self):
        self.assertFalse(self.s.in_window(at(MONDAY, 6, 59)))  # 08:59 Paris
        self.assertTrue(self.s.in_window(at(MONDAY, 7, 0)))
        self.assertTrue(self.s.in_window(at(MONDAY, 14, 59)))
        self.assertFalse(self.s.in_window(at(MONDAY, 15, 0)))  # 17:00 Paris

    def test_warmup_quota(self):
        store = Store(":memory:")
        mail = {"max_per_day": 40, "warmup": {"start_per_day": 15, "increase_per_day": 5}}
        self.assertEqual(daily_quota(store, mail, MONDAY), 15)
        for i, day in enumerate(["2026-09-21", "2026-09-22", "2026-09-23"]):
            store.conn.execute(
                "INSERT INTO sends (siren, email, campaign, status, created_at) VALUES (?, ?, 'c', 'sent', ?)",
                (str(i), f"a{i}@x.fr", day + "T08:00:00+00:00"),
            )
        self.assertEqual(daily_quota(store, mail, MONDAY), 30)
        mail["max_per_day"] = 25
        self.assertEqual(daily_quota(store, mail, MONDAY), 25)


class FakeImap:
    def __init__(self, messages: dict[int, bytes], validity: bytes = b"7"):
        self.messages, self.validity = messages, validity
        self.fetched: list[int] = []

    def __call__(self, cfg):  # used as the connect factory
        return self

    def select(self, folder, readonly=True):
        assert readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.validity]

    def uid(self, command, *args):
        if command == "SEARCH":
            if args[1] == "SINCE":
                uids = sorted(self.messages)
            else:  # "UID n:*" always returns at least the highest UID, like a real server
                low = int(args[2].split(":")[0])
                uids = [u for u in sorted(self.messages) if u >= low] or [max(self.messages)]
            return "OK", [" ".join(map(str, uids)).encode()]
        uid = int(args[0])
        self.fetched.append(uid)
        return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[uid]), b")"]

    def logout(self):
        pass


def raw(headers: str, body: str) -> bytes:
    return (headers.strip() + "\n\n" + body).encode()


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.cfg["agent"]["ready_buffer"] = 0  # refill is tested separately
        self.now = at(MONDAY, 6)
        self.store = Store(":memory:", clock=lambda: self.now)
        self.sent: list = []
        self.imap = FakeImap({1: raw("From: x@y.fr\nMessage-ID: <n1@y.fr>\nSubject: hello", "hi")})
        patcher = mock.patch("fr_outreach.agent.random")
        rnd = patcher.start()
        rnd.random.return_value = 0.0
        rnd.uniform.return_value = 1.0
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def agent(self, live: bool = True, sender=None) -> Agent:
        return Agent(
            self.store, self.cfg, live=live, sender_factory=sender or FakeSender(self.sent),
            clock=lambda: self.now, sleep=lambda s: None, imap_connect=self.imap,
        )

    def run_day(self, agent: Agent, day: datetime) -> int:
        total = 0
        for minute in range(6 * 60, 16 * 60):
            self.now = day + timedelta(minutes=minute)
            total += agent.tick()["sent"]
        return total


class AgentSendingTests(AgentTestCase):
    def test_quota_spread_over_the_day_and_warmup(self):
        for i in range(60):
            add_company(self.store, f"{i:09d}", [f"contact@societe{i}.fr"])
        agent = self.agent()
        self.assertEqual(self.run_day(agent, MONDAY), 15)
        times = [datetime.fromisoformat(r[0]) for r in self.store.conn.execute(
            "SELECT created_at FROM sends WHERE status = 'sent' ORDER BY id")]
        self.assertEqual(times[0], at(MONDAY, 7))                         # opens at 09:00 Paris
        self.assertTrue(all(b - a >= timedelta(minutes=28) for a, b in zip(times, times[1:])))
        self.assertLess(times[-1], at(MONDAY, 15))                         # before 17:00 Paris
        self.assertEqual(self.run_day(agent, MONDAY + timedelta(days=1)), 20)   # warm-up +5
        self.assertEqual(self.run_day(agent, MONDAY + timedelta(days=5)), 0)    # Saturday
        self.assertEqual(len({m["To"] for m in self.sent if m["To"] != "report@vendeur.fr"}), 35)

    def test_rehearsal_writes_previews_only(self):
        add_company(self.store, "552100554", ["contact@acme.fr"])
        agent = self.agent(live=False, sender=FakeSender([], RuntimeError("must not send")))
        self.now = at(MONDAY, 7, 5)
        self.assertEqual(agent.tick()["sent"], 1)
        self.assertEqual(len(list(Path(self.tmp.name).rglob("*.eml"))), 2)  # first e-mail + follow-up preview
        self.assertEqual(self.store.stats()["dry_run"], 1)
        self.assertEqual(self.store.stats()["sent"], 0)

    def test_live_requires_imap(self):
        agent = Agent(self.store, self.cfg, live=True, sender_factory=FakeSender([]))
        with self.assertRaisesRegex(Exception, "imap"):
            agent.preflight()

    def test_provider_block_pauses(self):
        add_company(self.store, "552100554", ["contact@acme.fr"])
        refused = smtplib.SMTPSenderRefused(550, b"5.7.1 account suspended for spam", "marie@vendeur.fr")
        agent = self.agent(sender=FakeSender([], refused))
        self.now = at(MONDAY, 7, 5)
        agent.tick()
        self.assertIn("refused the account", self.store.get_state("paused"))
        self.assertEqual(self.store.stats()["sent"] + self.store.stats()["failed"], 0)
        self.now = at(MONDAY, 9)
        self.assertEqual(agent.tick()["sent"], 0)

    def test_repeated_smtp_errors_pause(self):
        add_company(self.store, "552100554", ["contact@acme.fr"])
        agent = self.agent(sender=FakeSender([], OSError("connection refused")))
        for minute in range(0, 60):
            self.now = at(MONDAY, 7, minute)
            agent.tick()
        self.assertIn("5 SMTP errors", self.store.get_state("paused"))

    def test_high_bounce_rate_pauses_and_resume_resets(self):
        self.now = at(MONDAY - timedelta(days=1), 12)  # yesterday: 18 delivered, 2 rejected
        for i in range(20):
            add_company(self.store, f"{i:09d}", [f"contact@societe{i}.fr"])
            self.store.record_send(f"{i:09d}", f"contact@societe{i}.fr", "default", "sent" if i < 18 else "failed")
        add_company(self.store, "999999999", ["contact@next.fr"])
        agent = self.agent()
        self.now = at(MONDAY, 7, 5)
        result = agent.tick()
        self.assertIn("bounce rate 10.0%", result["paused"])
        self.assertEqual(result["sent"], 0)
        self.assertTrue(list(Path(self.tmp.name, "reports").glob("*.txt")))  # alert written
        resume(self.store)
        self.now = at(MONDAY, 8)
        self.assertEqual(agent.tick()["sent"], 1)

    def test_unreadable_mailbox_pauses_live_agent(self):
        add_company(self.store, "552100554", ["contact@acme.fr"])

        def broken(cfg):
            raise OSError("IMAP login failed")

        self.imap = broken
        agent = self.agent()
        self.now = at(MONDAY, 7, 5)
        result = agent.tick()
        self.assertIn("cannot read the reply mailbox", result["paused"] or self.store.get_state("paused"))
        self.assertEqual(result["sent"], 0)

    def test_daily_report_after_window(self):
        self.cfg["agent"]["report_to"] = "report@vendeur.fr"
        agent = self.agent()
        self.now = at(MONDAY, 15, 30)
        self.assertTrue(agent.tick()["report"])
        self.assertFalse(agent.tick()["report"])
        self.assertEqual(self.sent[-1]["To"], "report@vendeur.fr")
        self.assertIn("Daily report 2026-09-28", self.sent[-1]["Subject"])
        self.assertIn("quota 15", self.sent[-1].get_content())


class InboxSyncTests(AgentTestCase):
    def test_replies_optouts_bounces_are_recorded_once(self):
        add_company(self.store, "111111111", ["contact@acme.fr"], "ACME")
        add_company(self.store, "222222222", ["info@beta.fr"], "BETA")
        add_company(self.store, "333333333", ["old@gamma.fr"], "GAMMA")
        add_company(self.store, "444444444", ["hello@delta.fr"], "DELTA")
        self.store.record_send("111111111", "contact@acme.fr", "default", "sent", "<m1@vendeur.fr>")
        self.store.record_send("222222222", "info@beta.fr", "default", "sent", "<m2@vendeur.fr>")
        self.store.record_send("333333333", "old@gamma.fr", "default", "sent", "<m3@vendeur.fr>")
        self.store.record_send("444444444", "hello@delta.fr", "default", "sent", "<m4@vendeur.fr>")
        imap = FakeImap({
            1: raw("From: Jean <jean@acme.fr>\nMessage-ID: <r1@acme.fr>\nIn-Reply-To: <m1@vendeur.fr>\n"
                   "Subject: Re: question", "Oui, appelez-moi.\n\nLe 28/09, Marie a écrit :\n> répondez STOP"),
            2: raw("From: info@beta.fr\nMessage-ID: <r2@beta.fr>\nSubject: STOP", ""),
            3: raw("From: news@shop.com\nMessage-ID: <r3@shop.com>\nSubject: Promo", "Se désabonner"),
            4: raw("From: MAILER-DAEMON@mx.gamma.fr\nMessage-ID: <r4@mx>\nSubject: Undelivered",
                   "Final-Recipient: rfc822; old@gamma.fr\nStatus: 5.1.1"),
            5: raw("From: hello@delta.fr\nMessage-ID: <r5@delta.fr>\nSubject: Réponse automatique", "Absent"),
            7: raw("From: marie@vendeur.fr\nMessage-ID: <r7@v>\nSubject: [fr-outreach] Daily report",
                   "STOP is not an opt-out here"),
        })
        counts = sync_inbox(self.store, {"folder": "INBOX"}, connect=imap, own_addresses=frozenset({"marie@vendeur.fr"}))
        self.assertEqual({k: v for k, v in counts.items() if v},
                         {"reply": 1, "optout": 1, "other": 2, "bounce": 1, "auto": 1})
        self.assertTrue(self.store.is_suppressed("info@beta.fr"))
        self.assertTrue(self.store.is_suppressed("someone-else@beta.fr"))  # whole company opted out
        self.assertTrue(self.store.is_suppressed("old@gamma.fr"))
        self.assertFalse(self.store.is_suppressed("news@shop.com"))
        reply = self.store.inbox_events_since("2000", ("reply",))[0]
        self.assertEqual((reply["siren"], reply["company_name"]), ("111111111", "ACME"))
        # Second sync: only the new message is fetched.
        imap.messages[8] = raw("From: a@b.fr\nMessage-ID: <r8@b.fr>\nSubject: x", "y")
        imap.fetched.clear()
        sync_inbox(self.store, {"folder": "INBOX"}, connect=imap)
        self.assertEqual(imap.fetched, [8])
        # Nobody who replied or opted out is contacted again, even in a new campaign.
        self.cfg["mail"]["recontact_after_days"] = 0
        remaining = {r["siren"] for r in self.store.recipients("next-campaign", recontact_days=0)}
        self.assertEqual(remaining, {"444444444"})


class FakeSource:
    """Two registry queries: A has 3 pages, B has 1."""

    PAGES = {"A": 3, "B": 1}

    @staticmethod
    def build_queries(search):
        return [{"departement": "A"}, {"departement": "B"}]

    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def fetch_page(self, query, page, exclude_individual=True):
        dept = query["departement"]
        self.calls.append((dept, page))
        companies = [Company(siren=f"{ord(dept)}{page:02d}{i:04d}", name=f"Societe {dept}{page}-{i}") for i in range(25)]
        return companies, page >= self.PAGES[dept]


class RefillTests(AgentTestCase):
    def test_collect_resumes_where_it_stopped(self):
        source = FakeSource()
        self.assertEqual(collect_incremental(self.store, self.cfg, 30, source), 50)
        self.assertEqual(source.calls, [("A", 1), ("A", 2)])
        self.assertEqual(collect_incremental(self.store, self.cfg, 30, source), 50)
        self.assertEqual(source.calls[2:], [("A", 3), ("B", 1)])
        self.assertTrue(search_exhausted(self.store, self.cfg, source))
        self.assertEqual(collect_incremental(self.store, self.cfg, 30, source), 0)

    def test_refill_order_and_exhaustion_alert(self):
        self.cfg["agent"]["ready_buffer"] = 10
        agent = self.agent()
        with mock.patch("fr_outreach.agent.enrich_batch", return_value=(3, 2, 5)), \
                mock.patch("fr_outreach.agent.collect_incremental") as collect:
            self.assertEqual(agent.refill(), {"enriched": 5, "websites": 3, "with_email": 2})
            collect.assert_not_called()  # enrich what we have before pulling new companies
        with mock.patch("fr_outreach.agent.enrich_batch", return_value=(0, 0, 0)), \
                mock.patch("fr_outreach.agent.collect_incremental", return_value=0), \
                mock.patch("fr_outreach.agent.search_exhausted", return_value=True):
            self.assertEqual(agent.refill(), {"collected": 0})
        self.assertIsNotNone(self.store.get_state("search_exhausted"))
        self.assertTrue(any("exhausted" in p.read_text() for p in Path(self.tmp.name, "reports").glob("*.txt")))


class RegistryDownTests(AgentTestCase):
    def test_registry_outage_backs_off_quietly(self):
        import requests

        self.cfg["agent"]["ready_buffer"] = 10
        agent = self.agent()
        with mock.patch("fr_outreach.agent.enrich_batch", return_value=(0, 0, 0)), \
                mock.patch("fr_outreach.agent.collect_incremental", side_effect=requests.ConnectionError("down")) as collect:
            self.assertEqual(agent.refill(), {"collected": 0, "error": 1})
            self.assertIsNone(agent.refill())  # backing off: no new attempt right away
            self.assertEqual(collect.call_count, 1)


class LockTests(unittest.TestCase):
    def test_single_agent_per_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "db.sqlite")
            a, b = Store(path), Store(path)
            self.assertTrue(a.try_lock("a"))
            self.assertFalse(b.try_lock("b"))
            self.assertTrue(a.try_lock("a"))  # heartbeat
            a.release_lock("a")
            self.assertTrue(b.try_lock("b"))
            b.set_state("lock", "b|0")  # stale (crashed) holder
            self.assertTrue(a.try_lock("a"))
            a.close()
            b.close()


class DomainCheckTests(unittest.TestCase):
    RECORDS = {
        ("acme.fr", "MX"): ["1 smtp.google.com."],
        ("acme.fr", "TXT"): ["google-site-verification=abc", "v=spf1 include:_spf.google.com ~all"],
        ("google._domainkey.acme.fr", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjANBg"],
        ("_dmarc.acme.fr", "TXT"): ["v=DMARC1; p=none; rua=mailto:dmarc@acme.fr"],
    }

    def resolver(self, records):
        return lambda name, rtype: records.get((name, rtype), [])

    def test_ready_domain(self):
        result = check_domain("marie@acme.fr", self.resolver(self.RECORDS))
        self.assertEqual(result["provider"]["name"], "Google Workspace")
        self.assertEqual([s for s, _, _ in result["checks"]], ["OK", "OK", "OK", "OK"])

    def test_missing_dmarc_and_open_spf(self):
        records = dict(self.RECORDS)
        records[("acme.fr", "TXT")] = ["v=spf1 include:_spf.google.com +all"]
        del records[("_dmarc.acme.fr", "TXT")]
        checks = {what: status for status, what, _ in check_domain("acme.fr", self.resolver(records))["checks"]}
        self.assertEqual(checks, {"MX": "OK", "SPF": "FAIL", "DKIM": "OK", "DMARC": "FAIL"})


if __name__ == "__main__":
    unittest.main()
