"""CSS 引用提取与本地改写。

覆盖：

* @import "..." / @import url(...)（媒体查询后缀保留）；
* url(...) 的三种引号形式与无引号形式；
* 注释中的 url 不参与采集；
* 数据 URI（data:、blob:）原样保留。

循环引用（A @import B，B @import A）不会在解析层成环：
采集器按 URL 身份去重抓取，改写阶段只是逐文件替换字符串。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

_URL_RE = re.compile(
    r"""url\(\s*(?P<quote>['"]?)(?P<target>[^'")]+?)(?P=quote)\s*\)""",
    re.IGNORECASE,
)
_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*(?P<q1>['"]?)(?P<u1>[^'")]+?)(?P=q1)\s*\)"""
    r"""|(?P<q2>['"])(?P<u2>[^'"]+?)(?P=q2))""",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass
class CssReference:
    target: str           # 原始 token 文本
    resolved: str         # 相对 CSS 文档解析后的完整 URL（未去片段）
    kind: str             # "import" 或 "url"
    span: tuple[int, int]


def _mask_comments(css: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _COMMENT_RE.finditer(css)]


def _in_span(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def extract_css_references(css_text: str, base_url: str) -> list[CssReference]:
    """列出 CSS 内的全部外部引用（注释中的除外）。"""

    comments = _mask_comments(css_text)
    found: list[CssReference] = []
    for match in _IMPORT_RE.finditer(css_text):
        if _in_span(match.start(), comments):
            continue
        target = match.group("u1") or match.group("u2")
        found.append(
            CssReference(target, urljoin(base_url, target), "import", match.span())
        )
    for match in _URL_RE.finditer(css_text):
        if _in_span(match.start(), comments):
            continue
        target = match.group("target")
        # 避免与 @import url(...) 重复登记
        if any(start <= match.start() < end for start, end in
               [ref.span for ref in found]):
            continue
        found.append(
            CssReference(target, urljoin(base_url, target), "url", match.span())
        )
    found.sort(key=lambda ref: ref.span[0])
    return found


def rewrite_css(css_text: str, base_url: str, resolve_local) -> str:
    """把 CSS 内可本地化的引用替换为包内相对路径。

    resolve_local(resolved_url) -> 包内相对路径 | None；
    返回 None（未采集/范围外）时保留原绝对 URL，由离线核查解释原因。
    """

    references = extract_css_references(css_text, base_url)
    if not references:
        return css_text
    pieces: list[str] = []
    cursor = 0
    for ref in references:
        start, end = ref.span
        pieces.append(css_text[cursor:start])
        token = css_text[start:end]
        local = resolve_local(ref.resolved)
        if local is None:
            pieces.append(token)
        elif ref.kind == "import":
            # 媒体查询与分号在匹配跨度之外，保留原文衔接
            pieces.append(f'@import url("{local}")')
        else:
            pieces.append(_rebuild_url(token, local))
        cursor = end
    pieces.append(css_text[cursor:])
    return "".join(pieces)


def _rebuild_url(token: str, local: str) -> str:
    match = _URL_RE.fullmatch(token)
    # 用单引号，安全嵌入 HTML 双引号属性（内联 style）
    return f"url('{local}')" if match else token
