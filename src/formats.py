"""The binary format this repository publishes.

One file per address family, read by ServerBox's `GeoBundle`. **There are two
implementations of it and nothing makes them agree except the test vectors in
`tests/vectors/`**, which are checked into both repositories. A reader tested
only against bytes its own writer produced agrees with itself and proves
nothing.

The country database and the per-/8 shards it used to publish are gone. Both
existed to keep a *per-lookup* request small: the country table so the app had
an offline floor, the shards so a lookup disclosed eight bits instead of an
address. Downloading the whole thing once discloses nothing at all, which
makes both of them answers to a question no longer asked.

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


# ---------------------------------------------------------------------------
# The bundle: one file, both families, city level throughout.
# ---------------------------------------------------------------------------

BUNDLE_MAGIC = b"SBGX"
BUNDLE_FORMAT = 1

#: How many leading bits of an IPv6 address the table keys on.
#:
#: 48, and measured rather than picked. At /48, 3,898,564 city ranges stay
#: distinguishable; /64 raises that only to 4,030,857 — 3% more distinctions
#: for 8 MB more file. /40 would merge different cities outright.
#:
#: It also removes a hazard rather than only saving bytes. A 64-bit prefix in
#: `8000::/1` is *negative* as a signed 64-bit integer, which is what Dart has,
#: so every comparison in a reader's binary search would need to be unsigned —
#: and nothing routed today is up there, so getting it wrong would wait years
#: to surface. 48 bits is always positive.
IP6_KEY_BITS = 48

#: Bits of the key that pick a first-level bucket, for each family.
#:
#: The bucket is not sharding left over from the file-per-/8 layout — that was
#: forced by a CDN's 20,000-file limit and is gone with it. It is in the file
#: because it makes every record's start *relative*, and a relative start is
#: three or four bytes instead of four or six. Measured on the real data, and
#: it wins after compression too, which is the only size that matters to
#: someone downloading it: IPv4 13.62 MB against 14.20 flat, IPv6 11.89
#: against 12.20.
IP4_BUCKET_BITS = 8
IP6_BUCKET_BITS = 16

IP4_OFFSET_BYTES = (32 - IP4_BUCKET_BITS) // 8          # 3
IP6_OFFSET_BYTES = (IP6_KEY_BITS - IP6_BUCKET_BITS) // 8  # 4
IP4_BUNDLE_RECORD = IP4_OFFSET_BYTES + 4
IP6_BUNDLE_RECORD = IP6_OFFSET_BYTES + 4

#: Which address family a bundle holds. In the header rather than inferred
#: from the filename, so a file that has been renamed or cached under the
#: wrong name is refused instead of read with the other family's stride.
FAMILY_IP4 = 4
FAMILY_IP6 = 6

#: `3x` pads the header to 16 bytes. Without it the header is 13 and the u32
#: bucket table that follows starts unaligned — legal everywhere, and free to
#: avoid.
_HEADER = struct.Struct(">4sBBHBI3x")
BUNDLE_HEADER_BYTES = _HEADER.size                       # 16


@dataclass(frozen=True)
class BundleRecord:
    """One row of either family: where it starts, and what is there."""

    start: int
    """The whole key — 32 bits for IPv4, [IP6_KEY_BITS] for IPv6.

    Split into bucket and offset by the writer, so a caller never has to know
    the layout to hand over a row.
    """

    lat: int
    lon: int
    """Quantised, or ([NO_DATA], 0) for a gap."""


def _bucketed(
    records: list[BundleRecord],
    key_bits: int,
    bucket_bits: int,
    offset_bytes: int,
) -> tuple[bytes, bytes, int]:
    """A family's bucket table and its records.

    Every bucket that has any record must begin with one at offset 0, or an
    address in the low part of it falls below every row and has no answer at
    all — which is a different thing from having no data, and the difference is
    the whole reason [NO_DATA] exists. The caller is responsible for that; this
    refuses a list that breaks it rather than writing a file that reads wrong.
    """
    span_bits = key_bits - bucket_bits
    buckets = 1 << bucket_bits
    table = bytearray()
    body = bytearray()
    at = 0
    index = 0
    previous = -1
    for bucket in range(buckets):
        table += at.to_bytes(4, "big")
        first_in_bucket = True
        while index < len(records) and (records[index].start >> span_bits) == bucket:
            r = records[index]
            if r.start <= previous:
                raise SystemExit(f"bundle records out of order at {r.start}")
            previous = r.start
            offset = r.start & ((1 << span_bits) - 1)
            if first_in_bucket and offset != 0:
                raise SystemExit(
                    f"bucket {bucket} starts at offset {offset}, not 0"
                )
            first_in_bucket = False
            body += offset.to_bytes(offset_bytes, "big")
            body += struct.pack(">hh", r.lat, r.lon)
            at += 1
            index += 1
    # The closing entry, so a reader takes `table[n]..table[n+1]` without a
    # special case for the last bucket.
    table += at.to_bytes(4, "big")
    return bytes(table), bytes(body), at


def build_bundle(
    family: int,
    records: list[BundleRecord],
    year: int,
    month: int,
) -> bytes:
    """One address family's city data, in one file.

    ```text
    magic "SBGX"          4 B
    format                1 B   == 1
    family                1 B   == 4 or 6
    year u16, month u8    3 B
    count u32             4 B
    reserved              3 B   zero, padding the header to 16
    bucket table  (2^bucketBits + 1) x u32   record index each bucket begins at
    records       count x (offset + lat i16 + lon i16)
    ```

    Two files rather than one, and **that is Cloudflare's 25 MiB per-asset
    limit rather than a preference**. The two families together come to 25.51
    MiB compressed, which is over it; apart they are 13.62 and 11.89. Raw they
    are 23.65 and 29.74, so the second is over on its own — which is why what
    is published is gzipped rather than left for the transport to compress.
    That also makes the download size a fixed number the app can put in front
    of someone before they agree to it.

    One file *per family* rather than 221 per family: the sharding only ever
    bought bandwidth. It cost eight bits of disclosure per lookup, and
    downloading the family costs none at all — which removes the index, the
    `present` set, the per-shard cache, the flattened fallback names and the
    month in the path, all of which existed to make many files work.

    The month is in the header rather than the filename, so a reader can tell
    what it holds without being told. That is what lets the app decide on its
    own to replace what it has.
    """
    if family == FAMILY_IP4:
        key_bits, bucket_bits, offset_bytes = 32, IP4_BUCKET_BITS, IP4_OFFSET_BYTES
    elif family == FAMILY_IP6:
        key_bits, bucket_bits, offset_bytes = (
            IP6_KEY_BITS,
            IP6_BUCKET_BITS,
            IP6_OFFSET_BYTES,
        )
    else:
        raise SystemExit(f"unknown family {family}")

    table, body, count = _bucketed(records, key_bits, bucket_bits, offset_bytes)
    header = _HEADER.pack(BUNDLE_MAGIC, BUNDLE_FORMAT, family, year, month, count)
    return header + table + body
