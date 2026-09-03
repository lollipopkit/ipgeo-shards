"""What the container serves with, and what it is allowed to write.

Both are silent when wrong. A `Cache-Control` that says a shard expires in an
hour costs bandwidth nobody measures; a missing CORS header locks out a whole
class of client and only a browser says so; and a path nginx writes to that is
not on a tmpfs makes the container restart-loop, which is what it did.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NGINX = ROOT / "deploy" / "nginx.conf"
DOCKERFILE = ROOT / "Dockerfile"

#: Every path the endpoint serves, and how long it may be kept.
#:
#: The index says which month is current so it has to be re-read; everything
#: else is versioned and never edited in place, so it can be kept for as long
#: as a client likes.
EXPECTED_MAX_AGE = {
    "= /v1/index.json": 3600,
    # A regex location: the month is part of the path, which is what makes
    # `immutable` true.
    "~ ^/v1/[0-9]{4}-[0-9]{2}/": 2592000,
    "= /country_v1.bin": 2592000,
}


def _location(path: str) -> str:
    """The body of the `location` block for `path`.

    Matched to the first closing brace with no opening one in between, rather
    than to a particular indentation. The indentation version quietly matched
    from one block to the end of the file, so every assertion about
    `index.json` was really about the whole config and two of them passed for
    the wrong reason.
    """
    block = re.search(
        r"location\s+" + re.escape(path) + r"\s*\{([^{}]*)\}",
        NGINX.read_text(),
    )
    assert block is not None, f"{path} has no location block"
    return block.group(1)


class CacheControl(unittest.TestCase):
    def test_every_path_is_served_with_the_max_age_it_should_be(self):
        for path, max_age in EXPECTED_MAX_AGE.items():
            with self.subTest(path=path):
                self.assertIn(f"max-age={max_age}", _location(path))

    def test_the_versioned_paths_are_immutable_and_the_index_is_not(self):
        # A versioned path is never edited in place, so a client may keep it
        # for as long as it likes. The index is what says which month is
        # current, so it has to be re-read or a client never learns.
        self.assertNotIn("immutable", _location("= /v1/index.json"))
        self.assertIn("must-revalidate", _location("= /v1/index.json"))
        self.assertIn("immutable", _location("~ ^/v1/[0-9]{4}-[0-9]{2}/"))
        self.assertIn("immutable", _location("= /country_v1.bin"))

    def test_cors_is_open_everywhere(self):
        # The app is not a browser, but a panel or anything else built on this
        # is. A missing header here is a class of client that cannot use the
        # endpoint at all.
        for path in EXPECTED_MAX_AGE:
            self.assertIn("Access-Control-Allow-Origin", _location(path), path)

    def test_nothing_outside_the_contract_is_served(self):
        # No directory listing, and no reaching past the root.
        nginx = NGINX.read_text()
        self.assertIn("autoindex off", nginx)
        self.assertIn("return 404", _location("/"))

    def test_the_shards_are_not_logged(self):
        # The whole design is that a request discloses only a prefix. Writing
        # down which prefix somebody asked for is the one thing it avoids.
        self.assertRegex(NGINX.read_text(), r"\baccess_log\s+off;")


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

    def test_the_config_is_checked_at_build_time(self):
        # Whether nginx accepts a config is not something this can decide —
        # only nginx can. `nginx -t` in the build is what does, and it is here
        # so that removing it is a deliberate act: without it a config error
        # ships and the container restart-loops, which is what happened to an
        # unquoted regex containing braces.
        self.assertIn("RUN nginx -t", DOCKERFILE.read_text())

    def test_the_base_is_pinned(self):
        # A rebuild of the same commit should be the same image; the data
        # changes monthly and the base should not change under it.
        match = re.search(r"^FROM\s+(\S+)", DOCKERFILE.read_text(), re.M)
        self.assertIsNotNone(match)
        self.assertRegex(match.group(1), r"^alpine:\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
