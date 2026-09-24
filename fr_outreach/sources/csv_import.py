"""Import any CSV export: Dealroom, Crunchbase, Les Pépites Tech, French Tech
lists, Diane/Orbis, Altares, CAPFI, Pappers exports...

Those providers have no free API (or forbid scraping), so the practical route is
to export a list from their UI and map its columns here. A SIREN column is
required so records can be de-duplicated and cross-checked with SIRENE; if the
export has no SIREN, run it through `fr-outreach collect --source api
--query "<name>"` first, or add the column manually.
"""
from __future__ import annotations

import csv
import re
from typing import Any, Iterator, Optional

from ..models import Company

# Our field -> column names commonly used by the various exports (case-insensitive).
DEFAULT_MAPPING: dict[str, list[str]] = {
    "siren": ["siren", "siren number", "numéro siren", "company registration number"],
    "name": ["name", "nom", "company name", "organization name", "denomination", "dénomination", "raison sociale"],
    "website": ["website", "site web", "site internet", "url", "homepage", "company website"],
    "city": ["city", "ville", "hq city", "headquarters location", "commune"],
    "postal_code": ["postal code", "code postal", "zip", "cp"],
    "naf": ["naf", "code naf", "ape", "code ape"],
    "revenue": ["revenue", "chiffre d'affaires", "ca", "turnover", "operating revenue (turnover)"],
    "net_income": ["net income", "résultat net", "resultat net", "p/l for period"],
}


def _to_int(value: str) -> Optional[int]:
    digits = re.sub(r"[^\d-]", "", value or "")
    return int(digits) if digits not in ("", "-") else None


class CsvSource:
    name = "csv"

    def __init__(self, path: str, label: str = "csv", mapping: Optional[dict[str, list[str]]] = None, delimiter: str = ""):
        self.path = path
        self.label = label
        self.mapping = {**DEFAULT_MAPPING, **(mapping or {})}
        self.delimiter = delimiter

    def search(self, search: dict[str, Any]) -> Iterator[Company]:
        with open(self.path, encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            delimiter = self.delimiter or csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
            reader = csv.DictReader(fh, delimiter=delimiter)
            lookup = {(h or "").strip().lower(): h for h in reader.fieldnames or []}
            columns = {
                field: next((lookup[a] for a in aliases if a in lookup), None) for field, aliases in self.mapping.items()
            }
            if not columns["siren"]:
                raise ValueError(f"{self.path}: no SIREN column found (looked for {self.mapping['siren']}).")
            max_results = search.get("max_results") or None
            for n, row in enumerate(reader):
                if max_results and n >= max_results:
                    return
                get = lambda f: (row.get(columns[f]) or "").strip() if columns[f] else ""  # noqa: E731
                siren = re.sub(r"\D", "", get("siren"))[:9]
                if len(siren) != 9:
                    continue
                website = get("website")
                yield Company(
                    siren=siren,
                    name=get("name"),
                    legal_name=get("name"),
                    naf=get("naf"),
                    postal_code=get("postal_code"),
                    city=get("city"),
                    department=get("postal_code")[:2],
                    revenue=_to_int(get("revenue")),
                    net_income=_to_int(get("net_income")),
                    website=website,
                    website_source=self.label if website else "",
                    source=self.label,
                )
