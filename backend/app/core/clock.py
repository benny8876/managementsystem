"""Hospital-local wall-clock time (Myanmar, UTC+06:30, no DST).

Every stored timestamp/date is naive local time, so "today", day/month report
windows, and what the screens show all line up with the clock on the wall.
Auth token expiry (app/core/security.py) deliberately stays on UTC.
"""
from datetime import date, datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=6, minutes=30), "Asia/Yangon")
UTC_OFFSET_MINUTES = 390


def now() -> datetime:
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def today() -> date:
    return now().date()
