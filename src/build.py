#!/usr/bin/env python3
"""Builds everything this repository publishes.

    python3 src/build.py --out public

Two artefacts, from two DB-IP exports:

``country_v1.bin``
    One coordinate per country and a range table over both address families.
    About 4 MB, bundled *inside* the app — so it answers offline, on a first
    run, and for anyone who leaves the shard download switched off.

``v1/<month>/ip4/NNN.bin`` + ``v1/index.json``
    City-level coordinates, one file per /8, fetched on demand. The month is
    in the path so a shard really is immutable: the data changes monthly, and
    at a stable URL a thirty-day `Cache-Control` would mean a CDN serving last
    month's answer for another thirty days. The index names the directory, so
    a client never builds the month into a path itself.

Deterministic: same inputs, byte-identical outputs. Every dictionary is walked
in sorted order and nothing depends on the clock beyond which month's export
was downloaded.
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
from pathlib import Path

import dbip
import formats
from formats import CountryRange, ShardRecord

#: DB-IP's code for "not known", which is not a place.
UNKNOWN_CC = "ZZ"

#: Null Island. Every geo dataset has rows there and none of them mean it; it
#: is what a missing coordinate looks like after being written to a CSV.
NULL_ISLAND = (0.0, 0.0)


# --------------------------------------------------------------------------
# Where a country is drawn
# --------------------------------------------------------------------------


def country_coords(city_csv: Path) -> dict[str, tuple[float, float]]:
    """One coordinate per country: its most-addressed city.

    **Not its capital**, which is what the design started out saying. Two
    reasons, and the second is the one that decided it:

    - Coverage. Natural Earth's populated places mark 195 countries as holding
      an ``Admin-0 capital``; DB-IP knows 242. The 47 missing are territories
      and dependencies — Hong Kong, Macau, Puerto Rico, Greenland, Palestine —
      and several of them hold a great many servers.
    - This app draws servers, and servers are in data centres. Weighting each
      city by how many addresses it covers puts Germany at Frankfurt rather
      than Berlin, and the United States in Columbus rather than Washington.
      For a country-level fallback that is the better answer, not a worse one.

    A geometric centroid was never a candidate: it lands in the ocean for
    anywhere shaped like Indonesia, and somewhere meaningless for anywhere
    shaped like Chile.
    """
    weight: dict[str, collections.Counter[tuple[str, str]]] = collections.defaultdict(
        collections.Counter
    )
    coord: dict[tuple[str, str], tuple[float, float]] = {}

    for row in dbip.rows(city_csv):
        if len(row) < 8:
            continue
        start, end, _continent, cc, _region, city, lat, lon = row[:8]
        if cc == UNKNOWN_CC or not city or ":" in start:
            # IPv4 only for the weighting. Both families would do, but v6
            # allocations are handed out in blocks so vast that one /32 in a
            # small city would outweigh every /24 in the capital.
            continue
        a, b = dbip.as_int(start), dbip.as_int(end)
        if a is None or b is None or b < a:
            continue
        try:
            point = (float(lat), float(lon))
        except ValueError:
            continue
        if point == NULL_ISLAND:
            continue
        key = (cc, city)
        weight[cc][key] += b - a + 1
        coord.setdefault(key, point)

    out: dict[str, tuple[float, float]] = {}
    for cc, cities in weight.items():
        # Sorted before `max`, so a tie between two cities with the same
        # address count resolves the same way on every run.
        best = max(sorted(cities), key=lambda key: (cities[key], key))
        out[cc] = coord[best]
    return out


# --------------------------------------------------------------------------
# The country database
# --------------------------------------------------------------------------


def country_ranges(
    country_csv: Path, known: set[str]
) -> tuple[list[CountryRange], list[CountryRange]]:
    """The range tables, with gaps made explicit and neighbours merged.

    A record covers up to the next record's start, so three things have to
    happen here and each is silent if it does not:

    - a hole between two ranges becomes a gap record, or it inherits the
      country before it
    - a country with no coordinate becomes a gap, or its index points nowhere
    - two adjacent ranges of one country collapse into one record

    IPv6 keys on its top [formats.IP6_KEY_BITS] bits — see there for why that
    width and not another.
    """
    raw: dict[str, list[tuple[int, int, str | None]]] = {"ip4": [], "ip6": []}

    for row in dbip.rows(country_csv):
        if len(row) < 3:
            continue
        start, end, cc = row[0], row[1], row[2]
        a, b = dbip.as_int(start), dbip.as_int(end)
        if a is None or b is None or b < a:
            continue
        code = None if cc == UNKNOWN_CC or cc not in known else cc
        if ":" in start:
            shift = 128 - formats.IP6_KEY_BITS
            raw["ip6"].append((a >> shift, b >> shift, code))
        else:
            raw["ip4"].append((a, b, code))

    return _collapse(raw["ip4"]), _collapse(raw["ip6"])


def _collapse(entries: list[tuple[int, int, str | None]]) -> list[CountryRange]:
    """Sorted ranges to the record list the format wants."""
    out: list[CountryRange] = []
    end = -1
    for start, stop, code in sorted(entries):
        if start <= end:
            # Two source rows keyed to the same value — which happens on IPv6,
            # where 2,238 of DB-IP's ranges share a /48 with a range of another
            # country — or overlapping ones. The lower already covers this
            # address; taking the later would depend on sort order among
            # equals.
            end = max(end, stop)
            continue
        if end >= 0 and start > end + 1:
            out.append(CountryRange(end + 1, None))
        elif out and out[-1].code == code:
            end = stop
            continue
        out.append(CountryRange(start, code))
        end = stop
    return out


# --------------------------------------------------------------------------
# The city shards
# --------------------------------------------------------------------------


def ip4_shards(city_csv: Path) -> dict[int, list[ShardRecord]]:
    """City coordinates, split by the first octet.

    Merging adjacent ranges that quantise to the same point is done here and
    is worth almost nothing — it removes about 1% of the records, because
    DB-IP has already merged everything that shares a city. It stays because
    the 1% is free and because leaving it out would make the format's "no two
    consecutive records may be equal" rule untrue.
    """
    span = 1 << (formats.IP4_KEY_BITS - formats.IP4_SHARD_BITS)
    rows: dict[int, list[tuple[int, int, int, int]]] = collections.defaultdict(list)

    for row in dbip.rows(city_csv):
        if len(row) < 8:
            continue
        start, end, _continent, cc, _region, _city, lat, lon = row[:8]
        if ":" in start or cc == UNKNOWN_CC:
            continue
        a, b = dbip.as_int(start), dbip.as_int(end)
        if a is None or b is None or b < a:
            continue
        try:
            point = (float(lat), float(lon))
        except ValueError:
            continue
        if point == NULL_ISLAND:
            continue
        la, lo = formats.quantise(*point)
        # A range that crosses a /8 boundary is split, so every shard is
        # self-contained and a lookup never has to consider its neighbour.
        shard_shift = formats.IP4_KEY_BITS - formats.IP4_SHARD_BITS
        first, last = a >> shard_shift, b >> shard_shift
        for shard in range(first, last + 1):
            base = shard * span
            rows[shard].append(
                (max(a, base) - base, min(b, base + span - 1) - base, la, lo)
            )

    shards: dict[int, list[ShardRecord]] = {}
    for shard in sorted(rows):
        entries = sorted(rows[shard])
        out: list[ShardRecord] = []
        end = -1
        for a, b, la, lo in entries:
            if a <= end:
                # Overlapping source rows. The lower one already covers this
                # address; taking the later one would depend on sort order
                # among equals.
                end = max(end, b)
                continue
            if a > end + 1:
                out.append(ShardRecord(end + 1, formats.NO_DATA, 0))
            elif out and (out[-1].lat, out[-1].lon) == (la, lo):
                end = b
                continue
            out.append(ShardRecord(a, la, lo))
            end = b
        if out and out[0].offset != 0:
            out.insert(0, ShardRecord(0, formats.NO_DATA, 0))
        if out:
            shards[shard] = out
    return shards


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("public"))
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(".cache"),
        help="where the DB-IP exports are kept between runs",
    )
    args = parser.parse_args()

    country_csv, country_month = dbip.fetch("country", args.cache)
    city_csv, city_month = dbip.fetch("city", args.cache)

    print("deriving country coordinates")
    coords = country_coords(city_csv)
    print(f"  {len(coords)} countries")

    print("building the country range tables")
    ip4, ip6 = country_ranges(country_csv, set(coords))
    print(f"  {len(ip4):,} IPv4 records, {len(ip6):,} IPv6 records")

    blob = formats.build_country_db(coords, ip4, ip6)
    country_out = args.out / "country_v1.bin"
    country_out.parent.mkdir(parents=True, exist_ok=True)
    country_out.write_bytes(blob)
    print(f"  wrote {country_out} ({len(blob) / 1024 / 1024:.2f} MB)")

    print("building the IPv4 city shards")
    shards = ip4_shards(city_csv)
    shard_rel = f"v1/{city_month}/ip4"
    shard_dir = args.out / shard_rel
    # The whole `v1` tree, not just this month's directory: a build carries
    # one month, so leaving the last one behind would serve two.
    if (args.out / "v1").exists():
        shutil.rmtree(args.out / "v1")
    shard_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    largest = 0
    for shard in sorted(shards):
        data = formats.build_ip4_shard(shards[shard])
        (shard_dir / f"{shard:03d}.bin").write_bytes(data)
        total += len(data)
        largest = max(largest, len(data))
    print(
        f"  {len(shards)} shards, {total / 1024 / 1024:.1f} MB total, "
        f"largest {largest / 1024:.0f} KB"
    )

    index = {
        "version": 1,
        "generated": city_month,
        "countryGenerated": country_month,
        "attribution": (
            "IP geolocation by DB-IP (https://db-ip.com), "
            "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)"
        ),
        "families": {
            "ip4": {
                "shardBits": formats.IP4_SHARD_BITS,
                "keyBits": formats.IP4_KEY_BITS,
                "startBytes": formats.IP4_START_BYTES,
                "recordBytes": formats.IP4_RECORD_BYTES,
                "dir": shard_rel,
                # The same shard on the fallback endpoint. A release asset's
                # name cannot contain a slash, so the path is flattened —
                # stated here rather than left for a client to know by
                # convention.
                "fallbackName": "ip4-{shard:03d}.bin",
                "present": sorted(shards),
            }
        },
        # Stated rather than left to be inferred from the absence of a family,
        # so a client can tell "no IPv6 shards exist" from "the index is
        # truncated" — and so the reason is somewhere a reader will find it.
        "ip6": {
            "shards": False,
            "reason": (
                "No sharding of the city data keeps IPv6 both private and "
                "small enough: see the README. IPv6 is answered at country "
                "level by country_v1.bin, which needs no request at all."
            ),
        },
    }
    index_out = args.out / "v1" / "index.json"
    index_out.write_text(
        json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    print(f"  wrote {index_out}")


if __name__ == "__main__":
    main()
