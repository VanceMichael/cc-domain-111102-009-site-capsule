"""HTML 与 CSS 引用提取、改写的单元测试。"""

import unittest

import _path  # noqa: F401  隔离模式路径引导（discover 时 tests/ 在 sys.path）

from app.cssrewrite import extract_css_references, rewrite_css
from app.htmlrewrite import parse_html, rewrite_html
from app.model import REF_MEDIA, REF_PAGE, REF_STYLESHEET


class CssTests(unittest.TestCase):
    def test_import_and_url_with_comments(self):
        css = '/* url(commented.png) */ @import "other.css"; ' \
              '.a { background: url("bg.png"); }'
        refs = extract_css_references(css, "https://x.invalid/show/a.css")
        targets = [r.target for r in refs]
        self.assertIn("other.css", targets)
        self.assertIn("bg.png", targets)
        self.assertNotIn("commented.png", targets)

    def test_circular_import_resolves_by_identity(self):
        # 循环本身在解析层只是两个普通引用，不成环
        a = extract_css_references('@import "b.css";', "https://x/a.css")
        b = extract_css_references('@import "a.css";', "https://x/b.css")
        self.assertEqual(a[0].resolved, "https://x/b.css")
        self.assertEqual(b[0].resolved, "https://x/a.css")

    def test_rewrite_keeps_unarchived_absolute(self):
        css = ".a { background: url(https://x.invalid/missing.png); }"
        out = rewrite_css(css, "https://x.invalid/a.css", lambda u: None)
        self.assertIn("https://x.invalid/missing.png", out)
        out2 = rewrite_css(
            css, "https://x.invalid/a.css",
            lambda u: "../../store/ab/c.png",
        )
        self.assertIn("../../store/ab/c.png", out2)


class HtmlParseTests(unittest.TestCase):
    def test_base_relative_srcset_fragment(self):
        html = (
            '<base href="/show/">'
            '<img src="a.png" srcset="a.png 1x, b.png 2x">'
            '<a href="p.html#sec">x</a>'
        )
        parsed = parse_html(html, "https://x.invalid/show/index.html")
        self.assertEqual(parsed.base_href, "https://x.invalid/show/")
        srcs = {r.fetch_url for r in parsed.references if r.attribute == "srcset"}
        self.assertEqual(
            srcs,
            {"https://x.invalid/show/a.png", "https://x.invalid/show/b.png"},
        )
        link = next(r for r in parsed.references if r.tag == "a")
        self.assertEqual(link.fragment, "sec")
        self.assertEqual(link.fetch_url, "https://x.invalid/show/p.html")

    def test_inline_style_url(self):
        parsed = parse_html(
            '<div style="background: url(tile.png)">x</div>',
            "https://x.invalid/show/index.html",
        )
        self.assertTrue(any(
            r.fetch_url == "https://x.invalid/show/tile.png"
            for r in parsed.references
        ))

    def test_media_excluded_but_poster_kept(self):
        parsed = parse_html(
            '<video poster="cover.png" src="tour.mp4"></video>',
            "https://x.invalid/show/index.html",
        )
        kinds = {r.attribute: r.kind for r in parsed.references}
        self.assertEqual(kinds["src"], REF_MEDIA)
        self.assertNotEqual(kinds["poster"], REF_MEDIA)

    def test_meta_refresh(self):
        parsed = parse_html(
            '<meta http-equiv="refresh" content="0; url=next.html#top">',
            "https://x.invalid/show/a.html",
        )
        ref = next(r for r in parsed.references if r.tag == "meta")
        self.assertEqual(ref.kind, REF_PAGE)
        self.assertEqual(ref.fragment, "top")


class HtmlRewriteTests(unittest.TestCase):
    def test_rewrite_local_and_keep_external(self):
        html = (
            '<base href="/show/">'
            '<link rel="stylesheet" href="s.css">'
            '<a href="p.html#f">p</a>'
            '<a href="https://out.invalid/x">out</a>'
        )

        def resolve_local(fetch_url):
            if fetch_url.endswith("s.css"):
                return "../../assets/css/s.css"
            if fetch_url.endswith("p.html"):
                return "p.html"
            return None

        out = rewrite_html(html, "https://x.invalid/show/index.html",
                           resolve_local)
        self.assertNotIn("<base", out)  # base 被移除
        self.assertIn("../../assets/css/s.css", out)
        self.assertIn('href="p.html#f"', out)  # 片段保留
        self.assertIn("https://out.invalid/x", out)  # 外部原样保留

    def test_inline_style_quote_safe(self):
        html = "<div style=\"background: url('a.png')\">x</div>"
        out = rewrite_html(
            html, "https://x.invalid/show/index.html",
            lambda u: "../../store/ab/a.png",
        )
        # 单引号 url 不破坏 HTML 双引号属性
        self.assertIn("url('../../store/ab/a.png')", out)

    def test_stylesheet_classification(self):
        parsed = parse_html(
            '<link rel="stylesheet" href="s.css">'
            '<link rel="prefetch" href="next.html">',
            "https://x.invalid/show/index.html",
        )
        css_ref = next(r for r in parsed.references
                       if r.attribute == "href" and r.raw == "s.css")
        self.assertEqual(css_ref.kind, REF_STYLESHEET)


if __name__ == "__main__":
    unittest.main()
