"""Pappers API (paid, needs an API token): financial filters (CA, résultat) and,
depending on your plan, the company website.

Docs: https://www.pappers.fr/api/documentation
Set the token in the environment variable named by `pappers.api_token_env`
(default PAPPERS_API_TOKEN).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator, Optional

import requests

from ..models import Company

log = logging.getLogger(__name__)

BASE = "https://api.pappers.fr/v2"
WEBSITE_KEYS = ("site_internet", "site_web", "sites_internet", "website")


def _website_from(item: dict[str, Any]) -> str:
    for key in WEBSITE_KEYS:
        value = item.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        if value:
            return str(value)
    return ""


class PappersSource:
    name = "pappers"

    def __init__(self, api_token: str, session: Optional[requests.Session] = None, timeout: int = 20):
        if not api_token:
            raise ValueError("Pappers needs an API token (env PAPPERS_API_TOKEN).")
        self.token = api_token
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(5):
            resp = self.session.get(f"{BASE}{path}", params={**params, "api_token": self.token}, timeout=self.timeout)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("Pappers: too many retries")

    def search(self, search: dict[str, Any]) -> Iterator[Company]:
        params: dict[str, Any] = {"entreprise_cessee": "false", "par_page": 100}
        if search.get("query"):
            params["q"] = search["query"]
        if search.get("departments"):
            params["departement"] = ",".join(search["departments"])
        if search.get("regions"):
            params["region"] = ",".join(search["regions"])
        if search.get("postal_codes"):
            params["code_postal"] = ",".join(search["postal_codes"])
        if search.get("naf_codes"):
            params["code_naf"] = ",".join(search["naf_codes"])
        if search.get("revenue_min") is not None:
            params["chiffre_affaires_min"] = search["revenue_min"]
        if search.get("revenue_max") is not None:
            params["chiffre_affaires_max"] = search["revenue_max"]
        # Pappers has no INSEE "catégorie d'entreprise" filter: use revenue_min/max
        # and headcount instead to target SMEs.
        max_results = search.get("max_results") or None

        page, yielded = 1, 0
        while True:
            data = self._get("/recherche", {**params, "page": page})
            results = data.get("resultats") or []
            for item in results:
                if search.get("exclude_individual", True) and item.get("entreprise_individuelle"):
                    continue
                yield self.parse(item)
                yielded += 1
                if max_results and yielded >= max_results:
                    return
            if not results or page * params["par_page"] >= (data.get("total") or 0):
                return
            page += 1

    def enrich(self, siren: str) -> dict[str, Any]:
        """Full company record (finances, representatives, website if your plan exposes it)."""
        return self._get("/entreprise", {"siren": siren})

    @staticmethod
    def parse(item: dict[str, Any]) -> Company:
        siege = item.get("siege") or {}
        reps = [r for r in item.get("representants") or [] if r.get("personne_morale") is False]
        rep = reps[0] if reps else {}
        return Company(
            siren=item["siren"],
            name=item.get("nom_entreprise") or item.get("denomination") or "",
            legal_name=item.get("denomination") or item.get("nom_entreprise") or "",
            naf=item.get("code_naf") or "",
            category=item.get("categorie_entreprise") or "",
            headcount_band=item.get("tranche_effectif") or "",
            creation_date=item.get("date_creation") or "",
            address=" ".join(filter(None, [siege.get("adresse_ligne_1"), siege.get("code_postal"), siege.get("ville")])),
            postal_code=siege.get("code_postal") or "",
            city=siege.get("ville") or "",
            department=siege.get("departement") or "",
            region=siege.get("region") or "",
            is_individual=bool(item.get("entreprise_individuelle")),
            director_first_name=(rep.get("prenom_usuel") or rep.get("prenom") or "").title(),
            director_last_name=(rep.get("nom") or "").title(),
            director_role=rep.get("qualite") or "",
            revenue=item.get("chiffre_affaires"),
            net_income=item.get("resultat"),
            finances_year=str(item.get("annee_finances") or ""),
            website=_website_from(item),
            website_source=PappersSource.name if _website_from(item) else "",
            source=PappersSource.name,
        )
