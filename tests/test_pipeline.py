"""Offline tests: no network access needed (HTTP is faked)."""
from __future__ import annotations

import csv
import email
import os
import smtplib
import tempfile
import unittest
from email import policy
from pathlib import Path
from unittest import mock

from fr_outreach.config import load_config
from fr_outreach.db import Store
from fr_outreach.discovery import WebsiteFinder, domain_guesses
from fr_outreach.emails import clean, extract_emails, rank, registrable_domain
from fr_outreach.http import Page
from fr_outreach.inbox import classify
from fr_outreach.mailer import ComplianceError, run_campaign
from fr_outreach.models import Company
from fr_outreach.scraper import crawl_emails
from fr_outreach.sources.csv_import import CsvSource
from fr_outreach.sources.recherche_entreprises import RechercheEntreprisesSource, parse_result
from fr_outreach.sources.sirene import SireneStockSource

ROOT = Path(__file__).resolve().parents[1]

API_ITEM = {
    "siren": "552100554",
    "nom_complet": "ACME INDUSTRIE",
    "nom_raison_sociale": "ACME INDUSTRIE",
    "sigle": None,
    "activite_principale": "25.62B",
    "categorie_entreprise": "PME",
    "tranche_effectif_salarie": "12",
    "date_creation": "2004-05-01",
    "siege": {
        "siret": "55210055400013", "adresse": "12 RUE DES LILAS 69003 LYON", "code_postal": "69003",
        "libelle_commune": "LYON", "departement": "69", "region": "84",
    },
    "dirigeants": [
        {"type_dirigeant": "personne morale", "denomination": "HOLDING X"},
        {"type_dirigeant": "personne physique", "nom": "DUPONT", "prenoms": "JEAN PIERRE", "qualite": "Président"},
    ],
    "finances": {"2022": {"ca": 1000, "resultat_net": 10}, "2023": {"ca": 2500000, "resultat_net": 120000}},
    "complements": {"est_entrepreneur_individuel": False},
}


class FakeFetcher:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str):
        self.requested.append(url)
        text = self.pages.get(url) or self.pages.get(url.rstrip("/"))
        return Page(url=url, status=200, text=text) if text is not None else None


class EmailExtractionTests(unittest.TestCase):
    def test_plain_mailto_obfuscated_and_cloudflare(self):
        cf = "".join(f"{ord(c) ^ 0x42:02x}" for c in "ventes@acme.fr")
        page = f"""
            <a href="mailto:Contact@Acme.fr?subject=Hi">écrire</a>
            <p>direction [at] acme [dot] fr</p>
            <span class="__cf_email__" data-cfemail="42{cf}">[email protected]</span>
            <img src="logo@2x.png"> <script>var x="a1b2c3d4e5f6a7b8c9d0@sentry.io";</script>
            <p>jean.dupont&#64;acme.fr</p>
        """
        self.assertEqual(
            extract_emails(page),
            {"contact@acme.fr", "direction@acme.fr", "ventes@acme.fr", "jean.dupont@acme.fr"},
        )

    def test_clean_rejects_junk(self):
        self.assertIsNone(clean("image@2x.png"))
        self.assertIsNone(clean("test@example.com"))
        self.assertEqual(clean("mailto:Info@Societe.FR."), "info@societe.fr")

    def test_rank_prefers_company_role_addresses_and_drops_bad_ones(self):
        ranked = rank(
            [
                ("jean.dupont@acme.fr", "https://acme.fr/equipe"),
                ("contact@acme.fr", "https://acme.fr/contact"),
                ("recrutement@acme.fr", "https://acme.fr/jobs"),
                ("hello@agence-web.fr", "https://acme.fr/"),
                ("acme.lyon@orange.fr", "https://acme.fr/"),
                ("noreply@acme.fr", "https://acme.fr/"),
            ],
            "https://www.acme.fr/",
        )
        self.assertEqual([c.email for c in ranked], ["contact@acme.fr", "jean.dupont@acme.fr", "acme.lyon@orange.fr"])

    def test_registrable_domain(self):
        self.assertEqual(registrable_domain("https://www.shop.acme.fr/x"), "acme.fr")
        self.assertEqual(registrable_domain("acme.co.uk"), "acme.co.uk")


class SourceTests(unittest.TestCase):
    def test_parse_api_result(self):
        c = parse_result(API_ITEM)
        self.assertEqual((c.siren, c.name, c.city, c.department), ("552100554", "ACME INDUSTRIE", "LYON", "69"))
        self.assertEqual((c.director_first_name, c.director_last_name), ("Jean", "Dupont"))
        self.assertEqual((c.finances_year, c.revenue), ("2023", 2500000))

    def test_query_expansion_splits_by_department_and_category(self):
        queries = RechercheEntreprisesSource.build_queries(
            {"departments": ["69", "38"], "categories": ["PME", "ETI"], "naf_codes": ["62.01Z"], "exclude_individual": True}
        )
        self.assertEqual(len(queries), 4)
        combos = {(q["departement"], q["categorie_entreprise"]) for q in queries}
        self.assertEqual(combos, {("69", "PME"), ("69", "ETI"), ("38", "PME"), ("38", "ETI")})
        for q in queries:
            self.assertEqual(q["activite_principale"], "62.01Z")
            self.assertEqual(q["etat_administratif"], "A")

    def test_api_pagination_and_max_results(self):
        pages = [
            {"results": [dict(API_ITEM, siren=f"{i:09d}") for i in range(25)], "total_results": 30, "total_pages": 2},
            {"results": [dict(API_ITEM, siren=f"{i:09d}") for i in range(25, 30)], "total_results": 30, "total_pages": 2},
        ]
        session = mock.Mock()
        responses = []
        for p in pages:
            r = mock.Mock(status_code=200, headers={})
            r.json.return_value = p
            responses.append(r)
        session.get.side_effect = responses
        src = RechercheEntreprisesSource(session=session)
        with mock.patch("fr_outreach.sources.recherche_entreprises.time.sleep"):
            got = list(src.search({"departments": ["69"], "max_results": 28}))
        self.assertEqual(len(got), 28)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["page"], 2)

    def test_sirene_stock_files(self):
        with tempfile.TemporaryDirectory() as d:
            ul, et = os.path.join(d, "ul.csv"), os.path.join(d, "et.csv")
            with open(ul, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["siren", "statutDiffusionUniteLegale", "etatAdministratifUniteLegale", "categorieEntreprise",
                            "denominationUniteLegale", "activitePrincipaleUniteLegale", "trancheEffectifsUniteLegale",
                            "categorieJuridiqueUniteLegale"])
                w.writerow(["111111111", "O", "A", "PME", "ALPHA", "62.01Z", "12", "5710"])
                w.writerow(["222222222", "O", "C", "PME", "CLOSED", "62.01Z", "12", "5710"])
                w.writerow(["333333333", "P", "A", "PME", "PRIVATE", "62.01Z", "12", "5710"])
                w.writerow(["444444444", "O", "A", "GE", "BIG", "62.01Z", "53", "5599"])
                w.writerow(["555555555", "O", "A", "PME", "BETA", "62.01Z", "11", "5710"])
            with open(et, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["siren", "siret", "etablissementSiege", "codeCommuneEtablissement", "codePostalEtablissement",
                            "libelleCommuneEtablissement", "libelleVoieEtablissement"])
                w.writerow(["111111111", "11111111100011", "true", "69383", "69003", "LYON", "RUE X"])
                w.writerow(["555555555", "55555555500011", "true", "75056", "75001", "PARIS", "RUE Y"])
            got = list(SireneStockSource(ul, et).search({"categories": ["PME"], "departments": ["69"]}))
        self.assertEqual([c.siren for c in got], ["111111111"])
        self.assertEqual((got[0].city, got[0].department), ("LYON", "69"))

    def test_csv_import(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dealroom.csv")
            Path(path).write_text("NAME;SIREN;WEBSITE;HQ City\nStartUp;123 456 789;startup.io;Paris\nBad;12;x.fr;Lyon\n", encoding="utf-8")
            got = list(CsvSource(path, label="dealroom").search({}))
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0].siren, got[0].website, got[0].website_source), ("123456789", "startup.io", "dealroom"))


class DiscoveryAndScrapeTests(unittest.TestCase):
    def test_domain_guesses(self):
        self.assertIn("https://www.acmeindustrie.fr", domain_guesses("ACME INDUSTRIE SAS"))
        self.assertIn("https://www.acme-industrie.com", domain_guesses("ACME INDUSTRIE SAS"))

    def test_verified_by_siren_on_legal_page(self):
        fetcher = FakeFetcher({
            "https://www.acmeindustrie.fr": '<title>Acme Industrie</title><a href="/mentions-legales">Mentions légales</a>',
            "https://www.acmeindustrie.fr/mentions-legales": "ACME INDUSTRIE SAS - RCS Lyon 552 100 554",
            "https://www.acme-industrie.fr": "<title>Autre</title> homonyme sans rapport",
        })
        finder = WebsiteFinder(fetcher, {"guess_domains": True, "min_confidence": 50})
        url, source, conf = finder.find({"siren": "552100554", "name": "ACME INDUSTRIE", "postal_code": "69003"})
        self.assertEqual((url, source), ("https://www.acmeindustrie.fr", "guess"))
        self.assertGreaterEqual(conf, 80)

    def test_unverified_guess_is_rejected(self):
        fetcher = FakeFetcher({"https://www.acmeindustrie.fr": "<title>Blog</title> rien à voir"})
        finder = WebsiteFinder(fetcher, {"guess_domains": True, "min_confidence": 50})
        self.assertEqual(finder.find({"siren": "552100554", "name": "ACME INDUSTRIE"})[0], "")

    def test_crawl_follows_contact_pages(self):
        fetcher = FakeFetcher({
            "https://acme.fr/": '<a href="/nous-contacter">Contact</a><a href="/blog">Blog</a>',
            "https://acme.fr/nous-contacter": '<a href="mailto:contact@acme.fr">mail</a> rh@acme.fr',
        })
        got = crawl_emails(fetcher, "https://acme.fr/", max_pages=4)
        self.assertEqual([c.email for c in got], ["contact@acme.fr"])
        self.assertNotIn("https://acme.fr/blog", fetcher.requested)


class MailerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(":memory:")
        cfg = load_config(None)
        self.mail = dict(cfg["mail"])
        self.mail.update(
            template=str(ROOT / "templates" / "prospection_fr.txt"),
            from_name="Marie Martin", from_address="marie@vendeur.fr", company_legal_name="Vendeur SAS",
            company_address="1 rue X, 75001 Paris", unsubscribe_mailto="stop@vendeur.fr",
            outbox_dir=self.tmp.name, delay_seconds=0, jitter_seconds=0,
        )
        self.scraping = cfg["scraping"]
        c = parse_result(API_ITEM)
        c.website = "https://acme.fr"
        self.store.upsert_company(c)
        from fr_outreach.models import EmailCandidate

        self.store.add_emails(c.siren, [EmailCandidate("contact@acme.fr", "https://acme.fr/contact", 90)], {"acme.fr": True})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_refuses_without_sender_identity_or_opt_out(self):
        self.mail["unsubscribe_mailto"] = ""
        with self.assertRaises(ComplianceError):
            run_campaign(self.store, self.mail, self.scraping)

    def test_dry_run_writes_compliant_eml(self):
        counts = run_campaign(self.store, self.mail, self.scraping)
        self.assertEqual(counts["dry_run"], 1)
        files = list(Path(self.tmp.name).rglob("*.eml"))
        self.assertEqual(len(files), 1)
        msg = email.message_from_bytes(files[0].read_bytes(), policy=policy.default)
        body = msg.get_content()
        self.assertEqual(msg["To"], "contact@acme.fr")
        self.assertIn("ACME INDUSTRIE", msg["Subject"])
        self.assertIn("Bonjour Jean Dupont", body)
        self.assertIn("Vendeur SAS", body)
        self.assertIn("STOP", body)
        self.assertIn("mailto:stop@vendeur.fr", msg["List-Unsubscribe"])
        # dry runs can be regenerated
        self.assertEqual(run_campaign(self.store, self.mail, self.scraping)["dry_run"], 1)

    def test_real_send_refused_while_template_has_placeholders(self):
        with self.assertRaises(ComplianceError):
            run_campaign(self.store, self.mail, self.scraping, really_send=True)

    def test_real_send_once_and_suppression(self):
        tpl = Path(self.tmp.name) / "tpl.txt"
        tpl.write_text("Subject: Bonjour $company_name\n\n$greeting,\nMessage.\n", encoding="utf-8")
        self.mail["template"] = str(tpl)
        sent = []

        class FakeSender:
            def __init__(self, cfg): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def send(self, msg): sent.append(msg["To"])

        run = lambda: run_campaign(self.store, self.mail, self.scraping, really_send=True,  # noqa: E731
                                   sender_factory=FakeSender, sleep=lambda s: None)
        self.assertEqual(run()["sent"], 1)
        self.assertEqual(run()["sent"], 0)  # never twice in the same campaign
        self.mail["campaign"] = "relance"
        self.store.suppress("@acme.fr", "optout")
        self.assertEqual(run()["sent"], 0)  # suppressed domain
        self.assertEqual(sent, ["contact@acme.fr"])

    def test_bounce_is_suppressed(self):
        tpl = Path(self.tmp.name) / "tpl.txt"
        tpl.write_text("Subject: Bonjour\n\nMessage.\n", encoding="utf-8")
        self.mail["template"] = str(tpl)
        class RefusingSender:
            def __init__(self, cfg): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def send(self, msg): raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no such user")})

        counts = run_campaign(self.store, self.mail, self.scraping, really_send=True,
                              sender_factory=RefusingSender, sleep=lambda s: None)
        self.assertEqual(counts["failed"], 1)
        self.assertTrue(self.store.is_suppressed("contact@acme.fr"))


class InboxTests(unittest.TestCase):
    def test_optout_reply(self):
        msg = email.message_from_string(
            "From: Jean <contact@acme.fr>\nSubject: Re: question\n\nMerci de ne plus me contacter.\n\n"
            "Le 1 oct. 2026, Marie a écrit :\n> répondez STOP"
        )
        self.assertEqual(classify(msg), ("optout", ["contact@acme.fr"]))

    def test_quoted_footer_is_not_an_optout(self):
        msg = email.message_from_string(
            "From: Jean <contact@acme.fr>\nSubject: Re: question\n\nOui, appelez-moi mardi.\n\n"
            "Le 1 oct. 2026, Marie a écrit :\n> répondez STOP à cet e-mail"
        )
        self.assertEqual(classify(msg)[0], "other")

    def test_bounce(self):
        msg = email.message_from_string(
            "From: MAILER-DAEMON@mx.acme.fr\nSubject: Undelivered\n\nFinal-Recipient: rfc822; old@acme.fr\nStatus: 5.1.1"
        )
        self.assertEqual(classify(msg), ("bounce", ["old@acme.fr"]))


if __name__ == "__main__":
    unittest.main()
