"""范围判定、URL 规范化与安全落盘路径的单元测试。"""

import unittest

import _path  # noqa: F401  隔离模式路径引导（discover 时 tests/ 在 sys.path）

from app.model import KIND_PAGE, KIND_RESOURCE
from app.scope import (
    Scope,
    normalize_url,
    origin_of,
    relative_href,
    safe_page_relpath,
    split_fragment,
)


class NormalizeTests(unittest.TestCase):
    def test_query_and_path_kept(self):
        url = "https://Exhibition.Invalid:443/show/a.html?x=1&y=2#frag"
        norm = normalize_url(url)
        self.assertEqual(norm, "https://exhibition.invalid/show/a.html?x=1&y=2")
        self.assertEqual(split_fragment(url)[1], "frag")

    def test_default_port_stripped_but_custom_kept(self):
        self.assertEqual(
            normalize_url("http://host.invalid:80/a"),
            "http://host.invalid/a",
        )
        self.assertEqual(
            normalize_url("http://host.invalid:8080/a"),
            "http://host.invalid:8080/a",
        )

    def test_origin(self):
        self.assertEqual(
            origin_of("https://h.invalid:443/show/x?a=1"),
            "https://h.invalid",
        )


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.scope = Scope.from_config({
            "page_prefixes": ["https://exhibition.invalid/show/"],
            "resource_origins": ["https://exhibition.invalid"],
        })

    def test_page_prefix_boundary(self):
        self.assertTrue(
            self.scope.check("https://exhibition.invalid/show/a", KIND_PAGE).allowed
        )
        # 前缀按路径边界，/showcase 不被 /show/ 授权
        decision = self.scope.check(
            "https://exhibition.invalid/showcase/2030", KIND_PAGE
        )
        self.assertFalse(decision.allowed)

    def test_resource_origin_only(self):
        ok = self.scope.check(
            "https://exhibition.invalid/anywhere/a.png?size=s", KIND_RESOURCE
        )
        self.assertTrue(ok.allowed)
        denied = self.scope.check(
            "https://outside.invalid/a.png", KIND_RESOURCE
        )
        self.assertFalse(denied.allowed)

    def test_redirect_every_hop_checked(self):
        # 模拟逐跳：第一跳域内允许，第二跳越界必须拒绝
        hop1 = self.scope.check(
            "https://exhibition.invalid/redirect/second", KIND_RESOURCE
        )
        hop2 = self.scope.check(
            "https://outside.invalid/whole-site.css", KIND_RESOURCE
        )
        self.assertTrue(hop1.allowed)
        self.assertFalse(hop2.allowed)


class SafePathTests(unittest.TestCase):
    def test_parent_traversal_neutralized(self):
        rel = safe_page_relpath(
            "https://exhibition.invalid/show/../../etc/passwd"
        )
        self.assertNotIn("..", rel.split("/"))
        self.assertTrue(rel.startswith("pages/"))

    def test_query_disambiguates(self):
        a = safe_page_relpath("https://x.invalid/show/p.html")
        b = safe_page_relpath("https://x.invalid/show/p.html?lang=en")
        self.assertNotEqual(a, b)

    def test_relative_href(self):
        self.assertEqual(
            relative_href("pages/show/a.html", "store/ab/x.png"),
            "../../store/ab/x.png",
        )
        self.assertEqual(
            relative_href("assets/css/x.css", "assets/css/y.css"),
            "y.css",
        )


if __name__ == "__main__":
    unittest.main()
