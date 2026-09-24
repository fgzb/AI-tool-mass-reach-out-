# fr-outreach: automated B2B prospecting for French SMEs/ETIs

This tool runs four steps, and you can run each one on its own:

```
 1. collect   ─►  2. discover   ─►  3. scrape   ─►  4. send
 French          find & verify     crawl the       templated e-mail,
 registries      each company's    site for pro    dry run by default,
 (SIRENE, API,   website           e-mails         opt-out handling
 Pappers, CSV)
```

Everything is stored in a local SQLite database (`data/outreach.sqlite`). You can stop and restart any step. Nothing is fetched twice and nobody is e-mailed twice in the same campaign.

## Which source for which need

| Need | Source | How to use it here |
|---|---|---|
| All SMEs/ETIs in a region or department | **Annuaire des Entreprises**, through the **API Recherche d'entreprises** (free, no key) | `collect --source api` (default) |
| Automated large-scale searches | **API Recherche d'entreprises** | same; the search is split per department × NAF section × category to get past the API's 10,000-results-per-query cap |
| Complete raw list for all of France | **SIRENE stock files** from data.gouv.fr | `collect --source sirene --unites-legales StockUniteLegale_utf8.zip --etablissements StockEtablissement_utf8.zip` |
| SMEs with revenue / net income / accounts | **Pappers** API (paid token) | `collect --source pappers --revenue-min 2000000`; the free API also has `--revenue-min/--revenue-max` from the published accounts |
| Start-ups | **Dealroom, Crunchbase, Les Pépites Tech, French Tech** lists | export a CSV from their UI and run `collect --source csv --csv export.csv --label dealroom` |
| Diane/Orbis, CAPFI, Altares | paid databases, no open API | CSV export, then `--source csv` |

The CSV importer recognises common column names (`SIREN`, `Name`/`Dénomination`, `Website`/`Site web`, `City`/`Ville`, `Revenue`/`Chiffre d'affaires`…). A SIREN column is required so records can be de-duplicated and matched against SIRENE.

## How websites and e-mails are found

The French registries do not publish websites or e-mail addresses, so the tool finds them this way:

1. **Website candidates:**
   - a website already provided by the source (Pappers, CSV);
   - optionally, results from the **Brave Search API** (`discovery.search_provider: brave`, key in `BRAVE_API_KEY`), with directory sites such as societe.com, Pappers and LinkedIn filtered out;
   - optionally, **domain guesses** (`acmeindustrie.fr`, `acme-industrie.com`…).
2. **Verification:** each candidate site is loaded together with its *mentions légales* page. French websites must publish their SIREN there, so finding the company's SIREN is worth 70/100. Matching the name, the page title and the postal code add more points. Candidates below `min_confidence` (default 50) are rejected, so a same-name company's site is not mistaken for yours.
3. **E-mail crawl:** the home page, the contact, *mentions légales* and *à propos* pages, plus fallback paths like `/contact` (at most 6 pages per site). The crawler respects robots.txt, waits between requests to the same site and sends an honest User-Agent. It decodes `mailto:` links, Cloudflare-protected addresses and `nom [at] domaine [dot] fr` forms.
4. **Ranking:**
   - Preferred first: role addresses on the company's own domain (`contact@`, `direction@`, `info@`…), then named people (`prenom.nom@`), then free webmail addresses (`@orange.fr`, `@gmail.com`, which are common for small French businesses).
   - Always dropped: `noreply`, `rh@`, `recrutement@`, `dpo@`, `compta@`…, and addresses on other domains (the web agency, the host…).
   - Addresses whose domain cannot receive mail are skipped (install `dnspython` for real MX lookups; otherwise a DNS A-record check is used).

## Quick start

```bash
pip install -e .                      # or: pip install -r requirements.txt
cp config.example.yaml config.yaml    # then edit: filters, sender identity, SMTP
# edit templates/prospection_fr.txt (the tool refuses to send while [PLACEHOLDERS] remain)

export SMTP_USERNAME=... SMTP_PASSWORD=...        # secrets only through env vars
export BRAVE_API_KEY=...        # optional
export PAPPERS_API_TOKEN=...    # optional

fr-outreach collect --departments 69,38 --categories PME,ETI --naf-sections C,J,M --max-results 500
fr-outreach discover
fr-outreach scrape
fr-outreach export -o review.csv      # check the companies and addresses
fr-outreach send                      # DRY RUN: .eml previews in outbox/<campaign>/
fr-outreach send --send               # really send (asks for confirmation)
fr-outreach sync-inbox                # record "STOP" replies and bounces from IMAP
fr-outreach stats
```

Or run all four steps in one go: `fr-outreach run --departments 69 --max-results 200` (dry run unless you add `--send`).

Useful filters: `--query "logiciel"`, `--naf 62.01Z,62.02A`, `--regions 84`, `--postal-codes 69003`, `--revenue-min 1000000`. Headcount bands (`search.headcount_bands` in the config) use INSEE codes: `11` = 10–19 employees … `32` = 250–499.

To run the pipeline on a schedule (for example every weekday morning), put `fr-outreach run --send --yes` in cron, or in a scheduled job with the same config file.

## Compliance (France / EU): read before sending

Under CNIL rules, B2B e-mail prospecting in France works on an **opt-out** basis (prior consent is not required). It is lawful only if:

- the message is **related to the recipient's job** (target by NAF code / activity);
- the recipient is **told where their address came from** and can **object easily, free of charge, at any time**;
- the **sender is clearly identified**.

The tool enforces this. It **refuses to send** unless your legal name, postal address and an unsubscribe address or URL are configured. Every message gets:

- an identification and opt-out footer;
- a data-source notice;
- `List-Unsubscribe` headers.

A suppression list (manual entries, `STOP` replies, bounces) is checked before every send.

Default behaviour:

- **dry run** (nothing is sent without `--send`);
- one address per company;
- sole traders (*entrepreneurs individuels*) excluded, because their address is personal data;
- companies marked non-public ("non diffusible") in SIRENE skipped;
- 20 s ± 10 s between messages;
- at most 50 messages per run and 200 per day.

What stays your responsibility:

- keeping a record of this processing in your **registre des traitements** (GDPR record of processing activities), with legitimate interest as the legal basis;
- deleting prospects who have not engaged within **3 years**;
- honouring opt-outs immediately: run `fr-outreach sync-inbox` regularly or use `fr-outreach suppress`;
- sending from a domain with SPF, DKIM and DMARC set up, and ramping volume up slowly;
- checking the terms of use of paid databases (Pappers, Dealroom, Crunchbase…) before re-using their data.

Real sending is not a mass-mailing platform. Keep volumes reasonable and messages relevant.

## Development

```bash
python -m unittest discover -s tests -v   # offline tests, no network needed
```

Code layout: `fr_outreach/sources/` (one module per data source), `discovery.py` (website finding and verification), `http.py` (polite fetcher), `scraper.py` + `emails.py` (crawling, extraction, ranking), `mailer.py` (templating, safeguards, SMTP), `inbox.py` (opt-outs and bounces), `db.py` (SQLite), `cli.py`.
