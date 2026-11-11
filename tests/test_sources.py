import unittest
import json
import _path  # noqa: F401  隔离模式路径引导
from app.wire import Replay

from app.references import inspect_links
class DomainTests(unittest.TestCase):
    def test_base(self):
        r=inspect_links('<base href="/a/"><img src="i.png">',"https://x.invalid/z")
        self.assertEqual(r[-1]["fetch"],"https://x.invalid/a/i.png")
    def test_fragment(self):
        r=inspect_links('<a href="a?q=1#f">x</a>',"https://x.invalid/")[0]
        self.assertEqual(r["fragment"],"f");self.assertTrue(r["fetch"].endswith("?q=1"))
    def test_external(self):
        self.assertEqual(inspect_links('<img src="//y.invalid/a">',"https://x.invalid/")[0]["fetch"],"https://y.invalid/a")


class WireTests(unittest.TestCase):
    def test_unknown_route(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as d:
            p=Path(d)/"routes.json";p.write_text("{}")
            self.assertEqual(Replay(p).get("https://missing.invalid").status,404)
