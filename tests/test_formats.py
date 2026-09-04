"""Tests for the two published formats, and for the vectors both sides share.

    python3 -m unittest discover -s tests

Standard library only, like everything else here. There is no build to install
and nothing to pin.
"""

from __future__ import annotations

import contextlib
import io
import ipaddress
import json
import shutil
import struct
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import build  # noqa: E402
import dbip  # noqa: E402
import formats  # noqa: E402
import verify  # noqa: E402

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


class Bundle(unittest.TestCase):
    """The published format, checked through a reader that is not the writer.

    `src/verify.py` is that reader — it is written from the documentation and
    shares no code with `formats.py`, so a mistake in a helper cannot make
    both sides agree about it.
    """

    def build(self, family, records, year=2026, month=9):
        return formats.build_bundle(family, records, year, month)

    def read(self, raw, name="test"):
        return verify.Bundle(raw, name)

    def test_header_says_what_the_file_is(self):
        raw = self.build(
            formats.FAMILY_IP4, [formats.BundleRecord(0x08000000, 100, 200)]
        )
        b = self.read(raw)
        self.assertEqual(b.family, 4)
        self.assertEqual(b.when, (2026, 9))
        self.assertEqual(b.count, 1)
        # Padded to 16 so the u32 table that follows starts aligned.
        self.assertEqual(formats.BUNDLE_HEADER_BYTES, 16)

    def test_a_lookup_takes_the_last_record_at_or_below_the_key(self):
        raw = self.build(
            formats.FAMILY_IP4,
            [
                formats.BundleRecord(0x08000000, *formats.quantise(10, 20)),
                formats.BundleRecord(0x08080808, *formats.quantise(37.42, -122.08)),
            ],
        )
        b = self.read(raw)
        self.assertAlmostEqual(b.look("8.8.8.8")[0], 37.42, places=2)
        # Past the second record and still covered by it: a bundle is ranges,
        # not points.
        self.assertAlmostEqual(b.look("8.9.9.9")[0], 37.42, places=2)
        self.assertAlmostEqual(b.look("8.0.0.1")[0], 10, places=2)

    def test_a_gap_is_the_sentinel_latitude(self):
        raw = self.build(
            formats.FAMILY_IP4,
            [
                formats.BundleRecord(0x08000000, *formats.quantise(10, 20)),
                formats.BundleRecord(0x08010000, formats.NO_DATA, 0),
            ],
        )
        self.assertEqual(self.read(raw).look("8.1.2.3"), "gap")

    def test_a_bucket_with_no_records_answers_nothing(self):
        """Which is what replaced the index's `present` set."""
        raw = self.build(
            formats.FAMILY_IP4, [formats.BundleRecord(0x08000000, 100, 200)]
        )
        self.assertIsNone(self.read(raw).look("9.9.9.9"))

    def test_ipv6_is_keyed_on_its_leading_bits(self):
        # Two addresses inside one /48 are one key, so they answer alike.
        raw = self.build(
            formats.FAMILY_IP6,
            [
                # The bucket has to open at its own first key; see the writer.
                formats.BundleRecord(0x2620_0000_0000, formats.NO_DATA, 0),
                formats.BundleRecord(
                    0x2620_00FE_0000, *formats.quantise(37.88, -122.27)
                ),
            ],
        )
        b = self.read(raw)
        self.assertAlmostEqual(b.look("2620:fe::fe")[0], 37.88, places=2)
        self.assertAlmostEqual(b.look("2620:fe::1:2:3")[0], 37.88, places=2)

    def test_refuses_a_bucket_that_does_not_start_at_zero(self):
        # An address below the first record of its bucket would otherwise have
        # no answer at all, which a client cannot tell from "no data here".
        with self.assertRaises(SystemExit):
            self.build(formats.FAMILY_IP4, [formats.BundleRecord(0x08000001, 1, 2)])

    def test_refuses_records_out_of_order(self):
        with self.assertRaises(SystemExit):
            self.build(
                formats.FAMILY_IP4,
                [
                    formats.BundleRecord(0x08000000, 1, 2),
                    formats.BundleRecord(0x08000000, 3, 4),
                ],
            )

    def test_refuses_an_unknown_family(self):
        with self.assertRaises(SystemExit):
            self.build(5, [])

    def test_a_renamed_file_is_refused_rather_than_misread(self):
        """The family is in the header, not the filename.

        Read with the other family's stride every record would decode to
        something plausible, which is the failure this format has to make
        impossible rather than unlikely.
        """
        raw = self.build(formats.FAMILY_IP6, [formats.BundleRecord(0, 1, 2)])
        b = self.read(raw)
        with self.assertRaises(SystemExit):
            b.look("8.8.8.8")

    def test_the_shared_vectors_read_back(self):
        """The contract with ServerBox, which has its own reader.

        The same two files are checked into that repository. A reader tested
        only against bytes its own writer produced proves nothing about the
        other side.
        """
        for name in ("bundle_ip4_v1", "bundle_ip6_v1"):
            with self.subTest(name):
                raw = (VECTORS / f"{name}.bin").read_bytes()
                expected = json.loads((VECTORS / f"{name}.json").read_text())
                b = self.read(raw, name)
                b.check_structure()
                self.assertEqual(b.family, expected["family"])
                self.assertEqual(b.count, expected["records"])
                for address, want in expected["lookups"].items():
                    got = b.look(address)
                    if want is None:
                        self.assertIsNone(got, address)
                    elif want == "gap":
                        self.assertEqual(got, "gap", address)
                    else:
                        # The quantum is 90/32767 of latitude, about 0.003
                        # degrees — tighter than that is testing rounding.
                        self.assertAlmostEqual(got[0], want[0], delta=0.005, msg=address)
                        self.assertAlmostEqual(got[1], want[1], delta=0.01, msg=address)


class Fetch(unittest.TestCase):
    """The download, which is the one path a monthly build cannot do without.

    It had no test and it was broken: DB-IP answers 403 to Python's default
    `Python-urllib` User-Agent, and every local run had been served from a
    cache filled by hand — so the first time it ran for real was in CI.

    The network is stubbed. What is worth pinning is the request this builds
    and the walk back through the months, not whether DB-IP is up.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.requests: list[urllib.request.Request] = []
        self.real = urllib.request.urlopen
        urllib.request.urlopen = self._fake
        self.addCleanup(setattr, urllib.request, "urlopen", self.real)
        #: month -> body, or an HTTP status to raise
        self.answers: dict[str, object] = {}

    def _fake(self, request, timeout=None):
        self.requests.append(request)
        for month, answer in self.answers.items():
            if month in request.full_url:
                if isinstance(answer, int):
                    raise urllib.error.HTTPError(
                        request.full_url, answer, "no", {}, None
                    )
                return contextlib.closing(io.BytesIO(answer))
        raise urllib.error.HTTPError(request.full_url, 404, "no", {}, None)

    def test_sends_a_user_agent_that_is_not_the_default(self):
        # The whole bug. Any value is accepted; the default is not.
        self.answers["2026-09"] = b"body"
        dbip.fetch("country", self.tmp, today=date(2026, 9, 4))
        agent = self.requests[0].get_header("User-agent")
        self.assertIsNotNone(agent)
        self.assertNotIn("Python-urllib", agent)
        self.assertIn("ipgeo-shards", agent)

    def test_asks_for_this_month_first(self):
        self.answers["2026-09"] = b"body"
        path, month = dbip.fetch("city", self.tmp, today=date(2026, 9, 4))
        self.assertEqual(month, "2026-09")
        self.assertEqual(path.read_bytes(), b"body")
        self.assertEqual(len(self.requests), 1)

    def test_walks_back_when_the_month_is_not_published(self):
        # The first of the month, before DB-IP has published.
        self.answers["2026-08"] = b"august"
        path, month = dbip.fetch("country", self.tmp, today=date(2026, 9, 1))
        self.assertEqual(month, "2026-08")
        self.assertEqual(path.read_bytes(), b"august")

    def test_walks_back_across_a_year(self):
        self.answers["2025-12"] = b"december"
        _path, month = dbip.fetch("country", self.tmp, today=date(2026, 1, 2))
        self.assertEqual(month, "2025-12")

    def test_gives_up_after_the_stated_number_of_months(self):
        with self.assertRaises(SystemExit):
            dbip.fetch("country", self.tmp, today=date(2026, 9, 4))
        self.assertEqual(len(self.requests), dbip.MAX_MONTHS_BACK + 1)

    def test_a_403_is_raised_rather_than_walked_past(self):
        # Only a 404 means "not published yet". Treating a 403 as one would
        # walk back three months and then report the wrong thing entirely —
        # which is what would have hidden this bug for another month.
        self.answers["2026-09"] = 403
        with self.assertRaises(urllib.error.HTTPError):
            dbip.fetch("country", self.tmp, today=date(2026, 9, 4))

    def test_a_cached_file_is_not_downloaded_again(self):
        (self.tmp / "dbip-country-lite-2026-09.csv.gz").write_bytes(b"cached")
        path, month = dbip.fetch("country", self.tmp, today=date(2026, 9, 4))
        self.assertEqual(month, "2026-09")
        self.assertEqual(path.read_bytes(), b"cached")
        self.assertEqual(self.requests, [])

    def test_an_empty_cached_file_is_ignored(self):
        # What an interrupted run leaves if the rename is skipped. Taking it
        # for a hit would build the month from nothing.
        (self.tmp / "dbip-country-lite-2026-09.csv.gz").write_bytes(b"")
        self.answers["2026-09"] = b"body"
        path, _month = dbip.fetch("country", self.tmp, today=date(2026, 9, 4))
        self.assertEqual(path.read_bytes(), b"body")

    def test_nothing_partial_is_left_behind(self):
        self.answers["2026-09"] = b"body"
        dbip.fetch("country", self.tmp, today=date(2026, 9, 4))
        self.assertEqual(list(self.tmp.glob("*.partial")), [])


if __name__ == "__main__":
    unittest.main()
