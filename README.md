# fr-outreach: automated B2B prospecting for French SMEs/ETIs

fr-outreach finds French companies in the public registries, finds their websites and professional e-mail addresses, and contacts them by e-mail. It can run by itself every day (`fr-outreach agent`) or one step at a time.

```
 collect   ─►  discover   ─►  scrape   ─►  send       ◄─ reads replies, opt-outs, bounces
 French         find & verify   crawl the    daily quota,
 registries     each company's  site for     spread over office hours,
 (API, SIRENE,  website         pro e-mails  warm-up, auto-pause, daily report
 Pappers, CSV)
```

Everything is stored in a local SQLite database (`data/outreach.sqlite`). You can stop and restart at any time. Nothing is fetched twice, and nobody is e-mailed twice.

## Automatic mode: the agent

```bash
fr-outreach agent            # rehearsal: does everything, but writes .eml previews instead of sending
fr-outreach agent --live     # the real thing
fr-outreach status           # quota today, sent today, contacts ready, paused or not
fr-outreach pause / resume   # emergency stop / restart
```

Once started, the agent runs on its own. Every minute it does the following:

1. **Reads your mailbox** (IMAP, every 30 min):
   - "STOP"-type replies go on the do-not-contact list, for the whole company.
   - Bounced addresses are removed.
   - Real replies are recorded so the company is never contacted again automatically. You answer them yourself from your mailbox.
   - Auto-replies and unrelated mail are ignored.
2. **Checks deliverability.** It pauses itself and e-mails you an alert if any of these happens:
   - bounces or rejections go over 5% of recent messages;
   - opt-outs go over 5%;
   - the mail provider refuses the account (quota exceeded, suspended, wrong password);
   - there are 5 SMTP errors in a row.
3. **Sends at most one e-mail**, and only inside the sending window:
   - Monday to Friday, 09:00–17:00 Paris time;
   - no French public holidays;
   - optional blackout periods, such as August.

   Messages are spaced so the day's quota is spread across the day (about one every 30 min at 15/day), never in bursts.
4. **E-mails you a daily report** after 17:00: sent vs. quota, replies (with company names), opt-outs, bounces, 7-day rates and how many contacts are ready.
5. **Keeps contacts ready in advance.** When fewer than 150 contacts are ready, it:
   - crawls websites for e-mails;
   - then finds websites for new companies;
   - then pulls the next page of your registry search.

   It remembers where it stopped, so it works through the whole search over the days. When the search is exhausted it tells you to widen the filters.

Only one agent can run on a database at a time. A crash never produces a duplicate e-mail, because each send is recorded as pending before the message goes out.

### Is one big send possible, or a daily quota?

**Use a daily quota, with a slow ramp-up. Never send the whole list in one go.** A domain's reputation depends on how its sending volume grows. A new or quiet domain that suddenly sends hundreds or thousands of unsolicited e-mails looks exactly like a spammer:

- Gmail, Outlook and **Orange/Wanadoo** (very common among French SMEs) start sending your messages to spam or rejecting them.
- Every e-mail from the domain is affected, including normal messages to your clients, and recovery takes weeks.
- Mailbox providers also have hard caps. **Google Workspace allows 2,000 messages/day per user**, and fewer on new or trial accounts. **Microsoft 365 allows 10,000 recipients/day and 30 messages/minute.** Web-hosting mailboxes are often much lower. Hitting a cap can freeze the account.
- Scraped lists always contain some dead addresses. In one big send those bounces all arrive together, and the bounce rate is what gets a domain blocked.

What the agent does by default, for **one mailbox on one domain**:

| | |
|---|---|
| Day 1 | 15 e-mails, spread from 09:00 to 17:00 |
| Each further sending day | +5 |
| Cruising speed | **100 per day**, reached after about 4 weeks of sending days, then roughly 500 per week |
| Volume after the first 2 months | about 3,600 companies contacted, with the domain still healthy |

You can change the numbers (`mail.warmup`, `mail.max_per_day`). If the domain is brand new (registered less than about a month ago), start lower, for example 10/day. To contact more companies per day later, add **more real mailboxes** (for example colleagues), each with its own quota, rather than raising one mailbox far above 100–150/day.

If this domain is also your main business domain (invoices, clients), consider doing prospecting from a separate domain, so your day-to-day e-mail is not affected if something goes wrong.

### Before going live with your address

1. Run `fr-outreach check-domain vous@votre-domaine.fr`.
   - It checks MX, **SPF**, **DKIM** and **DMARC**. Gmail and Yahoo require all three, and Orange and Outlook filter hard without them.
   - It detects your mail provider (Google Workspace, Microsoft 365, OVHcloud, IONOS, Gandi, Zoho) and prints the SMTP/IMAP settings to use.
   - Fix every `FAIL`.
2. Put the address in `mail.from_address` and the passwords in `.env`, never in `config.yaml` or the repository.
   - Google Workspace needs an app password.
   - Microsoft 365 no longer accepts plain passwords for IMAP, so it needs OAuth support, which is not built yet.
3. Run `fr-outreach agent` (rehearsal) for a day and read the `.eml` files in `outbox/<campaign>/`.
4. Replace every `[PLACEHOLDER]` in the template. Live mode refuses to start while any remain.
5. Start `fr-outreach agent --live`.

### Running it 24/7

The agent must run on a machine that stays on: a small VPS (a few euros a month) or an always-on computer.

**Docker** (recommended):

```bash
cp config.example.yaml config.yaml   # edit it
cp .env.example .env                 # passwords
docker compose up -d --build         # rehearsal; add "--live" in docker-compose.yml when ready
docker compose logs -f
```

**systemd** (on a Linux server, after `pip install .` in `/opt/fr-outreach/.venv`):

```ini
# /etc/systemd/system/fr-outreach.service
[Service]
WorkingDirectory=/opt/fr-outreach
ExecStart=/opt/fr-outreach/.venv/bin/fr-outreach agent --live
Restart=always
[Install]
WantedBy=multi-user.target
```

**cron**, if you prefer: run `fr-outreach agent --live --once` every 5 minutes.

## Which source for which need

| Need | Source | How |
|---|---|---|
| All SMEs/ETIs in a region or department | **Annuaire des Entreprises**, through the **API Recherche d'entreprises** (free, no key) | the agent's source; manually: `collect --source api` |
| Automated large-scale searches | **API Recherche d'entreprises** | searches are split per department × NAF section × category, to get past the 10,000-results-per-query cap |
| Complete raw list for all of France | **SIRENE stock files** from data.gouv.fr | `collect --source sirene --unites-legales StockUniteLegale_utf8.zip --etablissements StockEtablissement_utf8.zip` |
| SMEs with revenue / net income / accounts | **Pappers** API (paid token) | `collect --source pappers --revenue-min 2000000`; the free API also has `--revenue-min/--revenue-max` |
| Start-ups | **Dealroom, Crunchbase, Les Pépites Tech, French Tech** lists | export a CSV, then `collect --source csv --csv export.csv --label dealroom` |
| Diane/Orbis, CAPFI, Altares | paid databases, no open API | CSV export, then `--source csv` |

Companies imported manually (SIRENE, Pappers, CSV) are picked up by the agent like any others.

## How websites and e-mails are found

The French registries publish neither websites nor e-mail addresses.

1. **Website candidates:**
   - a website already provided by the source;
   - optionally, results from the **Brave Search API** (`discovery.search_provider: brave`), with directory sites such as societe.com, Pappers and LinkedIn filtered out;
   - domain guesses (`acmeindustrie.fr`, `acme-industrie.com`…).
2. **Verification:** each candidate is loaded together with its *mentions légales* page. French websites must publish their SIREN there, so finding the company's SIREN is worth 70/100. Name, page title and postal code add more points. Below `min_confidence` (default 50) the candidate is rejected, so a same-name company's site is not mistaken for yours.
3. **E-mail crawl:** home page, contact, *mentions légales* and *à propos* pages, at most 6 per site. The crawler respects robots.txt, waits between requests to the same site and uses an honest User-Agent. It decodes `mailto:` links, Cloudflare-protected addresses and `nom [at] domaine [dot] fr` forms.
4. **Ranking:**
   - Preferred first: role addresses on the company's own domain (`contact@`, `direction@`…), then named people, then webmail addresses such as `@orange.fr` (common for small companies).
   - Always dropped: `noreply`, `rh@`, `recrutement@`, `dpo@`, `compta@`…, and addresses of other companies (the web agency, the host).
   - If an address bounces, the next-best one at the same company is used.

## Manual commands

```bash
fr-outreach collect --departments 69,38 --categories PME,ETI --naf-sections C,J,M --max-results 500
fr-outreach discover
fr-outreach scrape
fr-outreach export -o review.csv      # check companies and addresses in a spreadsheet
fr-outreach send                      # dry run: .eml previews in outbox/<campaign>/
fr-outreach send --send               # real send, limited by today's quota
fr-outreach sync-inbox                # process replies, opt-outs and bounces
fr-outreach suppress contact@x.fr @y.fr
```

Useful filters: `--query "logiciel"`, `--naf 62.01Z,62.02A`, `--regions 84`, `--postal-codes 69003`, `--revenue-min 1000000`. Headcount bands (`search.headcount_bands`) use INSEE codes: `11` = 10–19 employees … `32` = 250–499.

## Compliance (France / EU)

Under CNIL rules, B2B e-mail prospecting in France works on an **opt-out** basis. It is lawful when:

- the message is **related to the recipient's job**;
- the recipient is **told where their address came from** and **can object easily, at any time**;
- the **sender is clearly identified**.

The tool enforces this:

- Every message carries your legal name and address, where the address came from, an opt-out (reply "STOP", or your unsubscribe link) and `List-Unsubscribe` headers.
- Opt-outs are applied automatically. Live mode refuses to start without IMAP access to the mailbox, so opt-outs are never missed.
- One address per company, no re-contact for 6 months, sole traders excluded, companies marked non-public in SIRENE skipped.

Still your responsibility:

- keeping this processing in your **registre des traitements** (GDPR record of processing activities), with legitimate interest as the legal basis;
- deleting prospects who have not engaged within **3 years**;
- targeting companies for whom your offer is relevant;
- checking the terms of use of paid databases before re-using their data.

## Development

```bash
python -m unittest discover -s tests -v   # offline tests, no network needed
```

Code layout:

| Path | Role |
|---|---|
| `fr_outreach/sources/` | one module per data source |
| `pipeline.py` | collect / discover / scrape batches |
| `discovery.py` | website finding and verification |
| `scraper.py`, `emails.py` | crawling, extraction and ranking |
| `mailer.py` | templating, safeguards, SMTP, quota |
| `schedule.py` | sending window, holidays, warm-up |
| `agent.py` | autonomous loop, health checks, reports |
| `inbox.py` | replies, opt-outs, bounces |
| `domain_check.py` | SPF / DKIM / DMARC checks |
| `db.py` | SQLite |
| `cli.py` | command line |
