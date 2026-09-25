"""Plain data containers shared by every stage of the pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass(slots=True)  # SIRENE imports hold many of these in memory
class Company:
    siren: str
    name: str
    legal_name: str = ""
    sigle: str = ""
    naf: str = ""  # code APE / NAF, e.g. "62.01Z"
    category: str = ""  # PME / ETI / GE (INSEE "catégorie d'entreprise")
    headcount_band: str = ""  # INSEE "tranche d'effectif salarié" code
    creation_date: str = ""
    address: str = ""
    postal_code: str = ""
    city: str = ""
    department: str = ""
    region: str = ""
    is_individual: bool = False  # entrepreneur individuel
    director_first_name: str = ""
    director_last_name: str = ""
    director_role: str = ""
    revenue: Optional[int] = None  # chiffre d'affaires (EUR)
    net_income: Optional[int] = None
    finances_year: str = ""
    website: str = ""
    website_source: str = ""
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("extra")
        row["is_individual"] = int(self.is_individual)
        return row


@dataclass(slots=True)
class EmailCandidate:
    email: str
    source_url: str
    score: int = 0
    kind: str = ""  # "role", "personal", "freemail", ...


# INSEE "tranche d'effectif salarié" codes -> human label.
HEADCOUNT_BANDS = {
    "NN": "non renseigné",
    "00": "0 salarié",
    "01": "1 ou 2 salariés",
    "02": "3 à 5 salariés",
    "03": "6 à 9 salariés",
    "11": "10 à 19 salariés",
    "12": "20 à 49 salariés",
    "21": "50 à 99 salariés",
    "22": "100 à 199 salariés",
    "31": "200 à 249 salariés",
    "32": "250 à 499 salariés",
    "41": "500 à 999 salariés",
    "42": "1 000 à 1 999 salariés",
    "51": "2 000 à 4 999 salariés",
    "52": "5 000 à 9 999 salariés",
    "53": "10 000 salariés et plus",
}
