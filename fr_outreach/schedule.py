"""When to send, and how much: sending window, French public holidays, warm-up quota."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

log = logging.getLogger(__name__)

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def get_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # no tz database (e.g. Windows without the tzdata package)
        log.warning("Time zone %r unavailable, using the machine's local time.", name)
        return datetime.now().astimezone().tzinfo or timezone.utc


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def french_holidays(year: int) -> set[date]:
    e = easter(year)
    return {
        date(year, 1, 1),
        e + timedelta(days=1),  # lundi de Pâques
        date(year, 5, 1),
        date(year, 5, 8),
        e + timedelta(days=39),  # Ascension
        e + timedelta(days=50),  # lundi de Pentecôte
        date(year, 7, 14),
        date(year, 8, 15),
        date(year, 11, 1),
        date(year, 11, 11),
        date(year, 12, 25),
    }


def _in_period(day: date, period: str) -> bool:
    """period: "MM-DD..MM-DD" (may wrap over the new year)."""
    start_s, _, end_s = period.partition("..")
    start = tuple(int(x) for x in start_s.strip().split("-"))
    end = tuple(int(x) for x in (end_s or start_s).strip().split("-"))
    md = (day.month, day.day)
    return start <= md <= end if start <= end else (md >= start or md <= end)


def parse_hhmm(value: str) -> time:
    hours, _, minutes = str(value).partition(":")
    return time(int(hours), int(minutes or 0))


class Schedule:
    def __init__(self, cfg: dict[str, Any]):
        self.tz = get_tz(cfg.get("timezone") or "Europe/Paris")
        self.days = {DAY_NAMES.index(d.lower()[:3]) for d in cfg.get("days") or DAY_NAMES[:5]}
        self.start = parse_hhmm(cfg.get("start") or "09:00")
        self.end = parse_hhmm(cfg.get("end") or "17:00")
        self.skip_holidays = cfg.get("skip_french_holidays", True)
        self.skip_periods = list(cfg.get("skip_periods") or [])
        if self.end <= self.start:
            raise ValueError("schedule.end must be after schedule.start")

    def local(self, now_utc: datetime) -> datetime:
        return now_utc.astimezone(self.tz)

    def is_sending_day(self, day: date) -> bool:
        if day.weekday() not in self.days:
            return False
        if self.skip_holidays and day in french_holidays(day.year):
            return False
        return not any(_in_period(day, p) for p in self.skip_periods)

    def window(self, day: date) -> tuple[datetime, datetime]:
        return (
            datetime.combine(day, self.start, tzinfo=self.tz),
            datetime.combine(day, self.end, tzinfo=self.tz),
        )

    def in_window(self, now_utc: datetime) -> bool:
        local = self.local(now_utc)
        start, end = self.window(local.date())
        return self.is_sending_day(local.date()) and start <= local < end

    def day_start_utc(self, now_utc: datetime) -> datetime:
        local = self.local(now_utc)
        return datetime.combine(local.date(), time(0), tzinfo=self.tz).astimezone(timezone.utc)

    def business_days_cutoff(self, now_utc: datetime, days: int) -> datetime:
        """UTC instant before which something happened at least `days` sending days ago.

        E.g. on a Friday with days=4, anything sent on Monday or earlier is before the cutoff.
        """
        day = self.local(now_utc).date()
        counted = 0
        while counted < days:
            day -= timedelta(days=1)
            if self.is_sending_day(day):
                counted += 1
        return datetime.combine(day + timedelta(days=1), time(0), tzinfo=self.tz).astimezone(timezone.utc)

    def send_interval(self, quota: int) -> timedelta:
        """Average spacing that spreads `quota` messages over the window (with some slack)."""
        start, end = self.window(date(2000, 1, 3))
        return (end - start) * 0.9 / max(quota, 1)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def daily_quota(store: Any, mail_cfg: dict[str, Any], day_start_utc: datetime) -> int:
    """Warm-up: start small, add a few messages for every day already sent, up to max_per_day."""
    cap = int(mail_cfg.get("max_per_day") or 0) or 10**9
    warm = mail_cfg.get("warmup") or {}
    if not warm.get("start_per_day"):
        return cap
    days = store.sending_days_before(iso(day_start_utc))
    return min(cap, int(warm["start_per_day"]) + int(warm.get("increase_per_day") or 0) * days)
