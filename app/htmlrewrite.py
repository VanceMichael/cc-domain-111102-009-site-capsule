"""HTML 引用提取与本地改写。

设计要点：

* 用正则在原始文本上按位置分词，除属性值替换外字节级保留原文，
  不依赖序列化器重排文档；
* 解析 <base href>，后续相对引用相对该 base 解析；远端 base 不跟随、
  改写时移除该标签并在清单留痕；
* srcset 逐个候选 URL 处理，宽度/密度描述符原样保留；
* 片段（#id）保留在改写后的 href 上；query 不丢弃；
* style 属性与 <style> 块内的 url() 交给 cssrewrite 处理；
* <audio>/<video> 及其 <source> 明确不采集，原标签保留、说明写清单。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from .cssrewrite import extract_css_references, rewrite_css
from .model import (
    REF_BASE,
    REF_INLINE_CSS,
    REF_MEDIA,
    REF_PAGE,
    REF_RESOURCE,
    REF_STYLESHEET,
)
from .scope import is_http_url

_TAG_RE = re.compile(
    r"<!--.*?-->|<!DOCTYPE[^>]*>|<\?.*?\?>|</?(?P<name>[a-zA-Z][\w:-]*)(?P<body>[^>]*)>",
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*(=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))?)?""",
)
_HREF_ATTRS = {"href", "src", "poster", "data", "action"}
_MEDIA_TAGS = {"audio", "video", "track"}
_MEDIA_SOURCE_PARENT = {"audio", "video"}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


@dataclass
class HtmlReference:
    tag: str
    attribute: str
    raw: str                    # 属性里的原始值（srcset 为单个候选）
    resolved: str               # 含片段的完整 URL
    fetch_url: str              # 去片段的抓取身份
    fragment: str
    kind: str                   # REF_*
    descriptor: str = ""        # srcset 的 1x / 800w 描述符


@dataclass
class StyleBlock:
    """<style>...</style> 文本块，供 CSS 改写阶段处理。"""

    start: int
    end: int
    text: str


@dataclass
class ParsedHtml:
    base_href: str | None       # 解析后的有效 base（无 base 标签时为文档 URL）
    base_raw: str | None        # <base href> 的原始值
    base_remote: bool           # base 指向其他 origin
    references: list[HtmlReference]
    style_blocks: list[StyleBlock] = field(default_factory=list)
    base_span: tuple[int, int] | None = None


def _attributes(body: str) -> list[tuple[str, str | None, int, int]]:
    """返回 [(name, value, value_start, value_end)]，位置相对 body；无值属性 value=None。"""

    result = []
    for match in _ATTR_RE.finditer(body):
        name = match.group(1).lower()
        if match.group(2) is None:
            result.append((name, None, -1, -1))
            continue
        if match.group(3) is not None:
            value, group_index = match.group(3), 3
        elif match.group(4) is not None:
            value, group_index = match.group(4), 4
        else:
            value, group_index = match.group(5) or "", 5
        result.append((name, value, match.start(group_index), match.end(group_index)))
    return result


def _classify(tag: str, rel: str | None) -> str:
    """决定一条 HTML 引用按页面、样式表还是普通资源处理。"""

    if tag in ("a", "area", "iframe", "frame", "meta"):
        return REF_PAGE
    if tag == "base":
        return REF_BASE
    if tag == "link":
        rels = set((rel or "").lower().split())
        if "stylesheet" in rels:
            return REF_STYLESHEET
        if rels & {"icon", "preload", "modulepreload", "manifest", "apple-touch-icon"}:
            return REF_RESOURCE
        if rels & {"prefetch", "prerender", "dns-prefetch", "preconnect"}:
            return REF_PAGE
        return REF_RESOURCE
    return REF_RESOURCE


def parse_html(html: str, document_url: str) -> ParsedHtml:
    """提取文档中的全部引用与 style 块，并确定有效 base。"""

    references: list[HtmlReference] = []
    style_blocks: list[StyleBlock] = []
    base_raw: str | None = None
    base_href: str | None = None
    base_span: tuple[int, int] | None = None
    open_stack: list[str] = []

    for tag_match in _TAG_RE.finditer(html):
        name = tag_match.group("name")
        if name is None:
            continue  # 注释、DOCTYPE、处理指令
        tag = name.lower()
        body = tag_match.group("body") or ""
        is_closing = html[tag_match.start():tag_match.start() + 2] == "</"
        attrs = _attributes(body)
        attr_map = {n: v for n, v, _, _ in attrs}

        if tag == "style" and not is_closing:
            close = re.search(r"</style\s*>", html[tag_match.end():], re.IGNORECASE)
            block_end = tag_match.end() + close.start() if close else len(html)
            style_blocks.append(
                StyleBlock(tag_match.end(), block_end, html[tag_match.end():block_end])
            )

        if is_closing:
            if open_stack and open_stack[-1] == tag:
                open_stack.pop()
            continue

        if tag == "base" and base_raw is None:
            href = attr_map.get("href")
            if href:
                base_raw = href.strip()
                base_href = urljoin(document_url, base_raw)
                base_span = tag_match.span()
            continue

        rel = attr_map.get("rel")
        parent = open_stack[-1] if open_stack else ""

        for attr_name, value, _, _ in attrs:
            if not value:
                continue
            if tag == "meta":
                if attr_name == "http-equiv" and value.lower() == "refresh":
                    continue
                if attr_name == "content":
                    target = _meta_refresh_target(value)
                    if target is not None:
                        _append_ref(references, tag, "content", target,
                                    document_url, base_href, REF_PAGE)
                continue
            if attr_name == "srcset":
                for candidate, descriptor in _parse_srcset(value):
                    _append_ref(references, tag, "srcset", candidate,
                                document_url, base_href, REF_RESOURCE,
                                descriptor=descriptor)
                continue
            if attr_name == "style":
                for ref in _inline_style_refs(value, document_url, base_href):
                    references.append(ref)
                continue
            if attr_name not in _HREF_ATTRS:
                continue
            kind = _classify(tag, rel)
            # 音视频的可播放源明确不采集；poster 是封面图片，仍按资源处理
            if attr_name != "poster" and (
                tag in _MEDIA_TAGS
                or (tag == "source" and parent in _MEDIA_SOURCE_PARENT)
            ):
                kind = REF_MEDIA
            _append_ref(references, tag, attr_name, value, document_url,
                        base_href, kind)

        if tag not in _VOID_TAGS:
            open_stack.append(tag)

    effective_base = base_href or document_url
    base_remote = bool(base_href) and _different_origin(base_href, document_url)
    return ParsedHtml(
        base_href=effective_base,
        base_raw=base_raw,
        base_remote=base_remote,
        references=references,
        style_blocks=style_blocks,
        base_span=base_span,
    )


def _different_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme.lower(), pa.netloc.lower()) != (
        pb.scheme.lower(),
        pb.netloc.lower(),
    )


def _append_ref(references, tag, attribute, raw, document_url, base_href,
                kind, descriptor=""):
    base = base_href or document_url
    resolved = urljoin(base, raw.strip())
    fetch_url, fragment = _split_frag(resolved)
    references.append(
        HtmlReference(
            tag=tag,
            attribute=attribute,
            raw=raw.strip(),
            resolved=resolved,
            fetch_url=fetch_url,
            fragment=fragment,
            kind=kind,
            descriptor=descriptor,
        )
    )


def _split_frag(url: str) -> tuple[str, str]:
    if "#" not in url:
        return url, ""
    head, frag = url.split("#", 1)
    return head, frag


def _parse_srcset(value: str) -> list[tuple[str, str]]:
    """解析 srcset，返回 [(候选 URL, 描述符)]。"""

    candidates: list[tuple[str, str]] = []
    for part in value.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        candidates.append((tokens[0], " ".join(tokens[1:])))
    return candidates


def _meta_refresh_target(content: str) -> str | None:
    if "url=" not in content.lower():
        return None
    tail = content[content.lower().index("url=") + 4:].strip()
    if tail[:1] in ("'", '"'):
        quote = tail[0]
        tail = tail[1:].split(quote, 1)[0]
    else:
        tail = tail.split(";", 1)[0].strip()
    return tail or None


def _inline_style_refs(value: str, document_url: str, base_href: str | None):
    base = base_href or document_url
    refs = []
    for css_ref in extract_css_references("*{" + value + "}", base):
        fetch_url, fragment = _split_frag(css_ref.resolved)
        refs.append(
            HtmlReference(
                tag="@style",
                attribute="style",
                raw=css_ref.target,
                resolved=css_ref.resolved,
                fetch_url=fetch_url,
                fragment=fragment,
                kind=REF_INLINE_CSS,
            )
        )
    return refs


def rewrite_html(html: str, document_url: str, resolve_local) -> str:
    """重写整份 HTML。

    resolve_local(fetch_url) -> 包内相对路径 | None。
    未采集的引用保留原绝对 URL（片段拼回），离线核查负责解释其未采集原因。
    远端 <base> 被移除，其相对解析在解析阶段已固化为绝对 URL。
    """

    parsed = parse_html(html, document_url)
    output = html

    # 1) 移除 <base>
    if parsed.base_span is not None:
        start, end = parsed.base_span
        output = (
            output[:start]
            + "<!-- base href resolved and removed by site-capsule -->"
            + output[end:]
        )

    # 2) <style> 块（从后向前替换以保持位置有效）
    for block in sorted(parsed.style_blocks, key=lambda b: b.start, reverse=True):
        rewritten = rewrite_css(
            block.text, parsed.base_href,
            lambda u: _local_or_none(u, resolve_local),
        )
        output = output[:block.start] + rewritten + output[block.end:]

    # 3) 标签属性（从后向前替换；重新扫描输出文本，因为前两步改变了长度）
    replacements: list[tuple[int, int, str]] = []
    for tag_match in _TAG_RE.finditer(output):
        name = tag_match.group("name")
        if name is None or output[tag_match.start():tag_match.start() + 2] == "</":
            continue
        body = tag_match.group("body") or ""
        body_start = tag_match.start(2)
        for attr_name, attr_value, val_start, val_end in _attributes(body):
            if attr_value is None:
                continue
            abs_start, abs_end = body_start + val_start, body_start + val_end
            if attr_name == "srcset":
                new_value = _rewrite_srcset(attr_value, parsed.base_href, resolve_local)
            elif attr_name == "style":
                wrapped = "*{" + attr_value + "}"
                rewritten = rewrite_css(
                    wrapped, parsed.base_href,
                    lambda u: _local_or_none(u, resolve_local),
                )
                new_value = rewritten[2:-1]
            elif attr_name in _HREF_ATTRS:
                resolved = urljoin(parsed.base_href, attr_value.strip())
                new_value = _rewrite_single(resolved, resolve_local)
            else:
                continue
            if new_value != attr_value:
                replacements.append((abs_start, abs_end, new_value))

    for start, end, text in sorted(replacements, key=lambda item: item[0], reverse=True):
        output = output[:start] + text + output[end:]
    return output


def _local_or_none(resolved_url: str, resolve_local):
    fetch_url, _ = _split_frag(resolved_url)
    if not is_http_url(fetch_url):
        return None
    return resolve_local(fetch_url)


def _rewrite_single(resolved: str, resolve_local) -> str:
    fetch_url, fragment = _split_frag(resolved)
    if not is_http_url(fetch_url):
        return resolved  # data:/mailto: 等原样保留
    local = resolve_local(fetch_url)
    if local is None:
        return resolved  # 范围外/失败：保留绝对 URL，清单里写明原因
    return local + (f"#{fragment}" if fragment else "")


def _rewrite_srcset(value: str, base: str, resolve_local) -> str:
    output = []
    for candidate, descriptor in _parse_srcset(value):
        resolved = urljoin(base, candidate)
        fetch_url, _ = _split_frag(resolved)
        local = resolve_local(fetch_url) if is_http_url(fetch_url) else None
        output.append(f"{local or resolved} {descriptor}".strip())
    return ", ".join(output)
