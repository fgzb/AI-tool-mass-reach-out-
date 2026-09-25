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
from typing import Any, Iterator, Optional

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

    @staticmethod
    def _company(row: dict[str, str], search: dict[str, Any]) -> Optional[Company]:
        """Company from a StockUniteLegale row, or None if it does not match the filters."""
        if row.get("etatAdministratifUniteLegale") != "A":
            return None
        if row.get("statutDiffusionUniteLegale") not in (None, "", "O"):
            return None  # non-diffusible: must not be used
        if search["_categories"] and row.get("categorieEntreprise") not in search["_categories"]:
            return None
        if search["_nafs"] and row.get("activitePrincipaleUniteLegale") not in search["_nafs"]:
            return None
        if search["_bands"] and row.get("trancheEffectifsUniteLegale") not in search["_bands"]:
            return None
        # Catégorie juridique 1000 = entrepreneur individuel.
        is_individual = row.get("categorieJuridiqueUniteLegale") == "1000"
        if search.get("exclude_individual", True) and is_individual:
            return None
        name = (
            row.get("denominationUniteLegale")
            or row.get("denominationUsuelle1UniteLegale")
            or " ".join(filter(None, [row.get("prenom1UniteLegale"), row.get("nomUniteLegale")]))
        )
        return Company(
            siren=row["siren"],
            name=name,
            legal_name=row.get("denominationUniteLegale") or name,
            sigle=row.get("sigleUniteLegale") or "",
            naf=row.get("activitePrincipaleUniteLegale") or "",
            category=row.get("categorieEntreprise") or "",
            headcount_band=row.get("trancheEffectifsUniteLegale") or "",
            creation_date=row.get("dateCreationUniteLegale") or "",
            is_individual=is_individual,
            source=SireneStockSource.name,
        )

    @staticmethod
    def _head_office(row: dict[str, str], search: dict[str, Any]) -> Optional[tuple[str, str, str, str, str]]:
        """(address, postal code, city, department, siret) of a head office matching the filters."""
        if row.get("etablissementSiege") != "true":
            return None
        dept = department_of(row.get("codeCommuneEtablissement") or "")
        postal = row.get("codePostalEtablissement") or ""
        if search["_departments"] and dept not in search["_departments"]:
            return None
        if search["_postal_codes"] and postal not in search["_postal_codes"]:
            return None
        street = " ".join(filter(None, [
            row.get("numeroVoieEtablissement"), row.get("indiceRepetitionEtablissement"),
            row.get("typeVoieEtablissement"), row.get("libelleVoieEtablissement"),
        ]))
        city = row.get("libelleCommuneEtablissement") or ""
        return " ".join(filter(None, [street, postal, city])), postal, city, dept, row.get("siret") or ""

    @staticmethod
    def _with_address(company: Company, office: tuple[str, str, str, str, str]) -> Company:
        company.address, company.postal_code, company.city, company.department, siret = office
        company.extra["siret_siege"] = siret
        return company

    def search(self, search: dict[str, Any]) -> Iterator[Company]:
        search = {
            **search,
            "_categories": set(search.get("categories") or []), "_nafs": set(search.get("naf_codes") or []),
            "_bands": set(search.get("headcount_bands") or []), "_departments": set(search.get("departments") or []),
            "_postal_codes": set(search.get("postal_codes") or []),
        }
        max_results = search.get("max_results") or None
        if search.get("regions"):
            log.warning("SIRENE stock files have no region column; use 'departments' instead.")
        geo = bool(search["_departments"] or search["_postal_codes"])
        if geo and not self.etab_path:
            raise ValueError("Filtering by department/postal code needs the StockEtablissement file.")

        def limited(companies: Iterator[Company]) -> Iterator[Company]:
            for n, company in enumerate(companies, 1):
                yield company
                if max_results and n >= max_results:
                    return

        if geo:
            # Geographic filter first: only the head offices of the area are kept in memory
            # (tens of thousands), instead of every matching company in France (millions).
            offices: dict[str, tuple[str, str, str, str, str]] = {}
            with _open_csv(self.etab_path) as reader:  # type: ignore[arg-type]
                for row in reader:
                    office = self._head_office(row, search)
                    if office:
                        offices[row["siren"]] = office
            log.info("%d head offices in the selected area", len(offices))

            def from_units() -> Iterator[Company]:
                with _open_csv(self.ul_path) as reader:
                    for row in reader:
                        office = offices.get(row.get("siren", ""))
                        if office and (company := self._company(row, search)):
                            yield self._with_address(company, office)

            yield from limited(from_units())
            return

        companies: dict[str, Company] = {}
        with _open_csv(self.ul_path) as reader:
            for row in reader:
                if company := self._company(row, search):
                    companies[company.siren] = company
        log.info("%d companies match the unit-level filters", len(companies))
        if not self.etab_path:
            yield from limited(iter(companies.values()))
            return

        def from_offices() -> Iterator[Company]:
            with _open_csv(self.etab_path) as reader:  # type: ignore[arg-type]
                for row in reader:
                    company = companies.get(row.get("siren", ""))
                    if company and (office := self._head_office(row, search)):
                        yield self._with_address(company, office)

        yield from limited(from_offices())
