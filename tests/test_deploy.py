"""The two files that say the same thing about caching, and the Dockerfile.

`public/_headers` is what the Cloudflare endpoint reads; `deploy/nginx.conf`
is the same rules for the container. There is no format both sides understand,
so nothing but this stops them drifting apart — and drifting apart is silent:
one endpoint would start revalidating a file that cannot change, or stop
sending CORS, and only a browser would notice.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADERS = ROOT / "public" / "_headers"
NGINX = ROOT / "deploy" / "nginx.conf"
DOCKERFILE = ROOT / "Dockerfile"

#: The paths both files have to agree about, and the max-age each carries.
#:
#: The index says which month is current so it has to be re-read; everything
#: else is versioned and never edited in place, so it can be kept for as long
#: as a client likes.
EXPECTED_MAX_AGE = {
    "/v1/index.json": 3600,
    "/v1/ip4/": 2592000,
    "/country_v1.bin": 2592000,
}


def _headers_rules() -> dict[str, str]:
    """`_headers` as path -> the Cache-Control it sets."""
    rules: dict[str, str] = {}
    current: str | None = None
    for line in HEADERS.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            current = line.strip()
            continue
        name, _, value = line.strip().partition(":")
        if current and name.lower() == "cache-control":
            rules[current] = value.strip()
    return rules


class CacheControl(unittest.TestCase):
    def test_both_files_set_the_same_max_age_for_every_path(self):
        headers = _headers_rules()
        nginx = NGINX.read_text()

        for path, max_age in EXPECTED_MAX_AGE.items():
            with self.subTest(path=path):
                # `_headers` matches a glob; the paths there end in `*` for a
                # directory, so compare on the prefix.
                match = next(
                    (v for k, v in headers.items() if k.rstrip("*") == path), None
                )
                self.assertIsNotNone(match, f"{path} missing from _headers")
                self.assertIn(f"max-age={max_age}", match)

                # nginx names the same path in a `location` and sets the same
                # number somewhere inside it.
                block = re.search(
                    r"location\s+(?:=\s+)?" + re.escape(path) + r"\s*\{(.*?)\n    \}",
                    nginx,
                    re.S,
                )
                self.assertIsNotNone(block, f"{path} missing from nginx.conf")
                self.assertIn(f"max-age={max_age}", block.group(1))

    def test_the_immutable_paths_are_the_same_on_both_sides(self):
        headers = _headers_rules()
        nginx = NGINX.read_text()
        for path, value in headers.items():
            if "immutable" not in value:
                continue
            bare = path.rstrip("*")
            with self.subTest(path=bare):
                block = re.search(
                    r"location\s+(?:=\s+)?" + re.escape(bare) + r"\s*\{(.*?)\n    \}",
                    nginx,
                    re.S,
                )
                self.assertIsNotNone(block, f"{bare} is immutable only in _headers")
                self.assertIn("immutable", block.group(1))

    def test_cors_is_open_on_both(self):
        # The app is not a browser, but the panel and anything else built on
        # this is. A missing header here is a class of client that cannot use
        # the endpoint at all.
        self.assertIn("Access-Control-Allow-Origin: *", HEADERS.read_text())
        for path in EXPECTED_MAX_AGE:
            block = re.search(
                r"location\s+(?:=\s+)?" + re.escape(path) + r"\s*\{(.*?)\n    \}",
                NGINX.read_text(),
                re.S,
            )
            self.assertIn("Access-Control-Allow-Origin", block.group(1), path)


class Container(unittest.TestCase):
    def test_nothing_writes_where_the_image_is_read_only(self):
        # The container runs `read_only` with two tmpfs mounts. Every path
        # nginx opens has to be under one of them, or it restart-loops on
        # `Permission denied` — which is exactly what it did.
        nginx = NGINX.read_text()
        writable = ("/tmp/", "/run/nginx")
        for directive in (
            "client_body_temp_path",
            "proxy_temp_path",
            "fastcgi_temp_path",
            "uwsgi_temp_path",
            "scgi_temp_path",
            "pid",
        ):
            match = re.search(rf"^\s*{directive}\s+(\S+?);", nginx, re.M)
            self.assertIsNotNone(match, f"{directive} is not set")
            self.assertTrue(
                match.group(1).startswith(writable),
                f"{directive} writes to {match.group(1)}, which is read-only",
            )

    def test_the_error_log_is_set_before_the_config_is_read(self):
        # nginx opens its compiled-in error log before parsing the config, so
        # `error_log` in the file cannot stop it complaining about a path it
        # cannot write. `-e` on the command line can.
        self.assertIn('"-e", "/dev/stderr"', DOCKERFILE.read_text())

    def test_the_image_does_not_run_as_root(self):
        self.assertIn("USER nginx", DOCKERFILE.read_text())

    def test_the_base_is_pinned(self):
        # A rebuild of the same commit should be the same image; the data
        # changes monthly and the base should not change under it.
        match = re.search(r"^FROM\s+(\S+)", DOCKERFILE.read_text(), re.M)
        self.assertIsNotNone(match)
        self.assertRegex(match.group(1), r"^alpine:\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
