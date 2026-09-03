"""Tests for the two published formats, and for the vectors both sides share.

    python3 -m unittest discover -s tests

Standard library only, like everything else here. There is no build to install
and nothing to pin.
"""

from __future__ import annotations

import ipaddress
import json
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import build  # noqa: E402
import formats  # noqa: E402
from formats import CountryRange, ShardRecord  # noqa: E402

VECTORS = Path(__file__).resolve().parent / "vectors"


def v4(ip: str) -> int:
    return int(ipaddress.IPv4Address(ip))


def v6key(ip: str) -> int:
    return int(ipaddress.IPv6Address(ip)) >> (128 - formats.IP6_KEY_BITS)


class Quantise(unittest.TestCase):
    def test_round_trips_within_the_stated_resolution(self):
        for lat, lon in ((0, 0), (39.9042, 116.4074), (-35.28, 149.13), (90, 180)):
            la, lo = formats.quantise(lat, lon)
            self.assertAlmostEqual(la / formats.QUANTUM * 90, lat, delta=0.003)
            self.assertAlmostEqual(lo / formats.QUANTUM * 180, lon, delta=0.006)

    def test_never_produces_the_sentinel(self):
        # -32768 means "no data". The quantisation must not be able to reach it
        # or a real coordinate at the south pole would read as a gap.
        la, _ = formats.quantise(-90, 0)
        self.assertEqual(la, -formats.QUANTUM)
        self.assertNotEqual(la, formats.NO_DATA)

    def test_clamps_rather_than_overflowing(self):
        # A bad row in the source must not encode to a value that decodes
        # somewhere else entirely.
        la, lo = formats.quantise(1000, -1000)
        self.assertEqual((la, lo), (formats.QUANTUM, -formats.QUANTUM))


class CountryDb(unittest.TestCase):
    coords = {"CN": (39.9042, 116.4074), "US": (38.9072, -77.0369)}

    def test_header_and_lengths(self):
        blob = formats.build_country_db(
            self.coords,
            [CountryRange(v4("1.0.0.0"), "CN")],
            [CountryRange(v6key("2600::"), "US")],
        )
        self.assertEqual(blob[:4], formats.COUNTRY_MAGIC)
        self.assertEqual(blob[4], formats.COUNTRY_FORMAT)
        self.assertEqual(struct.unpack_from(">H", blob, 5)[0], 2)
        expected = 7 + 2 * 6 + 4 + 1 * 5 + 4 + 1 * (formats.IP6_START_BYTES + 1)
        self.assertEqual(len(blob), expected)

    def test_countries_are_sorted_so_the_output_is_stable(self):
        # The index is an offset into this table, so the order has to be a
        # function of the input and not of dictionary insertion.
        a = formats.build_country_db({"US": (1, 2), "CN": (3, 4)}, [], [])
        b = formats.build_country_db({"CN": (3, 4), "US": (1, 2)}, [], [])
        self.assertEqual(a, b)
        self.assertEqual(a[7:9], b"CN")

    def test_a_gap_is_the_sentinel_index(self):
        blob = formats.build_country_db(
            self.coords, [CountryRange(0, None)], []
        )
        # Past the header, the two-country table, the IPv4 count and the
        # record's own 4-byte start. Not `blob[-1]`: with no IPv6 records the
        # last four bytes are the zero count for that family.
        at = 7 + 2 * 6 + 4 + 4
        self.assertEqual(blob[at], formats.NO_COUNTRY)

    def test_refuses_more_countries_than_the_index_can_name(self):
        too_many = {f"{i // 26 + 65:c}{i % 26 + 65:c}": (0, 0) for i in range(300)}
        with self.assertRaises(SystemExit):
            formats.build_country_db(too_many, [], [])

    def test_refuses_ranges_out_of_order(self):
        # The file is a sorted array a binary search runs on directly. Out of
        # order it is not wrong at parse, it is wrong at one lookup in ten.
        with self.assertRaises(SystemExit):
            formats.build_country_db(
                self.coords,
                [CountryRange(100, "CN"), CountryRange(50, "US")],
                [],
            )


class Shards(unittest.TestCase):
    def test_stride_and_contents(self):
        data = formats.build_ip4_shard(
            [ShardRecord(0, 100, -200), ShardRecord(1024, formats.NO_DATA, 0)]
        )
        self.assertEqual(len(data), 2 * formats.IP4_RECORD_BYTES)
        self.assertEqual(int.from_bytes(data[0:3], "big"), 0)
        self.assertEqual(struct.unpack_from(">hh", data, 3), (100, -200))
        self.assertEqual(int.from_bytes(data[7:10], "big"), 1024)
        self.assertEqual(struct.unpack_from(">h", data, 10)[0], formats.NO_DATA)

    def test_must_start_at_zero(self):
        # A lookup takes the last record at or below the key, so a shard whose
        # first record is not at zero has addresses with no answer at all —
        # which reads the same as a gap and is not.
        with self.assertRaises(SystemExit):
            formats.build_ip4_shard([ShardRecord(16, 1, 2)])

    def test_refuses_records_out_of_order(self):
        with self.assertRaises(SystemExit):
            formats.build_ip4_shard(
                [ShardRecord(0, 1, 2), ShardRecord(0, 3, 4)]
            )

    def test_empty_is_empty(self):
        self.assertEqual(formats.build_ip4_shard([]), b"")


class Collapse(unittest.TestCase):
    """`build._collapse`, which is where the three silent mistakes live."""

    def test_a_hole_becomes_a_gap_record(self):
        out = build._collapse([(0, 9, "CN"), (20, 29, "US")])
        self.assertEqual(
            out,
            [CountryRange(0, "CN"), CountryRange(10, None), CountryRange(20, "US")],
        )

    def test_adjacent_ranges_of_one_country_merge(self):
        out = build._collapse([(0, 9, "CN"), (10, 19, "CN")])
        self.assertEqual(out, [CountryRange(0, "CN")])

    def test_an_unknown_country_is_a_gap_not_an_index(self):
        out = build._collapse([(0, 9, None), (10, 19, "US")])
        self.assertEqual(out, [CountryRange(0, None), CountryRange(10, "US")])

    def test_duplicate_keys_keep_the_lower(self):
        # IPv6 keyed at /48 puts several source ranges on one value. Taking the
        # later would depend on sort order among equals.
        out = build._collapse([(5, 5, "CN"), (5, 5, "US")])
        self.assertEqual(out, [CountryRange(5, "CN")])

    def test_output_is_sorted_whatever_the_input_order(self):
        forwards = build._collapse([(0, 9, "CN"), (20, 29, "US")])
        backwards = build._collapse([(20, 29, "US"), (0, 9, "CN")])
        self.assertEqual(forwards, backwards)


class SharedVectors(unittest.TestCase):
    """The vectors, checked with a reader written here rather than the writer.

    A second reader, deliberately: `make_vectors.py` produces the bytes with
    the writer, so checking them with the writer proves nothing. This walks the
    file the way a client does — binary search over a fixed stride — which is
    also the thing ServerBox's `GeoCountryDb` does in Dart.
    """

    @classmethod
    def setUpClass(cls):
        cls.blob = (VECTORS / "country_v1.bin").read_bytes()
        cls.spec = json.loads((VECTORS / "country_v1.json").read_text())

    def _lookup(self, ip: str) -> dict[str, float] | None:
        blob = self.blob
        count = struct.unpack_from(">H", blob, 5)[0]
        at = 7 + count * 6
        ip4_count = struct.unpack_from(">I", blob, at)[0]
        at += 4
        ip4_at = at
        at += ip4_count * 5
        ip6_count = struct.unpack_from(">I", blob, at)[0]
        at += 4
        ip6_at = at

        address = ipaddress.ip_address(ip)
        if address.version == 4:
            base, n, stride, start_bytes = ip4_at, ip4_count, 5, 4
            key = int(address)
        else:
            base, n, stride = ip6_at, ip6_count, formats.IP6_START_BYTES + 1
            start_bytes = formats.IP6_START_BYTES
            key = int(address) >> (128 - formats.IP6_KEY_BITS)

        lo, hi, found = 0, n - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            offset = base + mid * stride
            start = int.from_bytes(blob[offset : offset + start_bytes], "big")
            if start <= key:
                found, lo = mid, mid + 1
            else:
                hi = mid - 1
        if found < 0:
            return None
        index = blob[base + found * stride + stride - 1]
        if index == formats.NO_COUNTRY or index >= count:
            return None
        entry = 7 + index * 6 + 2
        lat, lon = struct.unpack_from(">hh", blob, entry)
        return {
            "lat": lat / formats.QUANTUM * 90,
            "lon": lon / formats.QUANTUM * 180,
        }

    def test_the_vectors_are_current(self):
        self.assertEqual(self.spec["format"], formats.COUNTRY_FORMAT)
        self.assertEqual(self.spec["ip6KeyBits"], formats.IP6_KEY_BITS)

    def test_every_lookup_answers_what_the_spec_says(self):
        for case in self.spec["lookups"]:
            with self.subTest(ip=case["ip"]):
                got = self._lookup(case["ip"])
                if case["expect"] is None:
                    self.assertIsNone(got)
                    continue
                self.assertIsNotNone(got)
                self.assertAlmostEqual(got["lat"], case["expect"]["lat"], places=6)
                self.assertAlmostEqual(got["lon"], case["expect"]["lon"], places=6)


if __name__ == "__main__":
    unittest.main()
