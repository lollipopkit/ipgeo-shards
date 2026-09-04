#!/usr/bin/env python3
"""Writes the vectors both repositories check their readers against.

    python3 tests/make_vectors.py

Small and hand-built rather than a slice of the real export: every case here
is one a reader can get wrong on its own — the first record of a bucket, an
empty bucket, a gap, the last record running to the end of its bucket, and for
IPv6 two addresses that collapse onto one key.

The `.json` beside each `.bin` says what a reader must answer, so the other
implementation does not have to be trusted to describe its own input.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import formats  # noqa: E402

OUT = Path(__file__).resolve().parent / "vectors"


def main() -> None:
    OUT.mkdir(exist_ok=True)

    ip4 = [
        formats.BundleRecord(0x08000000, *formats.quantise(10.5, 20.25)),
        formats.BundleRecord(0x08080808, *formats.quantise(37.4233, -122.0838)),
        formats.BundleRecord(0x08090000, formats.NO_DATA, 0),
        # A second bucket, so the table is exercised rather than assumed.
        formats.BundleRecord(0x0A000000, *formats.quantise(-33.8691, 151.2094)),
    ]
    # Each bucket opens with a record at its own first key. The real builder
    # inserts these; here they are written out, because a bucket whose first
    # record is not at offset 0 leaves every address below it with no answer
    # at all — which a reader cannot tell from "no data here".
    ip6 = [
        formats.BundleRecord(0x2606_0000_0000, *formats.quantise(51.5072, -0.1276)),
        formats.BundleRecord(0x2620_0000_0000, formats.NO_DATA, 0),
        formats.BundleRecord(0x2620_00FE_0000, *formats.quantise(37.8793, -122.2706)),
        formats.BundleRecord(0x2620_00FE_0001, formats.NO_DATA, 0),
    ]

    write(
        "bundle_ip4_v1",
        formats.FAMILY_IP4,
        ip4,
        {
            "8.0.0.0": [10.5, 20.25],
            "8.0.0.1": [10.5, 20.25],
            "8.8.8.8": [37.4233, -122.0838],
            "8.8.255.255": [37.4233, -122.0838],
            "8.9.0.0": "gap",
            "8.255.255.255": "gap",
            "10.0.0.1": [-33.8691, 151.2094],
            "9.9.9.9": None,
            "255.255.255.255": None,
        },
    )
    write(
        "bundle_ip6_v1",
        formats.FAMILY_IP6,
        ip6,
        {
            "2620:fe::fe": [37.8793, -122.2706],
            # A different address in the same /48, so it is the same key.
            "2620:fe::1:2:3": [37.8793, -122.2706],
            "2620:fe:1::1": "gap",
            "2620::1": "gap",
            "2606::1": [51.5072, -0.1276],
            "2001:db8::1": None,
        },
    )


def write(name: str, family: int, records: list, lookups: dict) -> None:
    blob = formats.build_bundle(family, records, 2026, 9)
    (OUT / f"{name}.bin").write_bytes(blob)
    (OUT / f"{name}.json").write_text(
        json.dumps(
            {
                "family": 4 if family == formats.FAMILY_IP4 else 6,
                "generated": "2026-09",
                "records": len(records),
                "bytes": len(blob),
                "lookups": lookups,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"{name}: {len(records)} records, {len(blob)} bytes")


if __name__ == "__main__":
    main()
