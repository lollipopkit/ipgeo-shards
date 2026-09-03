# ipgeo-shards

Static IP-to-coordinate data for [ServerBox](https://github.com/lollipopkit/flutter_server_box)'s
globe, rebuilt monthly and served as plain files.

> IP geolocation by [DB-IP](https://db-ip.com), used under
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

There is no server code here. That is the point.

## Why files and not an API

The app draws each server where its address is, which means something has to
turn an address into a coordinate. Every hosted geolocation API answers that
question by being *told the address* — so using one would mean sending the list
of a user's servers to a third party, and the only protection against that
would be somebody's promise not to keep it.

Instead the data is split into 256 files by the first octet. The app fetches
the one file its address falls in and searches it locally. **Whoever serves
these files learns that some address in a /8 was looked up, and cannot learn
which** — not because of a policy, but because the full address is never sent.
Self-hosting is a one-line change (see below) and improves nothing about that
guarantee, which is how it should be.

The same reasoning is why there is no request log worth keeping, no API key, no
rate limit and no account.

## What is published

| Path | What | Size |
|---|---|---|
| `country_v1.bin` | One coordinate per country, and a range table over IPv4 and IPv6 | 4.1 MB, ~2 MB compressed |
| `v1/ip4/NNN.bin` | City coordinates for `NNN.0.0.0/8` | 221 files, 110 KB mean, 286 KB largest |
| `v1/index.json` | Which shards exist, and the record layout to read them with | 2 KB |

`country_v1.bin` is **bundled inside the app** rather than fetched. It is the
floor: it answers offline, on a first run, and for anyone who leaves the shard
download switched off — so the globe is never empty and nothing has to be
agreed to before it works. It is refreshed only when the app ships a release.

The shards are fetched on demand and cached. Downloading them is opt-in and off
by default.

Both endpoints serve the same bytes from the same build:

- `https://ipgeo.lollipopkit.com/` — Cloudflare, assets only, no Worker script
- `https://github.com/lollipopkit/ipgeo-shards/releases/latest/download/` —
  the fallback, for a network where the first is unreachable. Release assets
  cannot contain `/` in a name, so the shards are flattened to `ip4-NNN.bin`.

## There are no IPv6 city shards, and that is measured

IPv6 gets country-level accuracy from `country_v1.bin`, not city-level. This
is not an oversight and not a to-do — it is what the data allows:

| Sharding | Files | Largest shard | Verdict |
|---|---|---|---|
| by first byte | 9 | 22 MB | one shard is a quarter of the dataset |
| by first 2 bytes | 82 | 9.7 MB | 14 shards over 500 KB |
| by first 3 bytes | 9,186 | 2.3 MB | 15 shards over 500 KB |
| by first 4 bytes | 131,002 | 512 KB | over Cloudflare's 20,000-file limit |

The IPv6 space is allocated so unevenly that no fixed prefix balances it. The
other direction — coarsening the *key* so records merge — does not help either:
keying at /48 removes 3% of the records, and /40, which removes 82%, does so by
merging cities that are genuinely different places. At that point the answer is
country-level anyway, and `country_v1.bin` already gives that for free and
offline.

Merging adjacent ranges is worth about 1% on both families, because DB-IP has
already merged everything sharing a city.

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

## Formats

Big-endian throughout. Coordinates are a signed 16-bit fraction of their own
axis — about 300 m of latitude — so a pair costs four bytes instead of sixteen,
which is far finer than a city centroid is meaningful to begin with.

Both formats are a **sorted array with a fixed stride and no header**, so a
lookup is a binary search straight on the bytes. Fixed width rather than
varint deltas, which would be about half the size: a delta encoding has to be
walked to be searched, and the size comes back at the transport layer anyway.

A record covers every address from its start up to the *next* record's start.
Gaps therefore have to be representable, or an unallocated block silently
inherits whichever entry precedes it — `0xFF` is the country index that means
"nothing here", and latitude `-32768` is the shard equivalent.

### `country_v1.bin`

```
magic "SBGC"     4 B
format           1 B   == 1
countryCount     2 B   u16, at most 255 (256 is the gap index)
country table    countryCount x 6 B: cc[2] + lat i16 + lon i16
ip4Count         4 B   u32
ip4 records      ip4Count x 5 B: start u32 + countryIdx u8
ip6Count         4 B   u32
ip6 records      ip6Count x 7 B: start u48 + countryIdx u8
```

IPv6 keys on the **top 48 bits**. Measured: /48 leaves 357,607 of DB-IP's
359,845 IPv6 ranges distinguishable, while /40 collapses 40% of them into a
different country. 48 bits also fits a signed 64-bit integer without the top
bit ever being set, which removes a whole class of comparison bug from every
client — at 64 bits, prefixes in `8000::/1` sort below zero, and nothing routed
today is up there to make that obvious.

### `v1/ip4/NNN.bin`

```
records          n x 7 B: offset u24 + lat i16 + lon i16
```

`offset` is the address minus the shard's own first address. The first record
is always at offset 0, so no address in the shard is below the table. Read
`shardBits`, `keyBits`, `startBytes` and `recordBytes` out of `index.json`
rather than hard-coding them.

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
everywhere — a client that cannot parse a shard falls back to the country
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

`wrangler.toml` declares an assets directory and no `main`. There is no Worker
script — nothing needs to run, and a Worker that did nothing would still be a
place a request could be logged.

CI publishes on the 4th of each month, and on a manual run with `deploy: true`.
A pull request runs the tests and stops there: downloading 90 MB to throw the
result away is twenty minutes for no answer the tests did not already give.

Two secrets: `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.

## Self-hosting

The files are static and the build recipe is above, so anyone who would rather
not talk to this host can serve their own:

```sh
python3 src/build.py --out public
# then put `public/` behind any static file server
```

Point the app at it under **Settings → Servers → Globe**. Note that this
changes who serves the files, not what a request discloses — that is fixed by
the /8 sharding and is the same wherever the files come from.

## Licence

The code in this repository is MIT; see `LICENSE`.

The **data** is derived from DB-IP's Lite databases and is therefore
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Anything that
redistributes `country_v1.bin` or the shards has to carry the attribution
above — which is why it also appears in `v1/index.json`, in the app's About
page, and in every release's notes.
