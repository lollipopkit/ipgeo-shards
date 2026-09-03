# Running your own

The endpoint is static files. Anything that serves a directory over HTTPS will
do, and this is only what the maintainer's host runs.

```sh
docker compose pull && docker compose up -d
```

The image is `ghcr.io/lollipopkit/ipgeo-shards:latest`, built by
`.github/workflows/update.yml` from the same run that produced the month's
data — so the image and the files it serves are never a different build. Tags:

- `latest` — the newest month
- `2026-09` — that month, forever

The container publishes nothing. Put a reverse proxy in front of it; it
listens on 8080 and answers by container name on the compose network.

## Behind a reverse proxy

The container has to be reachable by name, which means the proxy has to be on
this project's network:

```sh
docker network connect ipgeo-shards_default <your-proxy-container>
```

An nginx server block, matching how the other services on that host are
routed:

```nginx
server {
    server_name ipgeo.lollipopkit.com;

    listen 443 ssl;
    listen [::]:443 ssl;

    ssl_certificate     /etc/letsencrypt/live/lollipopkit.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/lollipopkit.com/privkey.pem;

    location / {
        # By name, so a container restart with a new address is not an
        # outage — resolved per request rather than pinned at config load.
        set $ipgeo_upstream ipgeo-shards:8080;
        proxy_pass http://$ipgeo_upstream;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then point the app at it: **Settings → Servers → Globe → endpoint**. What a
request discloses is fixed by the /8 sharding and is the same wherever the
files come from, so self-hosting changes who serves them and nothing else.

## Without Docker

```sh
python3 src/build.py --out public
```

and serve `public/` with anything. `deploy/nginx.conf` is what the image uses
and is the reference for the headers; nothing about it is required — it sets
`Cache-Control` and CORS, and getting those wrong costs bandwidth rather than
correctness.
