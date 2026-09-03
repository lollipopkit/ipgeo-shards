#!/usr/bin/env python3
"""Reads back what `build.py` produced, with a reader that is not the writer.

    python3 src/verify.py --out public

The point is to catch the failure mode this whole arrangement has: a file that
is subtly wrong is not an error anywhere. A client that cannot parse a shard
falls back to the country database, a client that reads a record at the wrong
offset draws a server in the wrong city, and neither says anything. The build
succeeding proves the writer ran, not that the bytes mean what they claim.

So this walks the output the way the app does — binary search over a fixed
stride, straight on the bytes — and checks addresses whose location is not in
question.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import struct
from pathlib import Path

import formats

#: Addresses whose country nobody disputes, and roughly where that is.
#:
#: A bounding box rather than a point: the country coordinate is one city and
#: which city it is moves with the month's data, so asserting a city would make
#: this fail for the wrong reason. What is being checked is that the file is
#: read correctly, not what DB-IP thinks.
COUNTRY_CASES = [
    # ip, name, lat range, lon range
    ("8.8.8.8", "Google DNS, US", (24, 50), (-126, -66)),
    ("9.9.9.9", "Quad9, US", (24, 50), (-126, -66)),
    ("1.1.1.1", "Cloudflare DNS, AU", (-44, -10), (112, 154)),
    ("114.114.114.114", "114DNS, CN", (18, 54), (73, 135)),
    # North America, not "US". DB-IP places both of these /48s in Canada: an
    # IPv6 block is registered once, to the registrant's regional office,
    # while the address itself is anycast from everywhere. That is a limit of
    # the data and not of this reader, so the box is the continent.
    ("2001:4860:4860::8888", "Google DNS v6, North America", (24, 60), (-126, -60)),
    ("2606:4700:4700::1111", "Cloudflare DNS v6, North America", (24, 60), (-126, -60)),
]

#: Nothing should place these. They are not routable, so a database that has an
#: answer for one has something wrong with it.
UNROUTABLE = ["10.0.0.1", "192.168.1.1", "127.0.0.1", "169.254.1.1"]


class CountryReader:
    """The bundled database, parsed the way a client parses it."""

    def __init__(self, blob: bytes) -> None:
        if blob[:4] != formats.COUNTRY_MAGIC:
            raise SystemExit("country: wrong magic")
        if blob[4] != formats.COUNTRY_FORMAT:
            raise SystemExit(f"country: format {blob[4]}, expected {formats.COUNTRY_FORMAT}")
        self.blob = blob
        self.count = struct.unpack_from(">H", blob, 5)[0]
        at = 7 + self.count * 6
        self.ip4_count = struct.unpack_from(">I", blob, at)[0]
        at += 4
        self.ip4_at = at
        at += self.ip4_count * 5
        self.ip6_count = struct.unpack_from(">I", blob, at)[0]
        at += 4
        self.ip6_at = at
        at += self.ip6_count * (formats.IP6_START_BYTES + 1)
        if at != len(blob):
            raise SystemExit(
                f"country: {len(blob) - at} bytes past the end of the records"
            )

    def lookup(self, ip: str) -> tuple[float, float] | None:
        address = ipaddress.ip_address(ip)
        if address.version == 4:
            base, n, stride, width = self.ip4_at, self.ip4_count, 5, 4
            key = int(address)
        else:
            width = formats.IP6_START_BYTES
            base, n, stride = self.ip6_at, self.ip6_count, width + 1
            key = int(address) >> (128 - formats.IP6_KEY_BITS)

        lo, hi, found = 0, n - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            offset = base + mid * stride
            if int.from_bytes(self.blob[offset : offset + width], "big") <= key:
                found, lo = mid, mid + 1
            else:
                hi = mid - 1
        if found < 0:
            return None
        index = self.blob[base + found * stride + stride - 1]
        if index == formats.NO_COUNTRY or index >= self.count:
            return None
        entry = 7 + index * 6 + 2
        lat, lon = struct.unpack_from(">hh", self.blob, entry)
        return lat / formats.QUANTUM * 90, lon / formats.QUANTUM * 180

    def check_ordering(self) -> None:
        """Every record's start is above the one before it.

        The one invariant a binary search cannot survive being wrong about,
        and the one that produces a plausible answer when it is: an unsorted
        array does not fail to search, it searches to the wrong place.
        """
        for name, base, n, stride, width in (
            ("ip4", self.ip4_at, self.ip4_count, 5, 4),
            (
                "ip6",
                self.ip6_at,
                self.ip6_count,
                formats.IP6_START_BYTES + 1,
                formats.IP6_START_BYTES,
            ),
        ):
            previous = -1
            for i in range(n):
                offset = base + i * stride
                start = int.from_bytes(self.blob[offset : offset + width], "big")
                if start <= previous:
                    raise SystemExit(f"country/{name}: record {i} is out of order")
                previous = start


def check_shards(out: Path, index: dict) -> None:
    family = index["families"]["ip4"]
    stride = family["recordBytes"]
    width = family["startBytes"]
    span = 1 << (family["keyBits"] - family["shardBits"])
    present = family["present"]

    listed = {int(p.stem) for p in (out / family["dir"]).glob("*.bin")}
    if listed != set(present):
        missing = sorted(set(present) - listed)
        extra = sorted(listed - set(present))
        raise SystemExit(
            f"shards: index and directory disagree (missing {missing}, extra {extra})"
        )

    for shard in present:
        path = out / family["dir"] / f"{shard:03d}.bin"
        data = path.read_bytes()
        if len(data) % stride:
            raise SystemExit(f"{path}: {len(data)} bytes is not a whole number of records")
        n = len(data) // stride
        if n == 0:
            raise SystemExit(f"{path}: listed as present but empty")
        previous = -1
        for i in range(n):
            offset = i * stride
            start = int.from_bytes(data[offset : offset + width], "big")
            if start <= previous:
                raise SystemExit(f"{path}: record {i} is out of order")
            if start >= span:
                raise SystemExit(f"{path}: record {i} starts outside its own shard")
            previous = start
        if int.from_bytes(data[:width], "big") != 0:
            raise SystemExit(f"{path}: does not start at offset 0")
    print(f"  {len(present)} shards, all sorted and self-contained")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("public"))
    args = parser.parse_args()

    print("country database")
    reader = CountryReader((args.out / "country_v1.bin").read_bytes())
    print(f"  {reader.count} countries, {reader.ip4_count:,} v4, {reader.ip6_count:,} v6")
    reader.check_ordering()
    print("  records are sorted")

    failures = 0
    for ip, name, lats, lons in COUNTRY_CASES:
        got = reader.lookup(ip)
        if got is None:
            print(f"  FAIL {name} ({ip}): no answer")
            failures += 1
            continue
        lat, lon = got
        ok = lats[0] <= lat <= lats[1] and lons[0] <= lon <= lons[1]
        print(f"  {'ok  ' if ok else 'FAIL'} {name} ({ip}): {lat:.3f}, {lon:.3f}")
        failures += not ok

    for ip in UNROUTABLE:
        got = reader.lookup(ip)
        if got is not None:
            print(f"  FAIL {ip} is not routable but was placed at {got}")
            failures += 1
    print(f"  {len(UNROUTABLE)} unroutable addresses are unplaced")

    print("shards")
    index = json.loads((args.out / "v1" / "index.json").read_text())
    if index["version"] != 1:
        raise SystemExit(f"index version {index['version']}")
    check_shards(args.out, index)

    if failures:
        raise SystemExit(f"{failures} check(s) failed")
    print("all checks passed")


if __name__ == "__main__":
    main()
