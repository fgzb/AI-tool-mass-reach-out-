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
        "connect_timeout": 5,
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
    # When the agent is allowed to send (local time of `timezone`).
    "schedule": {
        "timezone": "Europe/Paris",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "start": "09:00",
        "end": "17:00",
        "skip_french_holidays": True,
        "skip_periods": [],  # month-day ranges, e.g. ["08-01..08-23", "12-24..01-01"]
    },
    # Autonomous mode (`fr-outreach agent`).
    "agent": {
        "tick_seconds": 60,
        "ready_buffer": 150,  # keep this many ready-to-send contacts in stock
        "collect_batch": 50,  # new companies pulled from the registry per tick when stock is low
        "enrich_batch": 20,  # companies whose website + e-mails are looked up per tick
        "inbox_sync_minutes": 30,
        "health_window_days": 7,
        "health_min_sample": 20,
        "max_bounce_rate": 0.05,  # auto-pause above this (bounces + rejections / attempts)
        "max_optout_rate": 0.05,  # auto-pause above this (opt-out replies / sent)
        "max_consecutive_smtp_errors": 5,
        "report_to": "",  # daily report + alerts go to this address
    },
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
        "delay_seconds": 20,  # pause between two messages in a manual `send` run
        "jitter_seconds": 10,
        "max_per_run": 50,
        # Daily quota = min(max_per_day, start_per_day + increase_per_day x days already sent).
        # Ramping up slowly ("warm-up") is what keeps a sending domain out of spam folders.
        "max_per_day": 100,
        "warmup": {"start_per_day": 15, "increase_per_day": 5},
        # Never e-mail the same company again (any campaign) within this many days.
        "recontact_after_days": 180,
        # A/B test: list several templates; each company always gets the same one.
        "templates": [],
        # One follow-up in the same thread if nobody replied, opted out or bounced.
        "followup": {
            "enabled": True,
            "template": "templates/relance_fr.txt",
            "after_business_days": 4,
            "expire_after_days": 14,  # too late after that: skip it
            "max_share": 0.5,  # follow-ups use at most half of the daily quota
        },
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


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env support (KEY=VALUE lines); variables already set in the environment win."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        os.environ.setdefault(key, value.strip().strip("'\""))
