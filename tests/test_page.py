"""The query page's own arithmetic, run in a JS engine rather than reimplemented.

The page is the only JavaScript reader of this format, and it had no tests at
all. That is how it shipped a bug `src/verify.py` could never have found:
Python has no 32-bit integers, so a check written here in Python agrees with
the builder no matter what the browser does.

The bug was `k - (shard << 24)`. JS coerces both operands of `<<` to *signed
32-bit*, so `192 << 24` is -1073741824 rather than 3221225472, the offset came
out around 4.3 billion, and the binary search fell through to the last record
in the shard. Every address in 128.0.0.0/8 and above answered with wherever
that last record happened to be; 192.168.31.1 came back as Dallas. The four
sample buttons on the page are 8.8.8.8, 1.1.1.1, 9.9.9.9 and 114.114.114.114 —
all below 128, all correct, which is why nobody saw it.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "public" / "index.html"

# The page has no module boundary, so the functions under test are lifted out
# by name. Brittle on purpose: if one is renamed this fails loudly rather than
# quietly testing nothing.
WANTED = ("key",)


def _extract(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth, i, seen = 0, start, False
    while i < len(source):
        if source[i] == "{":
            depth, seen = depth + 1, True
        elif source[i] == "}":
            depth -= 1
            if seen and depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces reading {name}")


class PageArithmetic(unittest.TestCase):
    """`key`, and the shard/offset split the page derives from it."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("node") is None:
            # Not skipped. A skip here is how this would go back to being
            # untested the first time a runner lacked node.
            raise AssertionError("node is required to test the page's JS")
        cls.source = PAGE.read_text(encoding="utf-8")

    def run_js(self, addresses):
        """`{shard, offset}` for each address, as the page computes them."""
        lifted = "\n".join(_extract(self.source, n) for n in WANTED)
        # The same two lines the page runs, read out of it rather than copied,
        # so a future edit that reintroduces a shift is caught here.
        split = re.search(
            r"const span = ([^;]+);\s*const shard = ([^;]+);", self.source
        )
        self.assertIsNotNone(split, "the page no longer derives shard from span")
        offset = re.search(r"const offset = ([^;]+);", self.source)
        self.assertIsNotNone(offset, "the page no longer derives an offset")

        script = f"""
{lifted}
const family = {{ keyBits: 32, shardBits: 8 }};
const out = {json.dumps(addresses)}.map((text) => {{
  const k = key(text);
  const span = {split.group(1)};
  const shard = {split.group(2)};
  const offset = {offset.group(1)};
  return {{ text, k, shard, offset }};
}});
console.log(JSON.stringify(out));
"""
        done = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        return {r["text"]: r for r in json.loads(done.stdout)}

    def test_a_high_shard_is_not_wrapped_by_a_32_bit_shift(self):
        """The regression. 192 << 24 is negative; 192 * 2**24 is not."""
        got = self.run_js(["192.168.31.1"])["192.168.31.1"]
        self.assertEqual(got["shard"], 192)
        self.assertEqual(got["offset"], (168 << 16) | (31 << 8) | 1)

    def test_every_octet_boundary_splits_where_it_should(self):
        """Both halves of the space, including the one the bug did not touch."""
        cases = {
            "0.0.0.0": (0, 0),
            "8.8.8.8": (8, 0x080808),
            "114.114.114.114": (114, 0x727272),
            "127.255.255.255": (127, 0xFFFFFF),
            # The first address the old arithmetic got wrong.
            "128.0.0.0": (128, 0),
            "192.168.31.1": (192, 0xA81F01),
            "255.255.255.255": (255, 0xFFFFFF),
        }
        got = self.run_js(list(cases))
        for text, (shard, offset) in cases.items():
            with self.subTest(text):
                self.assertEqual(got[text]["shard"], shard)
                self.assertEqual(got[text]["offset"], offset)

    def test_the_key_is_the_whole_address(self):
        got = self.run_js(["255.255.255.255", "0.0.0.1"])
        self.assertEqual(got["255.255.255.255"]["k"], 0xFFFFFFFF)
        self.assertEqual(got["0.0.0.1"]["k"], 1)

    def test_the_offset_never_leaves_the_shard(self):
        """What the old bug violated, stated as the invariant it broke."""
        addresses = [f"{a}.170.85.3" for a in (0, 1, 127, 128, 200, 255)]
        for text, got in self.run_js(addresses).items():
            with self.subTest(text):
                self.assertGreaterEqual(got["offset"], 0)
                self.assertLess(got["offset"], 1 << 24)
                self.assertEqual(
                    got["shard"] * (1 << 24) + got["offset"], got["k"]
                )


if __name__ == "__main__":
    unittest.main()
