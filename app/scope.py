"""范围配置与 URL 判定。

授权配置分开声明两类范围：

* 页面范围：可跟随的页面 URL 前缀（路径级），决定链接能不能作为页面继续抓；
* 资源范围：允许抓取静态资源的 origin（scheme://host:port），
  资源路径不再加额外前缀限制，但 origin 之外一律拒绝。

任何一次重定向的每一跳都重新走同样的判定，因此“获准资源 302 到整站”
会在越界的那一跳被拦下。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urlsplit, urlunsplit

from .model import (
    KIND_PAGE,
    KIND_RESOURCE,
    REASON_NON_HTTP,
    REASON_ORIGIN_SCOPE,
    REASON_PAGE_SCOPE,
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}
# Windows 保留名与控制字符等，仅用于本地落盘名净化
_UNSAFE_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """规范化用于“身份比较/范围匹配”的 URL。

    刻意保留 path（含大小写）与 query，不做丢弃；仅做：
    scheme/host 小写、去掉默认端口、折叠空 path 为 "/"、去片段。
    片段由调用方另行保存。
    """

    parts = urlsplit(url.strip())
    if parts.scheme:
        scheme = parts.scheme.lower()
    else:
        scheme = "http"
    host = parts.hostname or ""
    host = host.lower().rstrip(".")
    port = parts.port
    default = _DEFAULT_PORTS.get(scheme)
    if port is None or (default and str(port) == default):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    # 不折叠 "."/".." —— 路径语义由 urljoin 解析阶段保证；身份比较保留原样，
    # 避免两个不同写法被错误合并。
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def split_fragment(url: str) -> tuple[str, str]:
    """返回 (去片段 URL, 片段文本不含 #)。片段必须保留在改写后的链接里。"""

    no_frag, frag = urldefrag(url)
    return no_frag, frag


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    default = _DEFAULT_PORTS.get(parts.scheme.lower())
    if port is None or (default and str(port) == default):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    return f"{parts.scheme.lower()}://{netloc}"


def is_http_url(url: str) -> bool:
    return urlsplit(url).scheme.lower() in ("http", "https")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None


@dataclass
class Scope:
    """授权范围。page_prefixes 支持通配；resource_origins 为精确 origin。"""

    page_prefixes: tuple[str, ...]
    resource_origins: frozenset[str]

    @classmethod
    def from_config(cls, config: dict) -> "Scope":
        prefixes = tuple(
            normalize_url(p.rstrip("*")) if p.endswith("*") else normalize_url(p)
            for p in config.get("page_prefixes", [])
        )
        origins = frozenset(
            origin_of(normalize_url(o)) for o in config.get("resource_origins", [])
        )
        return cls(prefixes, origins)

    def check(self, url: str, kind: str) -> Decision:
        """对单个 URL 做一次范围判定（重定向时每跳都调用）。"""

        if not is_http_url(url):
            return Decision(False, REASON_NON_HTTP)
        target = normalize_url(url)
        if kind == KIND_PAGE:
            for prefix in self.page_prefixes:
                # 前缀按路径边界匹配：/show/ 不会授权 /showcase/
                if target == prefix or target.startswith(prefix):
                    return Decision(True)
                # 支持显式通配符配置
                if fnmatch.fnmatch(target, prefix):
                    return Decision(True)
            return Decision(False, REASON_PAGE_SCOPE)
        if kind == KIND_RESOURCE:
            if origin_of(target) in self.resource_origins:
                return Decision(True)
            return Decision(False, REASON_ORIGIN_SCOPE)
        return Decision(False, REASON_PAGE_SCOPE)


def safe_page_relpath(url: str) -> str:
    """把页面 URL 映射为包内 pages/ 下的安全相对路径。

    远端文件名中的 ".." 段会被清空，绝不允许逃出输出目录；
    query 参与短哈希消歧，保证同路径不同查询是不同身份。
    """

    parts = urlsplit(normalize_url(url))
    raw_segments = [s for s in parts.path.split("/") if s not in ("", ".")]
    cleaned: list[str] = []
    for seg in raw_segments:
        seg = _UNSAFE_CHARS.sub("_", seg)
        # 关键：上级目录段直接降级为普通目录名，不做路径回退
        if seg in ("..",):
            seg = "__up__"
        if _WIN_RESERVED.match(seg):
            seg = "_" + seg
        if seg in (".", ""):
            continue
        cleaned.append(seg)
    if parts.path.endswith("/") or not cleaned:
        cleaned.append("index.html")
    elif "." not in cleaned[-1]:
        cleaned[-1] = cleaned[-1] + ".html"
    rel = "/".join(cleaned)
    if parts.query:
        import hashlib

        suffix = hashlib.sha1(parts.query.encode("utf-8")).hexdigest()[:8]
        stem, dot, ext = rel.rpartition(".")
        if dot:
            rel = f"{stem}__q{suffix}.{ext}"
        else:
            rel = f"{rel}__q{suffix}"
    return "pages/" + rel


def safe_store_relpath(sha256: str, media_type: str | None) -> str:
    """内容寻址路径：store/<前2位>/<sha>.<ext>。文件名与远端完全脱钩。"""

    ext = _ext_for_media_type(media_type)
    return f"store/{sha256[:2]}/{sha256}{ext}"


_MEDIA_EXT = {
    "text/html": ".html",
    "text/css": ".css",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/json": ".json",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "application/font-woff": ".woff",
    "application/vnd.ms-fontobject": ".eot",
    "font/ttf": ".ttf",
    "image/x-icon": ".ico",
}


def _ext_for_media_type(media_type: str | None) -> str:
    if not media_type:
        return ".bin"
    base = media_type.split(";", 1)[0].strip().lower()
    return _MEDIA_EXT.get(base, ".bin")


def relative_href(from_relpath: str, to_relpath: str) -> str:
    """计算包内两个相对路径之间的相对引用。"""

    base_parts = from_relpath.split("/")[:-1]
    target_parts = to_relpath.split("/")
    common = 0
    while common < len(base_parts) and common < len(target_parts) - 1:
        if base_parts[common] != target_parts[common]:
            break
        common += 1
    up = [".."] * (len(base_parts) - common)
    down = target_parts[common:]
    return "/".join(up + down) if (up or down) else "."
