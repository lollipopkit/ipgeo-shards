"""The two binary formats this repository publishes.

Both are read by ServerBox — the country database by `GeoCountryDb` in
`lib/core/service/geo_db.dart`, the shards by its shard client. **There are two
implementations of each format and nothing makes them agree except the test
vectors in `tests/vectors/`**, which are checked into both repositories. A
reader tested only against bytes its own writer produced agrees with itself and
proves nothing.

Big-endian throughout, because a format read by a binary search wants a fixed
stride and no endianness question at the call site.

Coordinates are a signed 16-bit fraction of their own axis, so latitude
resolves to about 300 m and longitude to about 600 m at the equator. Finer than
the source data, which is a city centroid.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

QUANTUM = 32767

#: Latitude that means "nothing is allocated here".
#:
#: -32768 rather than a separate flag byte: it is the one value the
#: quantisation above cannot produce, since -90 degrees encodes to -32767.
NO_DATA = -32768


def quantise(lat: float, lon: float) -> tuple[int, int]:
    """Degrees to the pair stored in both formats.

    Clamped rather than trusted. A coordinate outside the globe is a bad row in
    the source, and letting it through would produce a value that decodes to
    somewhere else entirely — or, at -32768 latitude, to the sentinel.
    """
    la = max(-QUANTUM, min(QUANTUM, round(lat / 90 * QUANTUM)))
    lo = max(-QUANTUM, min(QUANTUM, round(lon / 180 * QUANTUM)))
    return la, lo


# --------------------------------------------------------------------------
# The country database, bundled inside the app
# --------------------------------------------------------------------------

COUNTRY_MAGIC = b"SBGC"
COUNTRY_FORMAT = 1

#: How many leading bits of an IPv6 address the range table keys on.
#:
#: 48, not 64. Measured against the source: keying at /48 leaves 357,607 of
#: DB-IP's 359,845 IPv6 country ranges distinguishable, and the 2,238 it merges
#: are places where two countries share a /48 — which is a quirk of the export
#: rather than a real allocation. /40 would collapse 40% of them, which is a
#: different country for a lot of addresses.
#:
#: It also removes a hazard rather than only saving two bytes a record: 48 bits
#: is always positive in a Dart `int`, so the reader needs no unsigned
#: comparison. At 64 bits every prefix in `8000::/1` sorted below zero, and
#: nothing routed today is up there — so the bug would have waited years.
IP6_KEY_BITS = 48
IP6_START_BYTES = 6

#: The country index that means "nothing is allocated here".
#:
#: A record covers every address from its start up to the *next* record's,
#: which is what lets the file be a sorted array a binary search runs on
#: directly. Gaps therefore have to be representable, or an unallocated block
#: inherits whichever country happens to precede it.
NO_COUNTRY = 0xFF


@dataclass(frozen=True)
class CountryRange:
    start: int
    """First address in the range, as an integer."""

    code: str | None
    """ISO 3166-1 alpha-2, or None for a gap."""


def build_country_db(
    coords: dict[str, tuple[float, float]],
    ip4: list[CountryRange],
    ip6: list[CountryRange],
) -> bytes:
    """The file the app bundles.

    ``coords`` maps a country code to where that country is drawn. ``ip4`` and
    ``ip6`` are sorted lists of range starts; ``ip6`` keys on the top
    [IP6_KEY_BITS] bits of the address.

    Layout::

        magic "SBGC"     4 B
        format           1 B   == 1
        countryCount     2 B   u16, at most 255
        country table    countryCount x 6 B: cc[2] + lat i16 + lon i16
        ip4Count         4 B   u32
        ip4 records      ip4Count x 5 B: start u32 + countryIdx u8
        ip6Count         4 B   u32
        ip6 records      ip6Count x 7 B: start u48 + countryIdx u8
    """
    codes = sorted(coords)
    if len(codes) > NO_COUNTRY:
        # Loud, and with the fix in the message: the reader refuses a file
        # claiming more than 255 countries, because the 256th index is the gap
        # sentinel. Widening `countryIdx` to u16 is format 2, and costs one
        # byte per record on both sides.
        raise SystemExit(
            f"{len(codes)} countries does not fit in a u8 index "
            f"(max {NO_COUNTRY}). Bump COUNTRY_FORMAT to 2 with a u16 index, "
            f"in this file and in GeoCountryDb."
        )
    index = {code: i for i, code in enumerate(codes)}

    out = bytearray()
    out += COUNTRY_MAGIC
    out += struct.pack(">BH", COUNTRY_FORMAT, len(codes))
    for code in codes:
        lat, lon = quantise(*coords[code])
        out += code.encode("ascii") + struct.pack(">hh", lat, lon)

    for ranges, start_bytes in ((ip4, 4), (ip6, IP6_START_BYTES)):
        out += struct.pack(">I", len(ranges))
        previous = -1
        for r in ranges:
            if r.start <= previous:
                raise SystemExit(f"country ranges out of order at {r.start}")
            previous = r.start
            out += r.start.to_bytes(start_bytes, "big")
            out += bytes([index[r.code] if r.code else NO_COUNTRY])

    return bytes(out)


# --------------------------------------------------------------------------
# The city shards, fetched on demand
# --------------------------------------------------------------------------

#: How many leading bits of the address name the shard.
#:
#: Eight, so a request discloses the first octet and nothing more. That is the
#: whole reason this is static files rather than a query API: the limit is a
#: property of the protocol, not a promise about whoever runs the server.
#:
#: It is also why there are no IPv6 shards — see the README. A prefix coarse
#: enough to keep an anonymity set worth having is one no fixed sharding keeps
#: under Cloudflare's file limit, and a prefix fine enough to fit discloses
#: more than the country database already answers for free.
IP4_SHARD_BITS = 8
IP4_KEY_BITS = 32
IP4_START_BYTES = 3
IP4_RECORD_BYTES = IP4_START_BYTES + 4


@dataclass(frozen=True)
class ShardRecord:
    offset: int
    """Address minus the shard's own first address."""

    lat: int
    lon: int
    """Quantised, or (NO_DATA, 0) for a gap."""


def build_ip4_shard(records: list[ShardRecord]) -> bytes:
    """One shard: a sorted array of fixed-width records, and nothing else.

    No header. The stride is in `index.json` for the whole family, so a shard
    is exactly what a binary search wants — and adding a header would mean
    every client offsetting past it on every lookup.

    Fixed width rather than a varint delta encoding, which would be about half
    the bytes: a delta encoding cannot be searched without being walked, and
    the size comes back at the transport layer anyway. These compress to
    roughly half over the wire.
    """
    if not records:
        return b""
    if records[0].offset != 0:
        # A lookup takes the last record at or below the key, so the first
        # record has to be at zero or an address below it has no answer at all
        # — which is a different thing from having no data.
        raise SystemExit("a shard must start at offset 0")

    out = bytearray()
    previous = -1
    for r in records:
        if r.offset <= previous:
            raise SystemExit(f"shard records out of order at {r.offset}")
        previous = r.offset
        out += r.offset.to_bytes(IP4_START_BYTES, "big")
        out += struct.pack(">hh", r.lat, r.lon)
    return bytes(out)
