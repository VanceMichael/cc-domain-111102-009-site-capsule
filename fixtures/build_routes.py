"""根据 fixtures 目录的合成素材构建回放路由 demo-routes.json。

覆盖需求点名的四类验证场景：

1. 循环 CSS：style.css 与 nested.css 互相 @import；
2. 重定向越界：/show/jump.css 逐跳 302 到未授权 origin；
3. 同内容多地址：image.png?size=s 与 image-large.png 字节完全相同；
4. 下载中断：flaky.png 首次响应先吐部分字节再断开，二次成功。

另含页面前缀越界（/calendar/2030）、资源 origin 越界（outside.invalid）、
带 query 的同路径多身份、音视频排除、外部 base、mailto/data 协议等边角。
脚本只读写本地文件，不发起网络请求。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"
ORIGIN = "https://exhibition.invalid"
OUTSIDE = "https://outside.invalid"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def html_entry(body: str, status: int = 200) -> dict:
    return {
        "status": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": body,
    }


def css_entry(body: str) -> dict:
    return {
        "status": 200,
        "headers": {"Content-Type": "text/css; charset=utf-8"},
        "body": body,
    }


def png_entry(data: bytes) -> dict:
    return {
        "status": 200,
        "headers": {"Content-Type": "image/png"},
        "body": b64(data),
        "base64": True,
    }


INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<base href="/show/">
<link rel="stylesheet" href="style.css">
<link rel="icon" href="/favicon.ico">
</head><body>
<h1 id="top">合成艺术展</h1>
<img src="image.png?size=s" alt="主视觉"
     srcset="image.png?size=s 1x, image-large.png 2x">
<p>背景块：<span class="hero">hero</span></p>
<div style="background: url('tile-blue.png') repeat;">瓷砖纹理</div>
<nav>
  <a href="other.html#a">展品页（片段保留）</a>
  <a href="other.html?lang=en#a">同路径不同查询的另一身份</a>
  <a href="/calendar/2030">日历（页面范围外，越界不跟随）</a>
  <a href="https://outside.invalid/whole-site">外部整站链接</a>
  <a href="mailto:curator@exhibition.invalid">联系邮箱</a>
  <a href="detail.html#note">详情页（深度/预算边界）</a>
</nav>
<link rel="stylesheet" href="jump.css">
<video controls poster="image.png?size=s" src="/media/tour.mp4"></video>
<audio src="/media/guide.mp3"></audio>
<img src="flaky.png" alt="中断后可恢复的图片">
</body></html>
"""

OTHER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<link rel="stylesheet" href="style.css"></head>
<body>
<h1 id="a">合成展品</h1>
<a href="index.html#top">返回（片段保留）</a>
<img srcset="image.png?size=s 1x, image-large.png 2x" src="image.png?size=s">
<meta http-equiv="refresh" content="0; url=index.html#top">
</body></html>
"""

DETAIL_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"></head>
<body>
<h1 id="note">展品详情</h1>
<a href="index.html#top">返回</a>
<img src="tile-blue.png" alt="细节图">
</body></html>
"""

BASED_HTML = """<!doctype html>
<html><head><base href="https://cdn.invalid/assets/"></head>
<body><img src="solo.png" alt="外部 base 上的图片"></body></html>
"""

STYLE_CSS = '@import "nested.css";\n.hero { background: url(image.png?size=s); }\n'
NESTED_CSS = '@import "style.css";\n.small { background: url(../outside.png); }\n'


def main() -> None:
    image_png = (FIX / "image.png").read_bytes()
    large_png = (FIX / "image-large.png").read_bytes()
    blue_png = (FIX / "tile-blue.png").read_bytes()
    gray_png = (FIX / "outside.png").read_bytes()

    routes: dict[str, list] = {
        f"{ORIGIN}/show/index.html": [html_entry(INDEX_HTML)],
        f"{ORIGIN}/show/other.html": [html_entry(OTHER_HTML)],
        f"{ORIGIN}/show/detail.html": [html_entry(DETAIL_HTML)],
        f"{ORIGIN}/show/based.html": [html_entry(BASED_HTML)],
        f"{ORIGIN}/show/style.css": [css_entry(STYLE_CSS)],
        f"{ORIGIN}/show/nested.css": [css_entry(NESTED_CSS)],
        # 查询串不丢弃：两个查询身份各自登记，内容相同时共用 store 文件
        f"{ORIGIN}/show/image.png?size=s": [png_entry(image_png)],
        f"{ORIGIN}/show/image-large.png": [png_entry(large_png)],
        f"{ORIGIN}/show/tile-blue.png": [png_entry(blue_png)],
        # 根 origin 授权但路径在 show 之外，作为资源 origin 内文件仍可抓
        f"{ORIGIN}/favicon.ico": [
            {
                "status": 200,
                "headers": {"Content-Type": "image/x-icon"},
                "body": b64(b"\x00\x00\x01\x00FAKEICON"),
                "base64": True,
            }
        ],
        # CSS 中引用的上级路径，验证远端文件名里的 .. 不能逃出输出目录
        f"{ORIGIN}/outside.png": [png_entry(gray_png)],
        # 场景 2：域内一跳，再跳到未授权 origin
        f"{ORIGIN}/show/jump.css": [
            {
                "status": 302,
                "headers": {"Location": "/redirect/second"},
            }
        ],
        f"{ORIGIN}/redirect/second": [
            {
                "status": 302,
                "headers": {"Location": f"{OUTSIDE}/whole-site.css"},
            }
        ],
        f"{OUTSIDE}/whole-site.css": [css_entry("body{color:red}")],
        f"{OUTSIDE}/whole-site": [html_entry("<h1>never archived</h1>")],
        # 场景 4：首次流中断，二次成功
        f"{ORIGIN}/show/flaky.png": [
            {
                "status": 200,
                "headers": {"Content-Type": "image/png"},
                "body": b64(blue_png),
                "base64": True,
                "stream_failure": {"after_bytes": 20},
            },
            png_entry(blue_png),
        ],
        # 404 页面（错误状态要进失败关系）
        f"{ORIGIN}/show/missing.html": [
            {"status": 404, "headers": {}, "body": "not found"}
        ],
        # 外部 base 指向的资源（origin 未授权）
        "https://cdn.invalid/assets/solo.png": [png_entry(gray_png)],
        # 日历路径不在 page_prefixes 内
        f"{ORIGIN}/calendar/2030": [
            html_entry("<h1>calendar</h1>")
        ],
    }

    out = FIX / "demo-routes.json"
    out.write_text(json.dumps(routes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out.relative_to(ROOT)}，共 {len(routes)} 条路由")


if __name__ == "__main__":
    main()
