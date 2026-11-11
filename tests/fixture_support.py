"""测试辅助：在临时目录生成回放路由与授权配置。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

ORIGIN = "https://exhibition.invalid"
OUTSIDE = "https://outside.invalid"


def png1x(rgba=(10, 20, 30, 255)) -> bytes:
    import struct
    import zlib

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    row = b"\x00" + bytes(rgba)
    idat = zlib.compress(row, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def html(body):
    return {"status": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": body}


def css(body):
    return {"status": 200,
            "headers": {"Content-Type": "text/css; charset=utf-8"},
            "body": body}


def image(data):
    return {"status": 200,
            "headers": {"Content-Type": "image/png"},
            "body": base64.b64encode(data).decode("ascii"),
            "base64": True}


def write_config(path: Path, **overrides) -> dict:
    config = {
        "seeds": [f"{ORIGIN}/show/index.html"],
        "page_prefixes": [f"{ORIGIN}/show/"],
        "resource_origins": [ORIGIN],
        "max_pages": 10,
        "max_resources": 50,
        "max_bytes": 1_000_000,
        "max_depth": 3,
    }
    config.update(overrides)
    path.write_text(json.dumps(config), encoding="utf-8")
    return config


def full_routes(shared_png: bytes, other_png: bytes) -> dict:
    """覆盖循环 CSS / 越界重定向 / 同内容多地址 / 中断 / 越界页面的路由。"""

    flaky_png = png1x((90, 90, 90, 255))  # 独立字节，避免与其他图片内容相同
    index = f"""<!doctype html><html><head><meta charset="utf-8">
<base href="/show/">
<link rel="stylesheet" href="a.css">
</head><body>
<img src="pic.png?v=1" srcset="pic.png?v=1 1x, pic-copy.png 2x">
<div style="background: url('tile.png')"></div>
<a href="page2.html#sec">第二页</a>
<a href="/calendar/2030">越界日历</a>
<a href="{OUTSIDE}/whole">外部链接</a>
<link rel="stylesheet" href="jump.css">
<video src="/media/tour.mp4" poster="pic.png?v=1"></video>
<img src="flaky.png">
</body></html>"""
    page2 = ('<!doctype html><html><body><a href="index.html#top">返回</a>'
             '<img src="tile.png"></body></html>')
    return {
        f"{ORIGIN}/show/index.html": [html(index)],
        f"{ORIGIN}/show/page2.html": [html(page2)],
        # 循环 CSS：a.css 引 b.css，b.css 引回 a.css
        f"{ORIGIN}/show/a.css": [
            css('@import "b.css"; .a { background: url(pic.png?v=1); }')
        ],
        f"{ORIGIN}/show/b.css": [
            css('@import "a.css"; .b { background: url(tile.png); }')
        ],
        # 同内容多地址：pic.png?v=1 与 pic-copy.png 字节完全相同
        f"{ORIGIN}/show/pic.png?v=1": [image(shared_png)],
        f"{ORIGIN}/show/pic-copy.png": [image(shared_png)],
        f"{ORIGIN}/show/tile.png": [image(other_png)],
        # 逐跳重定向：域内一跳 -> 未授权 origin
        f"{ORIGIN}/show/jump.css": [
            {"status": 302, "headers": {"Location": "/redirect/hop2"}}
        ],
        f"{ORIGIN}/redirect/hop2": [
            {"status": 302,
             "headers": {"Location": f"{OUTSIDE}/whole-site.css"}}
        ],
        f"{OUTSIDE}/whole-site.css": [css("body{}")],
        f"{OUTSIDE}/whole": [html("<h1>x</h1>")],
        f"{ORIGIN}/calendar/2030": [html("<h1>cal</h1>")],
        # 下载中断：先吐部分字节再失败，第二次成功
        f"{ORIGIN}/show/flaky.png": [
            {"status": 200, "headers": {"Content-Type": "image/png"},
             "body": base64.b64encode(flaky_png).decode("ascii"),
             "base64": True,
             "stream_failure": {"after_bytes": 12}},
            image(flaky_png),
        ],
    }


def write_routes(path: Path, routes: dict) -> None:
    path.write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")
