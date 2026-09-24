"""API Recherche d'entreprises (the API behind annuaire-entreprises.data.gouv.fr).

Free, no API key. Docs: https://recherche-entreprises.api.gouv.fr/docs/
Limits worth knowing:
  * ~7 requests / second per IP  -> we throttle and honour 429 Retry-After;
  * max 25 results per page;
  * a single query can only be paged up to 10 000 results. To cover a large
    area exhaustively we split the search into one query per
    (department x NAF section x category) combination.
"""
from __future__ import annotations

import itertools
import logging
import time
from typing import Any, Iterator, Optional

import requests

from ..models import Company

log = logging.getLogger(__name__)

API_URL = "https://recherche-entreprises.api.gouv.fr/search"
PER_PAGE = 25
MAX_WINDOW = 10_000
MIN_INTERVAL = 1 / 6  # stay under the 7 req/s limit


class RechercheEntreprisesSource:
    name = "recherche-entreprises"

    def __init__(self, session: Optional[requests.Session] = None, user_agent: str = "", timeout: int = 20):
        self.session = session or requests.Session()
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self._last_call = 0.0

    # ------------------------------------------------------------------
    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(6):
            wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            resp = self.session.get(API_URL, params=params, timeout=self.timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                delay = float(resp.headers.get("Retry-After") or 2 ** attempt)
                log.warning("API returned %s, retrying in %.0fs", resp.status_code, delay)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("API Recherche d'entreprises: too many retries")

    @staticmethod
    def build_queries(search: dict[str, Any]) -> list[dict[str, Any]]:
        """Expand list filters into the cartesian product of single-value queries."""
        base: dict[str, Any] = {"etat_administratif": "A"}  # active companies only
        if search.get("query"):
            base["q"] = search["query"]
        if search.get("postal_codes"):
            base["code_postal"] = ",".join(search["postal_codes"])
        if search.get("naf_codes"):
            base["activite_principale"] = ",".join(search["naf_codes"])
        if search.get("headcount_bands"):
            base["tranche_effectif_salarie"] = ",".join(search["headcount_bands"])
        if search.get("revenue_min") is not None:
            base["ca_min"] = search["revenue_min"]
        if search.get("revenue_max") is not None:
            base["ca_max"] = search["revenue_max"]
        if search.get("exclude_individual", True):
            base["est_entrepreneur_individuel"] = "false"

        axes: list[list[tuple[str, str]]] = []
        if search.get("departments"):
            axes.append([("departement", d) for d in search["departments"]])
        elif search.get("regions"):
            axes.append([("region", r) for r in search["regions"]])
        if search.get("naf_sections"):
            axes.append([("section_activite_principale", s) for s in search["naf_sections"]])
        if search.get("categories"):
            axes.append([("categorie_entreprise", c) for c in search["categories"]])

        queries = []
        for combo in itertools.product(*axes) if axes else [()]:
            q = dict(base)
            q.update(dict(combo))
            queries.append(q)
        return queries

    def fetch_page(self, query: dict[str, Any], page: int, exclude_individual: bool = True) -> tuple[list[Company], bool]:
        """One page of results. Returns (companies, is_last_page)."""
        data = self._get({**query, "page": page, "per_page": PER_PAGE})
        if page == 1:
            total = data.get("total_results", 0)
            log.info("Query %s -> %s results", query, total)
            if total > MAX_WINDOW:
                log.warning(
                    "Query %s has %s results but the API only pages through %s. "
                    "Add departments / naf_sections to split it.", query, total, MAX_WINDOW,
                )
        companies = [parse_result(item) for item in data.get("results", [])]
        if exclude_individual:
            companies = [c for c in companies if not c.is_individual]
        last = page >= (data.get("total_pages") or 0) or page * PER_PAGE >= MAX_WINDOW or not data.get("results")
        return companies, last

    def search(self, search: dict[str, Any]) -> Iterator[Company]:
        max_results = search.get("max_results") or None
        exclude_individual = search.get("exclude_individual", True)
        yielded = 0
        seen: set[str] = set()
        for query in self.build_queries(search):
            page, last = 1, False
            while not last:
                companies, last = self.fetch_page(query, page, exclude_individual)
                for company in companies:
                    if company.siren in seen:
                        continue
                    seen.add(company.siren)
                    yield company
                    yielded += 1
                    if max_results and yielded >= max_results:
                        return
                page += 1


def _latest_finances(finances: Optional[dict[str, Any]]) -> tuple[str, Optional[int], Optional[int]]:
    if not finances:
        return "", None, None
    year = max(finances)
    f = finances[year] or {}
    return year, f.get("ca"), f.get("resultat_net")


def parse_result(item: dict[str, Any]) -> Company:
    siege = item.get("siege") or {}
    complements = item.get("complements") or {}
    year, revenue, net_income = _latest_finances(item.get("finances"))
    director = next(
        (d for d in item.get("dirigeants") or [] if d.get("type_dirigeant") == "personne physique"),
        {},
    )
    first_name = (director.get("prenoms") or "").split(" ")[0].title()
    return Company(
        siren=item["siren"],
        name=item.get("nom_complet") or item.get("nom_raison_sociale") or "",
        legal_name=item.get("nom_raison_sociale") or "",
        sigle=item.get("sigle") or "",
        naf=item.get("activite_principale") or siege.get("activite_principale") or "",
        category=item.get("categorie_entreprise") or "",
        headcount_band=item.get("tranche_effectif_salarie") or "",
        creation_date=item.get("date_creation") or "",
        address=siege.get("adresse") or "",
        postal_code=siege.get("code_postal") or "",
        city=siege.get("libelle_commune") or "",
        department=siege.get("departement") or "",
        region=siege.get("region") or "",
        is_individual=bool(complements.get("est_entrepreneur_individuel")),
        director_first_name=first_name,
        director_last_name=(director.get("nom") or "").title(),
        director_role=director.get("qualite") or "",
        revenue=revenue,
        net_income=net_income,
        finances_year=year,
        source=RechercheEntreprisesSource.name,
        extra={"siret_siege": siege.get("siret"), "nombre_etablissements": item.get("nombre_etablissements")},
    )
