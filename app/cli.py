"""site-capsule 命令行入口。

子命令：

* archive：按授权配置把一组种子页面采集到输出目录，支持断点续采；
* check：离线核查已生成的包并输出报告（全程不联网）；
* inspect：复用最初的只读链接检查工具，打印页面引用结构。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .archiver import Archiver
from .offline import check_package
from .transport import HttpTransport, ReplayTransport
from .wire import Replay


def _load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if "seeds" not in config or not config["seeds"]:
        raise SystemExit("配置缺少 seeds（入口页面 URL 列表）")
    if "page_prefixes" not in config:
        raise SystemExit("配置缺少 page_prefixes（可跟随的页面路径范围）")
    if "resource_origins" not in config:
        raise SystemExit("配置缺少 resource_origins（允许的静态资源 origin）")
    return config


def _build_transport(args):
    if args.replay:
        return ReplayTransport(Replay(args.replay))
    return HttpTransport(timeout=args.timeout)


def cmd_archive(args) -> int:
    config = _load_config(Path(args.config))
    if args.seed:
        config["seeds"] = args.seed
    transport = _build_transport(args)
    archiver = Archiver(config, transport, args.output)
    try:
        state = archiver.run(resume=not args.fresh)
    except KeyboardInterrupt:
        print("采集已取消，状态已保存；再次运行同一命令可从边界继续。",
              file=sys.stderr)
        return 130
    finally:
        transport.close()

    print(f"页面 {state.counters['pages']} 个，"
          f"资源 {state.counters['resources']} 个，"
          f"存储 {state.counters['stored_bytes']} 字节")
    if state.cancelled:
        print("包处于取消后的部分状态，未生成清单。", file=sys.stderr)
        return 2
    result = check_package(args.output)
    print(("离线核查通过。" if result.ok else "离线核查发现问题，详见报告。")
          + f" 报告：{Path(args.output) / 'offline-report.html'}")
    for violation in result.violations:
        print(f"  - {violation}", file=sys.stderr)
    return 0 if result.ok else 3


def cmd_check(args) -> int:
    result = check_package(args.output)
    print(f"检查文件 {result.files_checked} 个，本地引用 "
          f"{result.local_refs_checked} 条，外部保留链接 "
          f"{len(result.external_links)} 条")
    if result.ok:
        print("核查通过：断网打开本地包不会发生未声明的网络访问。")
        return 0
    print("核查未通过：", file=sys.stderr)
    for violation in result.violations:
        print(f"  - {violation}", file=sys.stderr)
    print(f"报告：{Path(args.output) / 'offline-report.html'}", file=sys.stderr)
    return 1


def cmd_inspect(args) -> int:
    from .references import inspect_links

    html_text = Path(args.html).read_text(encoding="utf-8")
    for item in inspect_links(html_text, args.base_url):
        print(json.dumps(item, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="site-capsule",
        description="限域的展览页面与静态资源采集包（不处理音视频）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    archive = sub.add_parser("archive", help="执行采集")
    archive.add_argument("--config", required=True, help="授权与预算配置 JSON")
    archive.add_argument("--output", required=True, help="输出目录")
    archive.add_argument("--seed", action="append",
                         help="追加/覆盖种子 URL，可多次传入")
    archive.add_argument("--replay", help="使用回放路由文件（测试，不联网）")
    archive.add_argument("--timeout", type=float, default=20.0)
    archive.add_argument("--fresh", action="store_true",
                         help="忽略已有 state.json 重新采集")
    archive.set_defaults(func=cmd_archive)

    check = sub.add_parser("check", help="离线核查已生成的包")
    check.add_argument("--output", required=True)
    check.set_defaults(func=cmd_check)

    inspect = sub.add_parser("inspect", help="只读检查单个 HTML 的引用")
    inspect.add_argument("--html", required=True)
    inspect.add_argument("--base-url", required=True)
    inspect.set_defaults(func=cmd_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
