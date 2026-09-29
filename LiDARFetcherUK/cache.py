"""Persistent cache of dataset information that does not depend on the area.

Each source stores what it fetched (last modified dates, collection lists) as
{"fetched": ISO timestamp, "metadata": {...}} under its NAME. Searches use it
as is; it is refetched by Refresh metadata, when a source has no entry, or once
it is older than MAX_AGE_DAYS. If a fetch fails the old entry is kept. Lives in
the QGIS profile folder so it survives plugin updates.
"""
import json
import os
from datetime import datetime, timezone

from qgis.core import QgsApplication

PATH = os.path.normpath(os.path.join(QgsApplication.qgisSettingsDirPath(), "LiDARFetcherUK",
                                     "catalogue_cache.json"))
# Searches refetch a source's dataset info once its cache is older than this.
MAX_AGE_DAYS = 30


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(data):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, PATH)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def age_days(fetched):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(fetched)).total_seconds() / 86400


def describe(fetched):
    """'29 Sep 2026 10:15 (today)' in local time, from an ISO timestamp."""
    when = datetime.fromisoformat(fetched).astimezone()
    days = (datetime.now().astimezone().date() - when.date()).days
    age = "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"
    return f"{when:%d %b %Y %H:%M} ({age})"
