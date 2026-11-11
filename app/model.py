"""采集包的核心数据模型。

全部状态都以可 JSON 序列化的 dataclass 表示，便于断点续采与清单落盘。
状态字符串集中在本模块定义，避免各处散落魔法值。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

# 采集对象类别
KIND_PAGE = "page"
KIND_RESOURCE = "resource"

# 引用的具体用途（决定入队时按页面还是资源做范围检查）
REF_PAGE = "page"                 # <a>、iframe、meta refresh 等可导航链接
REF_STYLESHEET = "stylesheet"     # <link rel=stylesheet>、CSS @import
REF_RESOURCE = "resource"         # 图片、脚本、字体等静态资源
REF_INLINE_CSS = "inline_css"     # style 属性 / <style> 内的 url()
REF_BASE = "base"                 # <base href>
REF_NON_HTTP = "non_http"         # mailto:、tel:、data: 等不可抓取协议
REF_MEDIA = "media_excluded"      # 音视频，按需求明确不采集

# 单条记录的生命周期状态
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CAPTURED = "captured"
STATUS_FAILED = "failed"
STATUS_DEFERRED = "deferred"       # 预算耗尽，留在可恢复边界上
STATUS_DENIED = "denied"           # 范围外，策略性拒绝
STATUS_EXCLUDED = "excluded"       # 音视频等刻意不采集，不算失败

# 边（依赖关系）的终态
EDGE_CAPTURED = "captured"
EDGE_EXTERNAL = "external"
EDGE_FAILED = "failed"
EDGE_DEFERRED = "deferred"
EDGE_EXCLUDED = "excluded"

# 未采集原因
REASON_PAGE_SCOPE = "out_of_scope_page_prefix"
REASON_ORIGIN_SCOPE = "out_of_scope_resource_origin"
REASON_REDIRECT_LEFT = "redirect_left_scope"
REASON_REDIRECT_LIMIT = "redirect_limit_exceeded"
REASON_FETCH = "fetch_error"
REASON_HTTP_STATUS = "http_error_status"
REASON_BUDGET_PAGES = "budget_max_pages"
REASON_BUDGET_RESOURCES = "budget_max_resources"
REASON_BUDGET_BYTES = "budget_max_bytes"
REASON_BUDGET_DEPTH = "budget_max_depth"
REASON_MEDIA = "media_intentionally_excluded"
REASON_NON_HTTP = "non_http_scheme"
REASON_EXTERNAL_BASE = "external_base_href"
REASON_UNSUPPORTED_REL = "link_rel_not_archived"
REASON_CANCELLED = "cancelled"


@dataclass
class Hop:
    """一次重定向跳：记录该跳的 URL、状态码与 Location。"""

    url: str
    status: int
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Edge:
    """页面/资源之间的一条依赖边（原始引用关系，身份保留）。"""

    source: str                     # 引用所在文档的规范化 URL
    target: str                     # 被引用地址（含片段时为完整形式）
    fetch_url: str                  # 去掉片段后的实际抓取身份
    fragment: str                   # 保留的片段标识，不含 #
    query: str                      # 原始查询串（不丢弃，仅留档）
    kind: str                       # REF_* 之一
    attribute: str                  # 出现位置，如 a.href / img.srcset / css.url
    status: str = EDGE_EXTERNAL
    reason: str | None = None
    local_path: str | None = None   # 若已采集，包内相对路径

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Record:
    """一个 URL 身份的采集记录。不同 URL 相同内容可指向同一个 store 文件。"""

    url: str
    kind: str
    status: str = STATUS_PENDING
    depth: int = 0
    parent: str | None = None
    media_type: str | None = None
    charset: str | None = None
    sha256: str | None = None
    bytes: int = 0
    store_path: str | None = None       # 原始字节的内容寻址路径
    published_path: str | None = None   # 改写后的可读路径（页面/CSS）
    partial_path: str | None = None     # 中断时的分片文件
    attempts: int = 0
    retryable: bool = False
    reason: str | None = None
    hops: list[Hop] = field(default_factory=list)
    shared_with: list[str] = field(default_factory=list)
    final_url: str | None = None        # 若发生域内重定向，记录最终地址

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["hops"] = [h.to_dict() if isinstance(h, Hop) else h for h in self.hops]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        data = dict(data)
        data["hops"] = [Hop(**h) for h in data.get("hops", [])]
        return cls(**data)


@dataclass
class Failure:
    url: str
    kind: str
    reason: str
    retryable: bool
    parent: str | None = None
    detail: str | None = None
    hops: list[Hop] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["hops"] = [h.to_dict() if isinstance(h, Hop) else h for h in self.hops]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Failure":
        data = dict(data)
        data["hops"] = [Hop(**h) for h in data.get("hops", [])]
        return cls(**data)
