"""Find the official website of a company.

SIRENE and the API Recherche d'entreprises do not publish websites, so we:
  1. keep a website already supplied by the source (Pappers, CSV export...);
  2. optionally ask a web search API (Brave Search) for "<name> <city>";
  3. optionally guess domains from the name (societe.fr, societe.com, ...).
Every candidate is then *verified* by loading it and looking for the SIREN, the
company name and the postal code (French sites must publish their SIREN in the
"mentions légales", which makes the SIREN a very strong signal).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import requests

from .emails import registrable_domain
from .http import Fetcher

log = logging.getLogger(__name__)

# Directories / social networks: never the company's own site.
DIRECTORY_DOMAINS = {
    "pappers.fr", "societe.com", "verif.com", "infogreffe.fr", "manageo.fr", "annuaire-entreprises.data.gouv.fr",
    "data.gouv.fr", "gouv.fr", "pagesjaunes.fr", "kompass.com", "linkedin.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "wikipedia.org", "societeinfo.com", "corporama.com", "lefigaro.fr",
    "entreprises.lefigaro.fr", "bodacc.fr", "score3.fr", "infonet.fr", "annuaire.118712.fr", "118712.fr",
    "mappy.com", "google.com", "tripadvisor.fr", "yelp.fr", "indeed.com", "welcometothejungle.com", "doctrine.fr",
    "dnb.com", "europages.fr", "hellowork.com", "leboncoin.fr", "b-reputation.com", "firmania.fr", "cylex.fr",
}
LEGAL_FORMS = r"\b(sas|sasu|sarl|eurl|sa|sci|snc|scop|selarl|selas|eirl|ei|groupe|group|france|societe|ste|ets|etablissements)\b"
LEGAL_LINK_RE = re.compile(r"mentions?[-_ ]?l[eé]gales?|legal|cgv|cgu|a-propos|about|qui-sommes|contact", re.I)
HREF_RE = re.compile(r"<a\s[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# Parking / for-sale pages often repeat the company name from the domain: never accept them.
PARKED_RE = re.compile(
    r"domain (name )?(is |may be )?for sale|ce (nom de )?domaine est (à|a) vendre|domaine (à|a) vendre|"
    r"buy this domain|acheter ce (nom de )?domaine|sedoparking|parkingcrew|bodis\.com|afternic|dan\.com/buy|"
    r"this domain (name )?(has been|is) (registered|parked)|site en construction chez|"
    r"domaine (réservé|enregistré) (par|chez)|hébergé par (ovh|ionos|gandi).{0,40}(bientôt|prochainement)",
    re.I,
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def name_tokens(name: str) -> list[str]:
    return [t for t in re.sub(LEGAL_FORMS, " ", normalize(name)).split() if len(t) > 2]


def domain_guesses(name: str, sigle: str = "", city: str = "") -> list[str]:
    base = re.sub(LEGAL_FORMS, " ", normalize(name)).split()
    if not base:
        return []
    stems = {"".join(base), "-".join(base)}
    if len(base) > 1:
        stems.add(base[0] if len(base[0]) > 3 else "".join(base[:2]))
    if sigle:
        stems.add(normalize(sigle).replace(" ", ""))
    out = []
    for stem in sorted(s for s in stems if 3 <= len(s) <= 40):
        out += [f"https://www.{stem}.fr", f"https://www.{stem}.com"]
    city_slug = normalize(city).replace(" ", "-")
    if city_slug and len(base) <= 3:  # common for local businesses: boulangerie-martin-lyon.fr
        out.append(f"https://www.{'-'.join(base)}-{city_slug}.fr")
    return out


def links(page_html: str, base_url: str) -> list[tuple[str, str]]:
    out = []
    for href, label in HREF_RE.findall(page_html):
        url = urljoin(base_url, href.strip())
        if url.startswith(("http://", "https://")):
            out.append((url, re.sub(r"<[^>]+>", " ", label)))
    return out


def legal_links(page_html: str, base_url: str) -> list[str]:
    """Same-site links to legal / contact / about pages, legal notice first, without duplicates."""
    ranked: dict[str, int] = {}
    for url, label in links(page_html, base_url):
        url = url.split("#")[0]
        text = f"{url} {label}"
        if not same_site(url, base_url) or not LEGAL_LINK_RE.search(text):
            continue
        rank = 0 if re.search(r"mention|l[ée]gal", text, re.I) else 1 if re.search(r"contact", text, re.I) else 2
        ranked[url] = min(rank, ranked.get(url, rank))
    return sorted(ranked, key=ranked.__getitem__)


def same_site(url_a: str, url_b: str) -> bool:
    return registrable_domain(url_a) == registrable_domain(url_b)


class WebsiteFinder:
    def __init__(self, fetcher: Fetcher, cfg: dict[str, Any], brave_api_key: str = "", session: Optional[requests.Session] = None):
        self.fetcher = fetcher
        self.provider = cfg.get("search_provider")
        self.guess = cfg.get("guess_domains", True)
        self.min_confidence = cfg.get("min_confidence", 50)
        self.brave_key = brave_api_key
        self.session = session or requests.Session()

    # -- candidate generation ---------------------------------------------
    def search_candidates(self, company: dict[str, Any]) -> list[str]:
        if self.provider != "brave" or not self.brave_key:
            return []
        query = f"\"{company['name']}\" {company.get('city') or ''}".strip()
        try:
            resp = self.session.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "country": "fr", "search_lang": "fr", "count": 10},
                headers={"X-Subscription-Token": self.brave_key, "Accept": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            results = resp.json().get("web", {}).get("results", [])
        except (requests.RequestException, ValueError) as exc:
            log.warning("Brave search failed for %s: %s", company["siren"], exc)
            return []
        out = []
        for r in results:
            url = r.get("url", "")
            dom = registrable_domain(url)
            if not dom or dom in DIRECTORY_DOMAINS or any(dom.endswith("." + d) for d in DIRECTORY_DOMAINS):
                continue
            parts = urlsplit(url)
            root = f"{parts.scheme}://{parts.netloc}/"
            if root not in out:
                out.append(root)
        return out[:5]

    # -- verification -------------------------------------------------------
    def verify(self, url: str, company: dict[str, Any]) -> tuple[int, str]:
        """Return (confidence 0-100, final URL)."""
        home = self.fetcher.get(url)
        if home is None:
            return 0, url
        pages = [home]
        siren = company["siren"]
        # Legal notice first (French sites must show their SIREN there), then contact / about pages.
        # Those pages stay in the fetcher's cache, so the e-mail crawl gets them for free.
        for link in legal_links(home.text, home.url)[:2]:
            if siren in re.sub(r"[\s.\u00a0]", "", " ".join(p.text for p in pages)):
                break
            page = self.fetcher.get(link)
            if page:
                pages.append(page)
        return self.confidence(pages, company), home.url

    @staticmethod
    def confidence(pages: list[Any], company: dict[str, Any]) -> int:
        blob = " ".join(p.text for p in pages)
        if PARKED_RE.search(blob):
            return 0
        flat = re.sub(r"[\s. ]", "", blob)
        norm = normalize(blob)
        siren = company["siren"]
        score = 0
        if siren in flat:
            score += 70
        tokens = name_tokens(company["name"])
        if tokens:
            hits = sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}\b", norm))
            score += int(25 * hits / len(tokens))
        title = TITLE_RE.search(pages[0].text)
        if title and tokens and any(t in normalize(title.group(1)) for t in tokens):
            score += 10
        if company.get("postal_code") and company["postal_code"] in blob:
            score += 10
        return min(score, 100)

    def find(self, company: dict[str, Any]) -> tuple[str, str, int]:
        """Return (url, source, confidence); url is "" when nothing convincing was found."""
        candidates: list[tuple[str, str]] = []
        if company.get("website"):
            url = company["website"]
            if not url.startswith("http"):
                url = "https://" + url.lstrip("/")
            candidates.append((url, company.get("website_source") or "source"))
        candidates += [(u, "search") for u in self.search_candidates(company)]
        if self.guess:
            resolves = getattr(self.fetcher, "resolves", lambda url: True)
            guesses = domain_guesses(company["name"], company.get("sigle") or "", company.get("city") or "")
            # A DNS lookup costs a few ms; an HTTP attempt on a dead domain costs seconds.
            candidates += [(u, "guess") for u in guesses if resolves(u)]

        best = ("", "", 0)
        seen: set[str] = set()
        for url, source in candidates:
            dom = registrable_domain(url)
            if dom in seen:
                continue
            seen.add(dom)
            conf, final_url = self.verify(url, company)
            if source not in ("search", "guess"):
                conf = max(conf, 60)  # trust the provider's website a bit
            if conf > best[2]:
                best = (final_url, source, conf)
            if conf >= 80:
                break
        if best[2] < self.min_confidence:
            return "", "", best[2]
        return best
