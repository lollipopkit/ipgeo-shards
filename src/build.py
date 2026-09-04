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
import gzip
import hashlib
import json
import shutil
from pathlib import Path

import dbip
import formats

#: DB-IP's code for "not known", which is not a place.
UNKNOWN_CC = "ZZ"

#: Null Island. Every geo dataset has rows there and none of them mean it; it
#: is what a missing coordinate looks like after being written to a CSV.
NULL_ISLAND = (0.0, 0.0)


# --------------------------------------------------------------------------
# Where a country is drawn
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The bundles: one file per family, city level in both.
# --------------------------------------------------------------------------


def city_records(city_csv: Path, family: int) -> list[formats.BundleRecord]:
    """City coordinates for one family, ready for `formats.build_bundle`.

    The same shape the per-/8 shards had, generalised: rows are collected into
    buckets, a range crossing a bucket boundary is split so every bucket is
    self-contained, and a bucket that has any record gets one at offset 0 —
    without it an address in the low part of a bucket falls below every row
    and has no answer at all, which is a different thing from having no data.

    IPv6 is keyed on its leading [formats.IP6_KEY_BITS] bits, so a /48 is the
    finest distinction the file can make. Rows that collapse onto one key are
    merged by the same rule as everything else here.

    Merging adjacent ranges that quantise to the same point removes about 1% —
    DB-IP has already merged everything sharing a city. It stays because the
    1% is free and because leaving it out would make the format's "no two
    consecutive records may be equal" rule untrue.
    """
    if family == formats.FAMILY_IP4:
        key_bits, bucket_bits, want_v6 = 32, formats.IP4_BUCKET_BITS, False
    elif family == formats.FAMILY_IP6:
        key_bits, bucket_bits, want_v6 = (
            formats.IP6_KEY_BITS,
            formats.IP6_BUCKET_BITS,
            True,
        )
    else:
        raise SystemExit(f"unknown family {family}")

    span_bits = key_bits - bucket_bits
    span = 1 << span_bits
    drop = 128 - key_bits if want_v6 else 0
    rows: dict[int, list[tuple[int, int, int, int]]] = collections.defaultdict(list)

    for row in dbip.rows(city_csv):
        if len(row) < 8:
            continue
        start, end, _continent, cc, _region, _city, lat, lon = row[:8]
        if (":" in start) != want_v6 or cc == UNKNOWN_CC:
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
        a >>= drop
        b >>= drop
        first, last = a >> span_bits, b >> span_bits
        for bucket in range(first, last + 1):
            base = bucket * span
            rows[bucket].append(
                (max(a, base) - base, min(b, base + span - 1) - base, la, lo)
            )

    out: list[formats.BundleRecord] = []
    for bucket in sorted(rows):
        base = bucket * span
        entries = sorted(rows[bucket])
        bucket_out: list[formats.BundleRecord] = []
        end = -1
        for a, b, la, lo in entries:
            if a <= end:
                # Overlapping source rows. The lower one already covers this
                # address; taking the later one would depend on sort order
                # among equals.
                end = max(end, b)
                continue
            if a > end + 1:
                bucket_out.append(
                    formats.BundleRecord(base + end + 1, formats.NO_DATA, 0)
                )
            elif bucket_out and (bucket_out[-1].lat, bucket_out[-1].lon) == (la, lo):
                end = b
                continue
            bucket_out.append(formats.BundleRecord(base + a, la, lo))
            end = b
        if bucket_out and bucket_out[0].start != base:
            bucket_out.insert(0, formats.BundleRecord(base, formats.NO_DATA, 0))
        out.extend(bucket_out)
    return out


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

    city_csv, city_month = dbip.fetch("city", args.cache)
    year, month = (int(part) for part in city_month.split("-"))

    # A fresh tree, not an overwrite. A build carries one month, and a file
    # left behind from the last one would be served alongside this one under a
    # name that says nothing about which it is.
    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)

    assets = []
    for family, name in ((formats.FAMILY_IP4, "ip4"), (formats.FAMILY_IP6, "ip6")):
        print(f"building the {name} city bundle")
        records = city_records(city_csv, family)
        blob = formats.build_bundle(family, records, year, month)
        # Published gzipped rather than left for the transport to compress.
        # Cloudflare's per-asset limit is 25 MiB and the raw IPv6 bundle is
        # 28.9, so this is what makes it fit at all — and it makes the
        # download a fixed number, which the app puts in front of someone
        # before they agree to it. `mtime=0` so a rebuild of unchanged data
        # produces identical bytes.
        packed = gzip.compress(blob, 9, mtime=0)
        path = args.out / f"{name}_city_v1.bin.gz"
        path.write_bytes(packed)
        assets.append(
            {
                "name": path.name,
                "family": 4 if family == formats.FAMILY_IP4 else 6,
                "records": len(records),
                "bytes": len(packed),
                "unpackedBytes": len(blob),
                "sha256": hashlib.sha256(packed).hexdigest(),
            }
        )
        print(
            f"  {len(records):,} records, {len(blob) / 1048576:.2f} MB raw, "
            f"{len(packed) / 1048576:.2f} MB packed"
        )

    manifest = {
        "version": 1,
        "generated": city_month,
        "attribution": (
            "IP geolocation by DB-IP (https://db-ip.com), "
            "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)"
        ),
        # Both sizes, because they answer different questions: `bytes` is what
        # someone is asked to agree to downloading, `unpackedBytes` is what it
        # then costs on their device. A prompt that quoted only the first would
        # understate the second by more than double.
        "assets": assets,
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    total = sum(a["bytes"] for a in assets)
    unpacked = sum(a["unpackedBytes"] for a in assets)
    print(
        f"  manifest.json — {total / 1048576:.2f} MB to download, "
        f"{unpacked / 1048576:.2f} MB on disk"
    )


if __name__ == "__main__":
    main()
