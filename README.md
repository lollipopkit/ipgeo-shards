# ipgeo-shards

Static IP-to-coordinate data for [ServerBox](https://github.com/lollipopkit/flutter_server_box)'s
globe, rebuilt monthly and served as plain files.

> IP geolocation by [DB-IP](https://db-ip.com), used under
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

There is no server code here. That is the point.

## Why a download and not an API

The app draws each server where its address is, which means something has to
turn an address into a coordinate. Every hosted geolocation API answers that
question by being *told the address* — so using one would mean sending the list
of a user's servers to a third party, and the only protection against that
would be somebody's promise not to keep it.

Instead the whole dataset is downloaded once, and every lookup after that is
local. **Whoever serves it learns that someone installed it, and nothing about
what was looked up** — not which address, not how many, not when. That is a
property of having the file rather than a promise about whoever served it, and
self-hosting improves nothing about it, which is how it should be.

It used to be split into 256 files by the first octet, fetched one at a time.
That disclosed eight bits of every address looked up, and the whole-bundle
format above is what replaced it.

The same reasoning is why there is no request log worth keeping, no API key, no
rate limit and no account.

## What is published

Three files, rebuilt monthly:

| file | packed | unpacked | records |
|---|---|---|---|
| `ip4_city_v1.bin.gz` | 13.62 MiB | 23.65 MiB | 3,542,032 |
| `ip6_city_v1.bin.gz` | 11.48 MiB | 28.90 MiB | 3,755,198 |
| `manifest.json` | — | — | names both, with sizes and a sha256 each |

City level in both families. Read `manifest.json` first: it is what says how
much a client is about to download, before it downloads any of it.

**Gzipped as published, not by the transport.** Cloudflare refuses an asset
over 25 MiB and the raw IPv6 bundle is 28.9, so this is what makes it fit at
all. It also makes the download a fixed number rather than whatever a CDN
negotiates, which matters because the app puts that number in front of someone
and asks them to agree to it.

Two endpoints, same bytes from the same run:

- `https://ipgeo-shards.pages.dev/` — Cloudflare Pages, the primary
- `https://github.com/lollipopkit/ipgeo-shards/releases/latest/download/` —
  a fixed URL that needs no tag lookup, for a network where the first is
  unreachable

## Coverage

| | covered |
|---|---|
| IPv4, city level | **100.00%** of routable space |
| IPv6, city level | effectively all of allocated space |

"Routable" is what `isPrivateHost` does not refuse — 3,702,258,688 addresses,
86.2% of the 32-bit space. No row is dropped for a missing coordinate or for
sitting on Null Island; the ones that are dropped are `ZZ` (DB-IP's own
"unknown"), which is 256 addresses.

**IPv6 used to be country level only, and the reason was file count.** A fixed
shard depth could not work: ranges have to be written into every shard they
span, so /8 left one shard at 16 MB while anything fine enough to cap a shard
needed 56,768 files at /16 or 1,043,906 at /24 — against Cloudflare's 20,000.
A variable depth would have fit, at 6,388 files.

None of that applies to a file that is downloaded whole. The bucket table is
inside it, there is no file count, and IPv6 costs 11.48 MiB packed. Two dead
ends are worth keeping written down: coarsening the key past /48 merges
different cities, and quantising coordinates from 1.1 km to 110 km removes
only 29% of the records — there are many records because the address space is
finely divided, not because coordinates repeat.

## Where a country is drawn

Each country's coordinate is its **most-addressed city**, weighted by how many
IPv4 addresses each city covers. Not its capital, and not a geometric centroid.

- A centroid lands in the sea for anywhere shaped like Indonesia.
- Capitals are incomplete: Natural Earth marks 195 countries as holding an
  `Admin-0 capital`, and DB-IP knows 242. Hong Kong, Macau, Puerto Rico,
  Greenland and Palestine are among the missing, and they hold a lot of
  servers.
- Weighting by addresses puts Germany at Frankfurt rather than Berlin, and the
  United States at Columbus rather than Washington. For an app that draws
  *servers*, that is the better answer — servers are in data centres.

## Format

Big-endian throughout, one file per family.

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

| | IPv4 | IPv6 |
|---|---|---|
| key | the whole address, 32 bits | the leading 48 bits |
| bucket | top 8 bits | top 16 bits of the key |
| offset | u24 | u32 |
| record | 7 B | 8 B |

A record covers every address from its own key up to the next record's, which
is what makes the file a sorted array a binary search runs on directly. Gaps
therefore have to be representable: `lat == -32768` means *nothing is
allocated here*, and without it an unallocated block would inherit whatever
precedes it. It is the one value the quantisation cannot produce.

**Every bucket that has any record begins with one at offset 0.** Otherwise an
address in the low part of a bucket falls below every row and has no answer at
all — which a client cannot tell from "no data here". A bucket with no records
at all is the normal way to say a whole /8 or /16 is unallocated; it replaced
the old index's `present` list.

The family is in the header rather than the filename, so a file renamed or
cached under the wrong name is refused instead of read with the other stride —
where every record would decode to something plausible.

`year`/`month` are in the header too, so a client can tell what it is holding
without being told. That is what lets it decide on its own to replace what it
has.

Coordinates are a signed 16-bit fraction of their own axis: about 300 m of
latitude, 600 m of longitude at the equator. Finer than a city centroid, and
4 bytes instead of 16.

**IPv6 is keyed at /48, measured rather than picked.** 3,898,564 city ranges
stay distinguishable there against 4,030,857 at /64 — 3% more distinctions for
8 MB more file. /40 would merge different cities outright. It also keeps every
prefix positive as a signed 64-bit integer, which `8000::/1` is not at /64;
a reader's binary search would need unsigned comparisons, and nothing routed
today would reveal the mistake.

## Building

Python 3.12, standard library only. There is nothing to install.

```sh
python3 src/build.py --out public     # fetch, convert, write
python3 src/verify.py --out public    # read it back and check it
python3 -m unittest discover -s tests
```

`build.py` caches the DB-IP downloads under `.cache/`, so a second run costs
nothing. Its output is deterministic: same inputs, byte-identical files.

`verify.py` is not a formality. A file that is subtly wrong is silent
everywhere — a client that cannot parse a bundle places nothing
database, one that reads at the wrong offset draws a server in the wrong city —
so the output is read back by a parser that is not the writer.

### Test vectors

`tests/vectors/` is a small binary and a JSON file saying what looking things
up in it must answer. The same pair is checked into the app at
`test/fixtures/geo/`, and each side reads the binary with its own
implementation. That is the only thing making two independently written readers
agree; a reader tested against bytes its own writer produced agrees with itself
and proves nothing.

Regenerate with `python3 tests/make_vectors.py`. **Do not regenerate a vector
to make a failing test pass.**

### The month in the URL

DB-IP's download URL carries the year and month, and the current month's file
is not published on the first. Every fetch walks back up to three months until
one answers, which also makes a mid-month re-run pick the same file the last
one did. A month DB-IP skips republishes the previous month rather than
failing.

## Deploying

There is no server. `.github/workflows/update.yml` builds on the 4th of each
month, verifies the output with a reader that is not the writer, and publishes
the same three files as a GitHub release, with Cloudflare Pages building its
own copy from the repository.

Cloudflare Pages builds the data itself, from this repository:

| | |
|---|---|
| build command | `python3 src/build.py --out public` |
| output directory | `public` |
| dependencies | none — the build is Python standard library only |

Nothing is uploaded to it from CI. What CI does is *nudge* it: Pages
triggers on push, this repository does not otherwise change monthly, and the
data comes from DB-IP at build time — so left alone it would serve whatever
month it was first configured on.

So the monthly run writes `published.json` (the month, and the manifest's
digest) and pushes it to `main`, which is what Cloudflare deploys. Written only
when the month or the manifest changed, so a re-run in the same month pushes
nothing.

Two builders are safe here because the build is byte-reproducible: Cloudflare
and the release are produced separately from the same month's export and come
out identical.

DB-IP releases on the first but not always on the first, so the fetch walks
back up to three months. A month with no release republishes the previous one
rather than failing.

## Self-hosting

The files are static and the build is reproducible, so anyone can serve their
own copy:

```sh
python3 src/build.py --out public
python3 src/verify.py --out public
# then put `public/` behind any static file server
```

Nothing about the protocol depends on who serves it. A client downloads both
bundles once and every lookup after that is local, so the server learns that
someone fetched the data and nothing else — not which addresses were looked
up, not how many, not when. That is a property of downloading the whole thing
rather than a promise about the operator, which is why changing the endpoint
buys nothing: there is nothing to improve.

## Licence

The code in this repository is MIT; see `LICENSE`.

The **data** is derived from DB-IP's Lite databases and is therefore
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Anything that
redistributes these files has to carry the attribution above — which is why
it also appears in `manifest.json`, in the app's About
page, and in every release's notes.
