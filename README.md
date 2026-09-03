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

- `https://ipgeo.lollipopkit.com/` — an `ipgeo-shards` container on the
  maintainer's host, behind Cloudflare's CDN. The same image anyone can run
- `https://github.com/lollipopkit/ipgeo-shards/releases/latest/download/` —
  the fallback, for a network where the first is unreachable. Release assets
  cannot contain `/` in a name, so the shards are flattened to `ip4-NNN.bin`.

## Coverage

Both families are placed at **country level for 100.00% of routable IPv4
space** — DB-IP's country export leaves exactly 256 addresses as `ZZ` — and
for effectively all of allocated IPv6. That is what `country_v1.bin` gives
offline, with nothing asked and nothing downloaded.

The city shards cover **100.00% of routable IPv4** too: no row is dropped for
a missing coordinate, and none sits on Null Island. "Routable" here means
everything `isPrivateHost` does not already refuse — 3,702,258,688 addresses,
86.2% of the 32-bit space.

**IPv6 has no city shards.** It is answered at country level and nowhere
finer. That is a scope decision rather than an impossibility, and it is worth
writing down which:

A *fixed* shard depth does not work. Ranges have to be written into every
shard they span, or a lookup lands in an empty file and finds nothing — and
IPv6 ranges are enormous in prefix space, so the record count grows with the
depth while the shard size falls:

| Depth | Files | Records | Largest shard |
|---|---|---|---|
| /8 | 222 | 4.16 M | 16.6 MB |
| /16 | 56,768 | 4.22 M | 6.5 MB |
| /24 | 1,043,906 | 5.20 M | 1.8 MB |

Every depth is bad at one end or the other: /8 and /12 keep the file count
small and leave one shard at 16 MB, while anything fine enough to cap a shard
needs 50,000 to a million files against Cloudflare's limit of 20,000.

A *variable* depth does work, and has been measured: split a shard only when
it would exceed 512 KB, eight bits at a time, and the result is 6,388 files
totalling 25 MB with none over 509 KB — 8 prefixes staying at /8, 1,268 at
/16, 3,064 at /24 and 2,048 at /32. The index would carry the depth each
prefix ended up at. That is a real option and it is not built; the country
answer is what IPv6 gets for now.

Coarsening the *key* instead is not a way out: /48 removes 3% of the records
and /40 removes 82% by merging cities that are genuinely different places, at
which point the answer is country-level anyway.

Merging adjacent ranges is worth about 1% on both families, because DB-IP has
already merged everything sharing a city. Quantising the coordinates is worth
little more — going from 1.1 km to 110 km precision removes only 29% of the
records, since what makes them numerous is how finely the address space is
carved up rather than how often a coordinate repeats.

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

CI builds on the 4th of each month and pushes the image to ghcr.io. **That is
the whole deploy**: the host runs `ghcr.io/lollipopkit/ipgeo-shards:latest`
and picks the month up with a `docker compose pull`. Nothing in this
repository holds a credential to that host, which is the arrangement worth
keeping — a workflow that could reach into the server would be a workflow
worth compromising.

A GitHub Release goes out in the same run, which is the second endpoint. It
needs `deploy: true` on a manual run, or the monthly schedule.

A pull request runs the tests and stops there: downloading 90 MB to throw the
result away is twenty minutes for no answer the tests did not already give.

No secrets. `GITHUB_TOKEN` is what pushes the image and cuts the release.

## Self-hosting

The files are static, so anyone who would rather not talk to this host can
serve their own. There is an image:

```sh
docker run -d -p 8080:8080 ghcr.io/lollipopkit/ipgeo-shards:latest
```

Alpine and nginx, with the month's data baked in — built by the same CI run
that produced the data, so the image and the files it serves are never a
different build. `amd64` and `arm64`. See `deploy/` for a compose file and a
reverse-proxy example.

Or without Docker:

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
