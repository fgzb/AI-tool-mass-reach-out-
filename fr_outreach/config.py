"""Configuration loading (YAML file + environment variables for secrets)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "database": "data/outreach.sqlite",
    "http": {
        "user_agent": "FrOutreachBot/0.1 (+contact: set-your-address@example.com)",
        "timeout": 15,
        "max_bytes": 2_000_000,
        "per_host_delay": 1.0,
        "respect_robots_txt": True,
        "workers": 8,
    },
    "search": {
        # Annuaire des Entreprises / API Recherche d'entreprises filters
        "query": "",
        "categories": ["PME", "ETI"],
        "departments": [],
        "regions": [],
        "postal_codes": [],
        "naf_codes": [],
        "naf_sections": [],
        "headcount_bands": [],
        "revenue_min": None,
        "revenue_max": None,
        "exclude_individual": True,
        "max_results": 1000,
    },
    "discovery": {
        "search_provider": None,  # "brave" or None
        "brave_api_key_env": "BRAVE_API_KEY",
        "guess_domains": True,
        "min_confidence": 50,
    },
    "scraping": {
        "max_pages_per_site": 6,
        "max_emails_per_company": 1,
        "check_mx": True,
    },
    "pappers": {"api_token_env": "PAPPERS_API_TOKEN"},
    "mail": {
        "campaign": "default",
        "template": "templates/prospection_fr.txt",
        "from_name": "",
        "from_address": "",
        "reply_to": "",
        "company_legal_name": "",
        "company_address": "",
        "unsubscribe_url": "",
        "unsubscribe_mailto": "",
        "data_source_notice": (
            "Vos coordonnées professionnelles ont été collectées à partir du registre "
            "public des entreprises (INSEE/SIRENE) et de votre site internet."
        ),
        "smtp": {
            "host": "",
            "port": 587,
            "security": "starttls",  # starttls | ssl | none
            "username_env": "SMTP_USERNAME",
            "password_env": "SMTP_PASSWORD",
        },
        "imap": {
            "host": "",
            "port": 993,
            "username_env": "IMAP_USERNAME",
            "password_env": "IMAP_PASSWORD",
            "folder": "INBOX",
        },
        "delay_seconds": 20,
        "jitter_seconds": 10,
        "max_per_run": 50,
        "max_per_day": 200,
        "outbox_dir": "outbox",
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if path and Path(path).exists():
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    elif path:
        raise FileNotFoundError(f"Config file not found: {path}")
    return _merge(DEFAULTS, data)


def secret(env_name: str | None) -> str:
    return os.environ.get(env_name or "", "")
