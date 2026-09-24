"""Bulk import from the SIRENE stock files (data.gouv.fr / INSEE).

Download both files (CSV, or the .zip as published) from
https://www.data.gouv.fr/fr/datasets/base-sirene-des-entreprises-et-de-leurs-etablissements-siren-siret/
  * StockUniteLegale_utf8.(csv|zip)      - one row per company (SIREN)
  * StockEtablissement_utf8.(csv|zip)    - one row per establishment (SIRET), used for the head-office address

The files are several GB, so both are streamed row by row. Only companies that
are active, fully public ("statutDiffusion" = O) and match the filters are kept.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from contextlib import contextmanager
from typing import Any, Iterator

from ..models import Company

log = logging.getLogger(__name__)


@contextmanager
def _open_csv(path: str) -> Iterator[csv.DictReader]:
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            member = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            with zf.open(member) as raw:
                yield csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
    else:
        with open(path, encoding="utf-8", newline="") as fh:
            yield csv.DictReader(fh)


def department_of(code_commune: str) -> str:
    """Department from an INSEE commune code (handles Corsica 2A/2B and overseas 97x)."""
    if not code_commune:
        return ""
    return code_commune[:3] if code_commune.startswith("97") else code_commune[:2]


class SireneStockSource:
    name = "sirene-stock"

    def __init__(self, unites_legales_path: str, etablissements_path: str | None = None):
        self.ul_path = unites_legales_path
        self.etab_path = etablissements_path

    def search(self, search: dict[str, Any]) -> Iterator[Company]:
        categories = set(search.get("categories") or [])
        nafs = set(search.get("naf_codes") or [])
        bands = set(search.get("headcount_bands") or [])
        departments = set(search.get("departments") or [])
        postal_codes = set(search.get("postal_codes") or [])
        exclude_individual = search.get("exclude_individual", True)
        max_results = search.get("max_results") or None
        if search.get("regions"):
            log.warning("SIRENE stock files have no region column; use 'departments' instead.")

        companies: dict[str, Company] = {}
        with _open_csv(self.ul_path) as reader:
            for row in reader:
                if row.get("etatAdministratifUniteLegale") != "A":
                    continue
                if row.get("statutDiffusionUniteLegale") not in (None, "", "O"):
                    continue  # non-diffusible: must not be used
                if categories and row.get("categorieEntreprise") not in categories:
                    continue
                if nafs and row.get("activitePrincipaleUniteLegale") not in nafs:
                    continue
                if bands and row.get("trancheEffectifsUniteLegale") not in bands:
                    continue
                # Catégorie juridique 1000 = entrepreneur individuel.
                is_individual = row.get("categorieJuridiqueUniteLegale") == "1000"
                if exclude_individual and is_individual:
                    continue
                name = (
                    row.get("denominationUniteLegale")
                    or row.get("denominationUsuelle1UniteLegale")
                    or " ".join(filter(None, [row.get("prenom1UniteLegale"), row.get("nomUniteLegale")]))
                )
                companies[row["siren"]] = Company(
                    siren=row["siren"],
                    name=name,
                    legal_name=row.get("denominationUniteLegale") or name,
                    sigle=row.get("sigleUniteLegale") or "",
                    naf=row.get("activitePrincipaleUniteLegale") or "",
                    category=row.get("categorieEntreprise") or "",
                    headcount_band=row.get("trancheEffectifsUniteLegale") or "",
                    creation_date=row.get("dateCreationUniteLegale") or "",
                    is_individual=is_individual,
                    source=self.name,
                    extra={"nic_siege": row.get("nicSiegeUniteLegale")},
                )
        log.info("%d companies match the unit-level filters", len(companies))

        if not self.etab_path:
            if departments or postal_codes:
                raise ValueError("Filtering by department/postal code needs the StockEtablissement file.")
            yield from list(companies.values())[:max_results]
            return

        yielded = 0
        with _open_csv(self.etab_path) as reader:
            for row in reader:
                if row.get("etablissementSiege") != "true":
                    continue
                company = companies.get(row.get("siren", ""))
                if company is None:
                    continue
                dept = department_of(row.get("codeCommuneEtablissement") or "")
                postal = row.get("codePostalEtablissement") or ""
                if departments and dept not in departments:
                    continue
                if postal_codes and postal not in postal_codes:
                    continue
                street = " ".join(
                    filter(
                        None,
                        [
                            row.get("numeroVoieEtablissement"),
                            row.get("indiceRepetitionEtablissement"),
                            row.get("typeVoieEtablissement"),
                            row.get("libelleVoieEtablissement"),
                        ],
                    )
                )
                company.address = " ".join(filter(None, [street, postal, row.get("libelleCommuneEtablissement")]))
                company.postal_code = postal
                company.city = row.get("libelleCommuneEtablissement") or ""
                company.department = dept
                company.extra["siret_siege"] = row.get("siret")
                yield company
                yielded += 1
                if max_results and yielded >= max_results:
                    return
