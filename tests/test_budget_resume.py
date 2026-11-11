"""深度预算与取消恢复的专项测试。"""

import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401

from app.archiver import Archiver
from app.manifest import ArchiveState
from app.model import STATUS_CAPTURED, STATUS_DEFERRED, STATUS_PENDING
from app.transport import ReplayTransport
from app.wire import Replay

from fixture_support import ORIGIN, html, write_config, write_routes


class DepthBudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _chain(self):
        names = ["d0", "d1", "d2", "d3"]
        routes = {}
        for i, name in enumerate(names):
            nxt = names[i + 1] if i + 1 < len(names) else None
            body = f'<a href="{nxt}.html">n</a>' if nxt else "<p>end</p>"
            routes[f"{ORIGIN}/show/{name}.html"] = [html(body)]
        return names, routes

    def test_depth_boundary_defers_then_resumes(self):
        names, routes = self._chain()
        rp = self.tmp / "r.json"
        write_routes(rp, routes)
        cfg = write_config(self.tmp / "c.json", max_depth=2)
        cfg["seeds"] = [f"{ORIGIN}/show/d0.html"]
        out = self.tmp / "cap"
        state = Archiver(cfg, ReplayTransport(Replay(rp)), out).run()
        # d0..d2 采集（深度 0/1/2），d3 超深度延期
        for name in ("d0", "d1", "d2"):
            self.assertEqual(
                state.get(f"{ORIGIN}/show/{name}.html").status,
                STATUS_CAPTURED,
            )
        d3 = state.get(f"{ORIGIN}/show/d3.html")
        self.assertEqual(d3.status, STATUS_DEFERRED)
        self.assertEqual(d3.reason, "budget_max_depth")
        # 延期身份保留在队列，构成可恢复边界
        self.assertIn(f"{ORIGIN}/show/d3.html", state.queue)

        # 放宽深度后续跑，d3 被采集，边界推进完成
        cfg2 = write_config(self.tmp / "c2.json", max_depth=5)
        cfg2["seeds"] = [f"{ORIGIN}/show/d0.html"]
        state2 = Archiver(cfg2, ReplayTransport(Replay(rp)), out).run()
        self.assertEqual(
            state2.get(f"{ORIGIN}/show/d3.html").status,
            STATUS_CAPTURED,
        )
        self.assertEqual(state2.queue, [])


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cancelled_run_persists_resumable_state(self):
        names = ["d0", "d1", "d2"]
        routes = {}
        for i, name in enumerate(names):
            nxt = names[i + 1] if i + 1 < len(names) else None
            body = f'<a href="{nxt}.html">n</a>' if nxt else "<p>end</p>"
            routes[f"{ORIGIN}/show/{name}.html"] = [html(body)]
        rp = self.tmp / "r.json"
        write_routes(rp, routes)
        cfg = write_config(self.tmp / "c.json")
        cfg["seeds"] = [f"{ORIGIN}/show/d0.html"]
        out = self.tmp / "cap"

        # 在首个页面处理完成后的检查点抛出 KeyboardInterrupt，模拟取消
        def cancel_after_first(state):
            if state.counters["pages"] >= 1:
                raise KeyboardInterrupt

        archiver = Archiver(
            cfg, ReplayTransport(Replay(rp)), out,
            on_checkpoint=cancel_after_first,
        )
        with self.assertRaises(KeyboardInterrupt):
            archiver.run()

        # state.json 已落盘，可加载，标记取消
        state = ArchiveState.load(out / "state.json")
        self.assertTrue(state.cancelled)
        captured = [u for u, r in state.records.items()
                    if r.status == STATUS_CAPTURED]
        self.assertTrue(captured)
        # 未完成身份仍在队列里
        self.assertTrue(state.queue)

        # 再次运行可继续，最终全部完成
        state2 = Archiver(cfg, ReplayTransport(Replay(rp)), out).run()
        for name in names:
            self.assertEqual(
                state2.get(f"{ORIGIN}/show/{name}.html").status,
                STATUS_CAPTURED,
            )
        self.assertEqual(state2.queue, [])
        self.assertFalse(state2.cancelled)


if __name__ == "__main__":
    unittest.main()
