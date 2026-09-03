"""Fetching and reading DB-IP's free CSV exports.

The CSV rather than the MMDB: both are published, and the CSV needs nothing
outside the standard library. `dbip-country-lite` is
``start,end,country_code``; `dbip-city-lite` adds continent, region, city,
latitude and longitude. Neither has a header row.

The download URL carries the year and month, and **the current month's file is
not published on the first of the month**. Every fetch therefore walks back
until something answers, which is also what makes a re-run mid-month
reproducible: it picks the same file the last run did.
"""

from __future__ import annotations

import csv
import gzip
import io
import ipaddress
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import date
from pathlib import Path

BASE = "https://download.db-ip.com/free"

#: Sent on every request, and **not optional**.
#:
#: DB-IP answers 403 to Python's default `Python-urllib/3.x` User-Agent. Any
#: other value is accepted, so this is not authentication and not a limit being
#: worked around — it is a bot filter that the standard library's default trips
#: over. Removing it as noise turns the monthly build into a 403 with a
#: traceback and nothing naming the cause.
USER_AGENT = "ipgeo-shards (+https://github.com/lollipopkit/ipgeo-shards)"

#: A hung connection must not hang the build. DB-IP serves 90 MB, so this is
#: per read rather than for the whole transfer.
TIMEOUT = 60

#: How many months back to look before giving up.
#:
#: Three, not one. One covers the first of the month; three covers DB-IP
#: skipping a month, which would otherwise stop the data being refreshed at all
#: with nothing but a CI failure to say so.
MAX_MONTHS_BACK = 3


def _months_back(today: date, n: int) -> Iterator[str]:
    year, month = today.year, today.month
    for _ in range(n + 1):
        yield f"{year:04d}-{month:02d}"
        month -= 1
        if month == 0:
            year, month = year - 1, 12


def fetch(dataset: str, cache: Path, today: date | None = None) -> tuple[Path, str]:
    """Downloads ``dataset`` ("country" or "city"), newest month first.

    Returns the local path and the month it came from. A file already in
    ``cache`` is used as it is, so a local re-run does not re-download 85 MB.
    """
    cache.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for month in _months_back(today or date.today(), MAX_MONTHS_BACK):
        name = f"dbip-{dataset}-lite-{month}.csv.gz"
        path = cache / name
        if path.exists() and path.stat().st_size > 0:
            print(f"using cached {name}")
            return path, month
        url = f"{BASE}/{name}"
        try:
            print(f"fetching {url}")
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                data = response.read()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            print(f"  not published yet ({e.code})")
            last_error = e
            continue
        # Written whole, then renamed: a run interrupted mid-download must not
        # leave a truncated file that the next one takes for a cache hit.
        tmp = path.with_suffix(".partial")
        tmp.write_bytes(data)
        tmp.rename(path)
        return path, month

    raise SystemExit(
        f"no {dataset} export published in the last {MAX_MONTHS_BACK} months "
        f"({last_error})"
    )


def rows(path: Path) -> Iterator[list[str]]:
    """Every row, as strings.

    `errors="replace"` because a city name is free text from a source this
    repository does not control, and one undecodable byte must not stop the
    month's build. The names are not used for anything but grouping.
    """
    with gzip.open(path, "rb") as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        yield from csv.reader(text)


def as_int(address: str) -> int | None:
    """An address as an integer, or None if it is not one.

    Both families, because both files carry both. The caller decides which it
    wants by looking for a colon, which is cheaper than parsing twice.
    """
    try:
        return int(ipaddress.ip_address(address))
    except ValueError:
        return None
