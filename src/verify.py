#!/usr/bin/env python3
"""Reads back what `build.py` produced, with a reader that is not the writer.

    python3 src/verify.py --out public

The point is to catch the failure mode this whole arrangement has: a file that
is subtly wrong is not an error anywhere. A client that cannot parse a bundle
places nothing and says the servers have no location; a client that reads a
record at the wrong offset draws a server in the wrong city. Neither says
anything is wrong. The build succeeding proves the writer ran, not that the
bytes mean what they claim.

So this walks the output the way the app does — a bucket table, then a binary
search over a fixed stride, straight on the bytes — and checks addresses whose
location is not in question. It is written from the format's documentation
rather than from `formats.py`, and it deliberately shares no code with the
writer: two implementations that import the same helper agree about a mistake
in the helper.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import struct
import sys
from pathlib import Path

MAGIC = b"SBGX"
NO_DATA = -32768
QUANTUM = 32767

#: Read out of the header rather than imported: a bundle that claims a family
#: this does not know is refused, and one that claims the *other* family is
#: read with the other stride and caught by the checks below.
LAYOUT = {
    # family: (key bits, bucket bits)
    4: (32, 8),
    6: (48, 16),
}

#: Addresses whose city is not in question, and the boundaries that have to
#: answer nothing. A gap is not a weaker answer than a coordinate — it is the
#: sentinel doing its job, and a build that lost it would place every private
#: address at whatever precedes it.
IP4_CASES = {
    "8.8.8.8": ("Mountain View", 37.42, -122.08),
    "1.1.1.1": ("Sydney", -33.87, 151.21),
    "9.9.9.9": ("Berkeley", 37.88, -122.27),
    "114.114.114.114": ("Jinan", 36.65, 117.12),
    "223.5.5.5": ("Hangzhou", 30.27, 120.16),
}
IP4_EMPTY = ["10.0.0.1", "127.0.0.1", "192.168.31.1", "172.16.0.1"]
IP6_CASES = {
    # Quad9. The one address this repository has always checked the IPv6 path
    # against, from when city-level v6 was first measured.
    "2620:fe::fe": ("Berkeley", 37.88, -122.27),
}
IP6_EMPTY = ["fd00::1", "fe80::1", "::1"]


class Bundle:
    """One family's file, read the way a client reads it."""

    def __init__(self, raw: bytes, name: str) -> None:
        self.name = name
        if len(raw) < 16:
            raise SystemExit(f"{name}: shorter than a header")
        magic, fmt, family, year, month, count = struct.unpack(">4sBBHBI3x", raw[:16])
        if magic != MAGIC:
            raise SystemExit(f"{name}: magic is {magic!r}, not {MAGIC!r}")
        if fmt != 1:
            raise SystemExit(f"{name}: format {fmt} is not 1")
        if family not in LAYOUT:
            raise SystemExit(f"{name}: family {family} is not one this reads")
        self.family = family
        self.when = (year, month)
        self.count = count
        self.key_bits, self.bucket_bits = LAYOUT[family]
        self.span_bits = self.key_bits - self.bucket_bits
        self.offset_bytes = self.span_bits // 8
        self.stride = self.offset_bytes + 4
        self.table_at = 16
        self.records_at = self.table_at + ((1 << self.bucket_bits) + 1) * 4
        wanted = self.records_at + count * self.stride
        if len(raw) != wanted:
            raise SystemExit(
                f"{name}: {len(raw)} bytes, but the header describes {wanted}"
            )
        self.raw = raw

    def bucket(self, i: int) -> int:
        at = self.table_at + i * 4
        return struct.unpack(">I", self.raw[at : at + 4])[0]

    def record(self, i: int) -> tuple[int, int, int]:
        at = self.records_at + i * self.stride
        start = int.from_bytes(self.raw[at : at + self.offset_bytes], "big")
        lat, lon = struct.unpack(">hh", self.raw[at + self.offset_bytes : at + self.stride])
        return start, lat, lon

    def key(self, address: str) -> int:
        a = ipaddress.ip_address(address)
        if (a.version == 6) != (self.family == 6):
            raise SystemExit(f"{self.name}: {address} is the wrong family for it")
        value = int(a)
        return value >> (128 - self.key_bits) if a.version == 6 else value

    def look(self, address: str) -> tuple[float, float] | str | None:
        """A coordinate, `"gap"`, or None when the bucket holds nothing."""
        k = self.key(address)
        b = k >> self.span_bits
        lo, hi = self.bucket(b), self.bucket(b + 1)
        if lo == hi:
            return None
        offset = k & ((1 << self.span_bits) - 1)
        found = -1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.record(mid)[0] <= offset:
                found, lo = mid, mid + 1
            else:
                hi = mid
        if found < 0:
            # Impossible if every non-empty bucket starts at offset 0, which is
            # checked below — so reaching here means that invariant broke.
            raise SystemExit(f"{self.name}: {address} is below its bucket's first record")
        _, lat, lon = self.record(found)
        if lat == NO_DATA:
            return "gap"
        return lat / QUANTUM * 90, lon / QUANTUM * 180

    def check_structure(self) -> None:
        """The invariants a lookup relies on and cannot notice the absence of."""
        buckets = 1 << self.bucket_bits
        if self.bucket(0) != 0:
            raise SystemExit(f"{self.name}: the first bucket does not start at 0")
        if self.bucket(buckets) != self.count:
            raise SystemExit(f"{self.name}: the table does not close at the record count")
        span = 1 << self.span_bits
        previous_end = 0
        for b in range(buckets):
            lo, hi = self.bucket(b), self.bucket(b + 1)
            if lo != previous_end or hi < lo:
                raise SystemExit(f"{self.name}: bucket {b} is {lo}..{hi}, out of order")
            previous_end = hi
            if lo == hi:
                continue
            first = self.record(lo)[0]
            if first != 0:
                # Without this an address below the first record has no answer
                # at all, which a client cannot tell from "no data here".
                raise SystemExit(f"{self.name}: bucket {b} starts at {first}, not 0")
            last = -1
            for i in range(lo, hi):
                start, lat, _ = self.record(i)
                if start <= last:
                    raise SystemExit(f"{self.name}: bucket {b} is not sorted at {i}")
                if start >= span:
                    raise SystemExit(f"{self.name}: offset {start} escapes bucket {b}")
                if not (NO_DATA <= lat <= QUANTUM):
                    raise SystemExit(f"{self.name}: latitude {lat} at {i} is not storable")
                last = start


def check(bundle: Bundle, cases: dict, empty: list[str]) -> None:
    for address, (where, lat, lon) in cases.items():
        got = bundle.look(address)
        if not isinstance(got, tuple):
            raise SystemExit(f"{address} should be {where}, and answered {got}")
        if abs(got[0] - lat) > 0.5 or abs(got[1] - lon) > 0.5:
            raise SystemExit(
                f"{address} should be {where} ({lat}, {lon}), "
                f"and answered {got[0]:.2f}, {got[1]:.2f}"
            )
        print(f"    {address} -> {got[0]:.4f}, {got[1]:.4f}  ({where})")
    for address in empty:
        got = bundle.look(address)
        if isinstance(got, tuple):
            raise SystemExit(
                f"{address} is not public space and answered {got} — the "
                "gap sentinel is missing, so every address in that block "
                "inherits whatever precedes it"
            )
        print(f"    {address} -> {got}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("public"))
    args = parser.parse_args()

    manifest = json.loads((args.out / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise SystemExit(f"manifest version {manifest.get('version')} is not 1")
    year, month = (int(p) for p in manifest["generated"].split("-"))

    seen = set()
    for asset in manifest["assets"]:
        path = args.out / asset["name"]
        packed = path.read_bytes()
        print(f"  {asset['name']} — {len(packed) / 1048576:.2f} MB")

        # The manifest is what the app trusts before it has the file, so every
        # number in it is checked against the file rather than assumed.
        if len(packed) != asset["bytes"]:
            raise SystemExit(f"{path}: {len(packed)} bytes, manifest says {asset['bytes']}")
        digest = hashlib.sha256(packed).hexdigest()
        if digest != asset["sha256"]:
            raise SystemExit(f"{path}: sha256 {digest}, manifest says {asset['sha256']}")
        # Cloudflare refuses an asset over 25 MiB, and it is the primary. A
        # build that crossed it would upload cleanly and 404 for everyone.
        if len(packed) > 25 * 1024 * 1024:
            raise SystemExit(f"{path}: over Cloudflare's 25 MiB per-asset limit")

        raw = gzip.decompress(packed)
        if len(raw) != asset["unpackedBytes"]:
            raise SystemExit(f"{path}: unpacks to {len(raw)}, manifest says {asset['unpackedBytes']}")

        bundle = Bundle(raw, asset["name"])
        if bundle.family != asset["family"]:
            raise SystemExit(f"{path}: header says family {bundle.family}, manifest {asset['family']}")
        if bundle.when != (year, month):
            raise SystemExit(f"{path}: header says {bundle.when}, manifest {(year, month)}")
        if bundle.count != asset["records"]:
            raise SystemExit(f"{path}: {bundle.count} records, manifest says {asset['records']}")
        seen.add(bundle.family)

        print("    checking the bucket table and every record")
        bundle.check_structure()
        if bundle.family == 4:
            check(bundle, IP4_CASES, IP4_EMPTY)
        else:
            check(bundle, IP6_CASES, IP6_EMPTY)

    if seen != {4, 6}:
        raise SystemExit(f"the manifest lists families {sorted(seen)}, not both")
    print("  ok")


if __name__ == "__main__":
    sys.exit(main())
