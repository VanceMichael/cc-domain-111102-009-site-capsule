"""采集器端到端测试：四大验证场景 + 预算边界 + 断点续采 + 安全路径。"""

import json
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401  隔离模式路径引导（discover 时 tests/ 在 sys.path）

from app.archiver import Archiver
from app.manifest import ArchiveState
from app.model import (
    EDGE_CAPTURED,
    EDGE_EXCLUDED,
    EDGE_EXTERNAL,
    STATUS_CAPTURED,
    STATUS_DEFERRED,
    STATUS_DENIED,
)
from app.offline import check_package
from app.transport import ReplayTransport
from app.wire import Replay

from fixture_support import (
    ORIGIN,
    OUTSIDE,
    css,
    full_routes,
    image,
    png1x,
    write_config,
    write_routes,
)


class ArchiveEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.shared = png1x((200, 30, 30, 255))
        self.other = png1x((30, 60, 200, 255))
        self.routes_path = self.tmp / "routes.json"
        write_routes(self.routes_path, full_routes(self.shared, self.other))
        self.config_path = self.tmp / "scope.json"
        self.config = write_config(self.config_path)
        self.out = self.tmp / "capsule"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, config=None, output=None):
        replay = Replay(self.routes_path)
        return Archiver(
            config or self.config,
            ReplayTransport(replay),
            output or self.out,
        ).run()

    # ---- 场景 1：循环 CSS ----------------------------------------

    def test_circular_css_resolved_without_loop(self):
        state = self._run()
        a = state.get(f"{ORIGIN}/show/a.css")
        b = state.get(f"{ORIGIN}/show/b.css")
        self.assertEqual(a.status, STATUS_CAPTURED)
        self.assertEqual(b.status, STATUS_CAPTURED)
        # 改写后的两份 CSS 都在包内，且互相指向发布副本
        a_text = (self.out / a.published_path).read_text()
        b_text = (self.out / b.published_path).read_text()
        self.assertIn(b.published_path.split("/")[-1], a_text)
        self.assertIn(a.published_path.split("/")[-1], b_text)
        # 不含任何绝对 http 引用
        self.assertNotIn("http://", a_text + b_text)
        self.assertNotIn("https://", a_text + b_text)

    # ---- 场景 2：重定向越界，逐跳检查 -----------------------------

    def test_redirect_leaving_scope_is_denied_hop_by_hop(self):
        state = self._run()
        jump = state.get(f"{ORIGIN}/show/jump.css")
        self.assertEqual(jump.status, STATUS_DENIED)
        self.assertEqual(jump.reason, "out_of_scope_resource_origin")
        # 两跳都留痕，第二跳是越界点
        self.assertEqual(len(jump.hops), 2)
        self.assertTrue(jump.hops[-1].location.startswith(OUTSIDE))
        # 越界目标身份从未进入记录，整站不会被偷偷带入
        self.assertNotIn(f"{OUTSIDE}/whole-site.css", state.records)
        # 失败关系写入清单
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertTrue(any(
            f["reason"] == "redirect_left_scope"
            and f["url"].endswith("jump.css")
            for f in manifest["failures"]
        ))

    # ---- 场景 3：同内容多地址，共用文件但身份保留 ------------------

    def test_identical_content_shares_store_but_keeps_identities(self):
        state = self._run()
        pic = state.get(f"{ORIGIN}/show/pic.png?v=1")
        copy = state.get(f"{ORIGIN}/show/pic-copy.png")
        self.assertEqual(pic.status, STATUS_CAPTURED)
        self.assertEqual(copy.status, STATUS_CAPTURED)
        # 内容相同 -> 同一个 store 文件
        self.assertEqual(pic.store_path, copy.store_path)
        self.assertEqual(pic.sha256, copy.sha256)
        # 但两个 URL 身份都保留，且互相记录共享
        self.assertIn(copy.url, pic.shared_with)
        self.assertIn(pic.url, copy.shared_with)
        # store 里物理文件按内容去重：shared 一份 + tile 一份 + flaky 一份
        store_files = list((self.out / "store").rglob("*.png"))
        self.assertEqual(len(store_files), 3)

    # ---- 场景 4：下载中断后恢复 -----------------------------------

    def test_interrupted_download_recovers_on_retry(self):
        state = self._run()
        flaky = state.get(f"{ORIGIN}/show/flaky.png")
        self.assertEqual(flaky.status, STATUS_CAPTURED)
        # 第一次中断、第二次成功，attempts 至少为 2
        self.assertGreaterEqual(flaky.attempts, 2)
        # 最终字节完整（PNG 签名正确、长度与成功响应一致），分片已清理
        stored = (self.out / flaky.store_path).read_bytes()
        self.assertTrue(stored.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(stored), 12)
        self.assertEqual(list((self.out / "partials").iterdir()), [])

    def test_resume_after_state_reload(self):
        # 第一次运行后，模拟再次启动：直接复用输出目录应幂等完成
        self._run()
        state2 = self._run()
        self.assertEqual(state2.queue, [])
        # 再跑离线核查仍然自洽（除刻意保留的失败关系外无新增问题）
        result = check_package(self.out)
        self.assertTrue(all("manifest.complete" in v for v in result.violations)
                        or result.ok)

    # ---- 范围外引用：页面与资源分别处置 ---------------------------

    def test_out_of_scope_links_explained_not_fetched(self):
        state = self._run()
        # 日历页面不在 page_prefixes，不建记录
        self.assertNotIn(f"{ORIGIN}/calendar/2030", state.records)
        # 边保留为 external 并带原因
        edge = next(e for e in state.edges
                    if e.fetch_url == f"{ORIGIN}/calendar/2030")
        self.assertEqual(edge.status, EDGE_EXTERNAL)
        self.assertEqual(edge.reason, "out_of_scope_page_prefix")
        # 音视频刻意排除
        media = [e for e in state.edges if e.kind == "media_excluded"]
        self.assertTrue(media)
        self.assertTrue(all(e.status == EDGE_EXCLUDED for e in media))

    # ---- 片段保留、查询不丢弃 -------------------------------------

    def test_fragment_kept_and_query_preserved(self):
        state = self._run()
        link = next(e for e in state.edges
                    if e.fetch_url == f"{ORIGIN}/show/page2.html")
        self.assertEqual(link.fragment, "sec")
        self.assertEqual(link.query, "")
        pic = state.get(f"{ORIGIN}/show/pic.png?v=1")
        self.assertEqual(pic.url, f"{ORIGIN}/show/pic.png?v=1")
        # 改写后的页面里片段仍在
        index = state.get(f"{ORIGIN}/show/index.html")
        text = (self.out / index.published_path).read_text()
        self.assertIn("page2.html#sec", text)

    def test_edges_reconciled_to_final_target_status(self):
        state = self._run()
        # 已采集目标的边必须终态为 captured 且带本地路径，不能残留 deferred
        for edge in state.edges:
            target = state.get(edge.fetch_url)
            if target is not None and target.status == STATUS_CAPTURED:
                self.assertEqual(edge.status, EDGE_CAPTURED)
                self.assertIsNotNone(edge.local_path)
        # 清单里同样不再有“目标已采集但边仍 deferred”的矛盾
        manifest = json.loads((self.out / "manifest.json").read_text())
        captured_urls = {
            r["url"] for r in manifest["pages"] + manifest["resources"]
            if r["status"] == STATUS_CAPTURED
        }
        for edge in manifest["edges"]:
            if edge["fetch_url"] in captured_urls:
                self.assertEqual(edge["status"], EDGE_CAPTURED)

    # ---- 预算边界：深度/数量/体积 ---------------------------------

    def test_page_budget_defers_without_losing_state(self):
        config = write_config(self.tmp / "tight.json", max_pages=1)
        state = self._run(config=config, output=self.tmp / "tight")
        statuses = [r.status for r in state.records.values()
                    if r.kind == "page"]
        self.assertIn(STATUS_DEFERRED, statuses)
        # 延期身份仍在队列里，构成可恢复边界
        self.assertTrue(state.queue)
        deferred = [r for r in state.records.values()
                    if r.status == STATUS_DEFERRED]
        self.assertTrue(all(r.reason.startswith("budget_") for r in deferred))
        # 放宽预算后续跑能推进
        config2 = write_config(self.tmp / "loose.json", max_pages=10)
        state2 = Archiver(
            config2, ReplayTransport(Replay(self.routes_path)),
            self.tmp / "tight",
        ).run()
        self.assertEqual(
            sum(1 for r in state2.records.values()
                if r.kind == "page" and r.status == STATUS_CAPTURED),
            2,
        )

    def test_byte_budget_freezes_further_downloads(self):
        # 极小体积预算：第一批文件后其余全部延期，已采内容保留
        config = write_config(self.tmp / "bytes.json", max_bytes=200)
        state = self._run(config=config, output=self.tmp / "bytes")
        self.assertTrue(any(
            r.status == STATUS_DEFERRED and r.reason == "budget_max_bytes"
            for r in state.records.values()
        ))
        # 已采集的 store 文件仍然存在
        self.assertTrue(any((self.tmp / "bytes" / "store").rglob("*")))

    # ---- 远端文件名中的上级目录不能影响输出路径 -------------------

    def test_remote_parent_path_cannot_escape_output(self):
        routes = full_routes(self.shared, self.other)
        routes[f"{ORIGIN}/show/evil.css"] = [
            css("@import \"../../../../etc/secret.css\"; "
                "body{background:url(../../outside.png)}")
        ]
        routes[f"{ORIGIN}/etc/secret.css"] = [css("body{}")]
        routes[f"{ORIGIN}/outside.png"] = [image(self.other)]
        rp = self.tmp / "evil-routes.json"
        write_routes(rp, routes)
        state = Archiver(
            self.config, ReplayTransport(Replay(rp)),
            self.tmp / "evil",
        ).run()
        for record in state.records.values():
            if record.store_path:
                self.assertFalse(record.store_path.startswith("/"))
                self.assertNotIn("..", record.store_path.split("/"))
            if record.published_path:
                self.assertNotIn("..", record.published_path.split("/"))
        # 所有产物都在输出目录内
        for path in (self.tmp / "evil").rglob("*"):
            if path.is_file():
                path.relative_to(self.tmp / "evil")

    # ---- 离线核查报告 --------------------------------------------

    def test_offline_report_lists_external_reasons(self):
        self._run()
        result = check_package(self.out)
        report = (self.out / "offline-report.html").read_text()
        # 报告面向阅读者展示中文原因，三类刻意保留场景都要能解释
        self.assertIn("页面路径超出授权前缀", report)
        self.assertIn("重定向逐跳检查时越界", report)
        self.assertIn("音视频按策略不采集", report)
        # 同时在清单数据里保留机器可读的原因码
        manifest = json.loads((self.out / "manifest.json").read_text())
        edge_reasons = {e.get("reason") for e in manifest["edges"]}
        self.assertIn("out_of_scope_page_prefix", edge_reasons)
        self.assertIn("media_intentionally_excluded", edge_reasons)
        # 存在失败关系时包必须标记不完整
        self.assertFalse(manifest["complete"])


class CompletePackageTests(unittest.TestCase):
    """无失败关系时，清单 complete=true 且离线核查通过。"""

    def test_clean_package_passes_offline_check(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            png = png1x()
            routes = {
                f"{ORIGIN}/show/index.html": [{
                    "status": 200,
                    "headers": {"Content-Type": "text/html; charset=utf-8"},
                    "body": ('<base href="/show/"><link rel="stylesheet" '
                             'href="s.css"><img src="i.png">'
                             '<a href="index.html#top">t</a>'),
                }],
                f"{ORIGIN}/show/s.css": [{
                    "status": 200,
                    "headers": {"Content-Type": "text/css"},
                    "body": "body{background:url(i.png)}",
                }],
                f"{ORIGIN}/show/i.png": [image(png)],
            }
            rp = tmp / "r.json"
            write_routes(rp, routes)
            cfg = write_config(tmp / "c.json")
            out = tmp / "cap"
            Archiver(cfg, ReplayTransport(Replay(rp)), out).run()
            result = check_package(out)
            self.assertEqual(result.violations, [])
            self.assertTrue(result.ok)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertTrue(manifest["complete"])


if __name__ == "__main__":
    unittest.main()
