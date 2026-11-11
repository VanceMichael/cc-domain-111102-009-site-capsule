"""限域采集编排器。

采集分两个阶段：

1. 抓取阶段：从种子 URL 出发做 BFS。页面链接按“页面前缀”授权，
   静态资源按“资源 origin”授权；重定向每一跳重新过范围检查。
   原始字节以内容寻址写入 store/，不同 URL 相同内容自然共用一个文件，
   但各自的 URL 身份、来源边与媒体类型仍分别记账。
2. 发布阶段：对已采集的 HTML/CSS 做本地引用改写，输出可读镜像；
   未采集的引用保留绝对 URL，原因写入清单，绝不静默联网。

预算（深度/页面数/资源数/字节数）耗尽时不丢状态：剩余身份标记
deferred 并保留在队列里，下次运行可从边界继续。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import model
from .cssrewrite import extract_css_references, rewrite_css
from .htmlrewrite import parse_html, rewrite_html
from .manifest import (
    CSS_DIR,
    PARTIALS_DIR,
    STORE_DIR,
    ArchiveState,
    utc_now,
)
from .model import (
    EDGE_CAPTURED,
    EDGE_DEFERRED,
    EDGE_EXCLUDED,
    EDGE_EXTERNAL,
    EDGE_FAILED,
    KIND_PAGE,
    KIND_RESOURCE,
    REF_BASE,
    REF_INLINE_CSS,
    REF_MEDIA,
    REF_NON_HTTP,
    REF_PAGE,
    REF_STYLESHEET,
    STATUS_CAPTURED,
    STATUS_DEFERRED,
    STATUS_DENIED,
    STATUS_EXCLUDED,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
)
from .scope import (
    Scope,
    is_http_url,
    normalize_url,
    relative_href,
    safe_page_relpath,
    safe_store_relpath,
    split_fragment,
)
from .transport import TransportError, parse_content_type

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 3
CHUNK_SIZE = 64 * 1024


class Budget:
    def __init__(self, config: dict):
        self.max_pages = int(config.get("max_pages", 50))
        self.max_resources = int(config.get("max_resources", 200))
        self.max_bytes = int(config.get("max_bytes", 100 * 1024 * 1024))
        self.max_depth = int(config.get("max_depth", 4))

    def page_allowed(self, state: ArchiveState) -> bool:
        return state.counters["pages"] < self.max_pages

    def resource_allowed(self, state: ArchiveState) -> bool:
        return state.counters["resources"] < self.max_resources

    def bytes_allowed(self, state: ArchiveState, incoming: int = 0) -> bool:
        return state.counters["stored_bytes"] + incoming <= self.max_bytes


class Archiver:
    def __init__(self, config: dict, transport, output_dir: str | Path,
                 on_checkpoint=None):
        self.config = config
        self.scope = Scope.from_config(config)
        self.budget = Budget(config)
        self.transport = transport
        self.output_dir = Path(output_dir)
        self.on_checkpoint = on_checkpoint or (lambda state: None)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / STORE_DIR).mkdir(exist_ok=True)
        (self.output_dir / PARTIALS_DIR).mkdir(exist_ok=True)

    # ============================================================
    # 入口
    # ============================================================

    def run(self, resume: bool = True) -> ArchiveState:
        state = self._load_or_init(resume)
        # 续采时清理上次留下的分片
        partials = self.output_dir / PARTIALS_DIR
        if partials.exists():
            for item in partials.iterdir():
                item.unlink(missing_ok=True)
        try:
            self._crawl(state)
        except KeyboardInterrupt:
            state.cancelled = True
            for record in state.records.values():
                if record.status == STATUS_IN_PROGRESS:
                    record.status = STATUS_PENDING
                    record.reason = model.REASON_CANCELLED
                    if record.url not in state.queue:
                        state.queue.append(record.url)
            state.save()
            raise
        if not state.cancelled:
            self._finalize(state)
        state.save()
        return state

    def _load_or_init(self, resume: bool) -> ArchiveState:
        state_path = self.output_dir / "state.json"
        if resume and state_path.exists():
            state = ArchiveState.load(state_path)
            state.cancelled = False
            # 续采：把上次预算耗尽留下的延期身份重新激活，从边界继续
            for record in state.records.values():
                if record.status == STATUS_DEFERRED:
                    record.status = STATUS_PENDING
                    record.reason = None
                    if record.url not in state.queue:
                        state.queue.append(record.url)
            return state
        state = ArchiveState(
            output_dir=str(self.output_dir),
            seeds=[normalize_url(u) for u in self.config["seeds"]],
        )
        for seed in state.seeds:
            decision = self.scope.check(seed, KIND_PAGE)
            if not decision.allowed:
                state.add_failure(model.Failure(
                    url=seed, kind=KIND_PAGE, reason=decision.reason,
                    retryable=False, detail="种子页面不在授权页面范围内",
                ))
                continue
            self._enqueue(state, seed, KIND_PAGE, depth=0, parent=None)
        state.save()
        return state

    # ============================================================
    # 抓取阶段
    # ============================================================

    def _crawl(self, state: ArchiveState) -> None:
        while state.queue:
            url = state.queue.pop(0)
            record = state.get(url)
            if record is None or record.status in (
                STATUS_CAPTURED, STATUS_DENIED, STATUS_EXCLUDED
            ):
                continue
            # 已延期的身份留在队列里构成可恢复边界，本次运行不再重复处理，
            # 否则预算耗尽后会“取出-延期”死循环
            if record.status == STATUS_DEFERRED:
                state.queue.append(url)
                break
            self._process(state, record)
            state.save()
            self.on_checkpoint(state)

    def _process(self, state: ArchiveState, record: model.Record) -> None:
        # 预算边界检查：超限身份留在队列并标记 deferred
        if record.kind == KIND_PAGE and not self.budget.page_allowed(state):
            self._defer(state, record, model.REASON_BUDGET_PAGES)
            return
        if record.kind == KIND_RESOURCE and not self.budget.resource_allowed(state):
            self._defer(state, record, model.REASON_BUDGET_RESOURCES)
            return
        if record.depth > self.budget.max_depth:
            self._defer(state, record, model.REASON_BUDGET_DEPTH)
            return

        record.status = STATUS_IN_PROGRESS
        record.attempts += 1
        try:
            stream = self._open_with_redirects(state, record)
        except _ScopeExit as exc:
            self._deny_redirect(state, record, exc)
            return
        except (TransportError, OSError) as exc:
            self._retry_or_fail(state, record, model.REASON_FETCH, str(exc))
            return

        try:
            status = stream.status
            if status >= 400:
                stream.close()
                self._retry_or_fail(
                    state, record, model.REASON_HTTP_STATUS,
                    f"HTTP {status}", retryable=False,
                )
                return
            if status in REDIRECT_STATUSES:
                stream.close()
                self._retry_or_fail(
                    state, record, model.REASON_REDIRECT_LIMIT,
                    "重定向次数耗尽且缺少 Location", retryable=False,
                )
                return
            media_type, charset = parse_content_type(
                stream.headers.get("Content-Type")
                or stream.headers.get("content-type")
            )
            sha, size, partial = self._drain(stream, record)
        except (TransportError, OSError) as exc:
            self._retry_or_fail(state, record, model.REASON_FETCH, str(exc),
                                partial_kept=True)
            return
        finally:
            try:
                stream.close()
            except Exception:
                pass

        # 体积预算：先落盘再判定；超限则保留已采内容、冻结后续抓取
        store_rel = safe_store_relpath(sha, media_type)
        store_abs = self.output_dir / store_rel
        already_stored = store_abs.exists()
        if not already_stored:
            store_abs.parent.mkdir(parents=True, exist_ok=True)
            partial.rename(store_abs)
        else:
            # 相同内容已在包内：删除本次分片，直接共用内容寻址文件
            partial.unlink(missing_ok=True)
        record.sha256 = sha
        record.bytes = size
        record.media_type = media_type
        record.charset = charset
        record.store_path = store_rel
        record.status = STATUS_CAPTURED
        record.reason = None
        record.partial_path = None
        if not already_stored:
            state.counters["stored_bytes"] += size
        self._count_capture(state, record)

        # 内容去重：同 SHA 已存在则共享，不重复占字节
        if already_stored:
            for other in state.records.values():
                if (other is not record and other.sha256 == sha
                        and other.store_path == store_rel
                        and record.url not in other.shared_with):
                    other.shared_with.append(record.url)
                    record.shared_with.append(other.url)

        if not self.budget.bytes_allowed(state):
            # 体积预算耗尽：当前文件保留，队列其余全部延期
            for queued in list(state.queue):
                other = state.get(queued)
                if other and other.status == STATUS_PENDING:
                    self._defer(state, other, model.REASON_BUDGET_BYTES)

        self._extract_references(state, record, store_abs, media_type, charset)

    # ---------- 重定向：逐跳授权 ----------

    def _open_with_redirects(self, state, record: model.Record):
        """手动跟随重定向，每一跳都按当前身份的类别重新过范围检查。"""

        current = record.url
        hops: list[model.Hop] = []
        for _ in range(MAX_REDIRECTS + 1):
            stream = self.transport.open(current)
            status = stream.status
            if status not in REDIRECT_STATUSES:
                record.hops = hops
                record.final_url = current
                return stream
            location = stream.headers.get("Location") or stream.headers.get("location")
            stream.close()
            if not location:
                raise TransportError(f"{current} 返回 {status} 但缺少 Location")
            next_url = normalize_url(urljoin(current, location))
            hops.append(model.Hop(url=current, status=status, location=next_url))
            # 关键：每一跳独立授权，获准资源不能借 302 把整站带进来
            decision = self.scope.check(next_url, record.kind)
            if not decision.allowed:
                raise _ScopeExit(next_url, decision.reason, hops)
            current = next_url
        raise TransportError("重定向次数超过上限")

    def _deny_redirect(self, state, record: model.Record, exc: "_ScopeExit") -> None:
        record.status = STATUS_DENIED
        record.reason = exc.reason
        record.hops = exc.hops
        state.add_failure(model.Failure(
            url=record.url, kind=record.kind, reason=model.REASON_REDIRECT_LEFT,
            retryable=False, parent=record.parent,
            detail=f"重定向在跳 {exc.exit_url} 越界（{exc.reason}）",
            hops=list(exc.hops),
        ))
        self._fail_edges(state, record, model.REASON_REDIRECT_LEFT)

    # ---------- 下载落盘 ----------

    def _drain(self, stream, record: model.Record):
        """流式落盘到 partials/。

        成功返回 (sha, size, partial_path)；读取中断时异常向上传播，
        已写入的分片保留在磁盘上，由调用方决定重试或失败。
        """

        digest = hashlib.sha256()
        size = 0
        partial_name = f"{record.kind}-{hashlib.sha1(record.url.encode()).hexdigest()[:16]}.part"
        partial_path = self.output_dir / PARTIALS_DIR / partial_name
        record.partial_path = str(partial_path.relative_to(self.output_dir))
        with partial_path.open("wb") as handle:
            for chunk in stream.iter_chunks(CHUNK_SIZE):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size, partial_path

    # ---------- 引用提取与入队 ----------

    def _extract_references(self, state, record: model.Record,
                            store_abs: Path, media_type: str | None,
                            charset: str | None) -> None:
        is_html = record.kind == KIND_PAGE or (media_type or "").endswith("html")
        is_css = (media_type or "") in ("text/css",) or record.url.split("?")[0].endswith(".css")
        if not (is_html or is_css):
            return
        raw = store_abs.read_bytes()
        text_encoding = charset or "utf-8"
        try:
            text = raw.decode(text_encoding, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")

        if is_html:
            parsed = parse_html(text, record.url)
            if parsed.base_raw is not None:
                edge_kind = REF_BASE
                if parsed.base_remote:
                    state.add_edge(model.Edge(
                        source=record.url, target=parsed.base_href,
                        fetch_url=normalize_url(parsed.base_href),
                        fragment="", query=urlsplit(parsed.base_href).query,
                        kind=edge_kind, attribute="base.href",
                        status=EDGE_EXTERNAL,
                        reason=model.REASON_EXTERNAL_BASE,
                    ))
            for ref in parsed.references:
                self._handle_reference(state, record, ref.kind, ref.attribute,
                                       ref.resolved, ref.fetch_url, ref.fragment)
        else:
            for css_ref in extract_css_references(text, record.url):
                kind = REF_STYLESHEET if css_ref.kind == "import" else REF_INLINE_CSS
                fetch_url, fragment = split_fragment(css_ref.resolved)
                self._handle_reference(
                    state, record, kind,
                    "css.@import" if css_ref.kind == "import" else "css.url",
                    css_ref.resolved, fetch_url, fragment,
                )

    def _handle_reference(self, state, record: model.Record, ref_kind: str,
                          attribute: str, resolved: str, fetch_url: str,
                          fragment: str) -> None:
        query = urlsplit(fetch_url).query

        # 非 HTTP 协议：刻意保留，不抓取
        if not is_http_url(fetch_url):
            state.add_edge(model.Edge(
                source=record.url, target=resolved, fetch_url=fetch_url,
                fragment=fragment, query=query, kind=REF_NON_HTTP,
                attribute=attribute, status=EDGE_EXCLUDED,
                reason=model.REASON_NON_HTTP,
            ))
            return

        # 音视频：明确不采集，标签与外部地址保留
        if ref_kind == REF_MEDIA:
            state.add_edge(model.Edge(
                source=record.url, target=resolved, fetch_url=fetch_url,
                fragment=fragment, query=query, kind=REF_MEDIA,
                attribute=attribute, status=EDGE_EXCLUDED,
                reason=model.REASON_MEDIA,
            ))
            return

        target_kind = KIND_PAGE if ref_kind == REF_PAGE else KIND_RESOURCE
        decision = self.scope.check(fetch_url, target_kind)
        if not decision.allowed:
            state.add_edge(model.Edge(
                source=record.url, target=resolved, fetch_url=fetch_url,
                fragment=fragment, query=query, kind=ref_kind,
                attribute=attribute, status=EDGE_EXTERNAL,
                reason=decision.reason,
            ))
            return

        existing = state.get(fetch_url)
        if existing is not None:
            self._edge_to_record(state, record, resolved, fetch_url,
                                 fragment, query, ref_kind, attribute, existing)
            return

        new_record = self._enqueue(
            state, fetch_url, target_kind,
            depth=record.depth + (1 if target_kind == KIND_PAGE else 0),
            parent=record.url,
        )
        self._edge_to_record(state, record, resolved, fetch_url, fragment,
                             query, ref_kind, attribute, new_record)

    def _edge_to_record(self, state, record, resolved, fetch_url, fragment,
                        query, ref_kind, attribute, target: model.Record) -> None:
        status_map = {
            STATUS_CAPTURED: EDGE_CAPTURED,
            STATUS_PENDING: EDGE_DEFERRED,
            STATUS_IN_PROGRESS: EDGE_DEFERRED,
            STATUS_DEFERRED: EDGE_DEFERRED,
            STATUS_DENIED: EDGE_EXTERNAL,
            STATUS_FAILED: EDGE_FAILED,
            STATUS_EXCLUDED: EDGE_EXCLUDED,
        }
        state.add_edge(model.Edge(
            source=record.url, target=resolved, fetch_url=fetch_url,
            fragment=fragment, query=query, kind=ref_kind,
            attribute=attribute,
            status=status_map.get(target.status, EDGE_EXTERNAL),
            reason=target.reason,
            local_path=target.store_path if target.status == STATUS_CAPTURED else None,
        ))

    def _enqueue(self, state, url: str, kind: str, depth: int,
                 parent: str | None) -> model.Record:
        normalized = normalize_url(url)
        existing = state.get(normalized)
        if existing is not None:
            return existing
        record = model.Record(url=normalized, kind=kind, depth=depth, parent=parent)
        state.upsert(record)
        state.queue.append(normalized)
        return record

    # ---------- 失败与预算 ----------

    def _retry_or_fail(self, state, record: model.Record, reason: str,
                       detail: str, retryable: bool = True,
                       partial_kept: bool = False) -> None:
        will_retry = retryable and record.attempts < MAX_ATTEMPTS
        if will_retry:
            record.status = STATUS_PENDING
            record.retryable = True
            record.reason = reason
            state.queue.insert(0, record.url)
        else:
            record.status = STATUS_FAILED
            record.retryable = False
            record.reason = reason
            if not partial_kept and record.partial_path:
                (self.output_dir / record.partial_path).unlink(missing_ok=True)
                record.partial_path = None
            state.add_failure(model.Failure(
                url=record.url, kind=record.kind, reason=reason,
                retryable=False, parent=record.parent, detail=detail,
                hops=list(record.hops),
            ))
            self._fail_edges(state, record, reason)

    def _fail_edges(self, state, record: model.Record, reason: str) -> None:
        for edge in state.edges:
            if edge.fetch_url == record.url and edge.status not in (
                EDGE_CAPTURED, EDGE_EXCLUDED, EDGE_EXTERNAL
            ):
                edge.status = EDGE_FAILED
                edge.reason = reason

    def _defer(self, state, record: model.Record, reason: str) -> None:
        record.status = STATUS_DEFERRED
        record.reason = reason
        record.retryable = True
        if record.url not in state.queue:
            state.queue.append(record.url)
        for edge in state.edges:
            if edge.fetch_url == record.url and edge.status == EDGE_DEFERRED:
                edge.reason = reason

    def _count_capture(self, state, record: model.Record) -> None:
        if record.kind == KIND_PAGE:
            state.counters["pages"] += 1
        else:
            state.counters["resources"] += 1

    def _reconcile_edges(self, state: ArchiveState) -> None:
        """按目标记录的最终状态重算每条边，消除创建时的临时 deferred。"""

        terminal = {
            STATUS_CAPTURED: EDGE_CAPTURED,
            STATUS_PENDING: EDGE_DEFERRED,
            STATUS_IN_PROGRESS: EDGE_DEFERRED,
            STATUS_DEFERRED: EDGE_DEFERRED,
            STATUS_DENIED: EDGE_EXTERNAL,
            STATUS_FAILED: EDGE_FAILED,
            STATUS_EXCLUDED: EDGE_EXCLUDED,
        }
        for edge in state.edges:
            target = state.get(edge.fetch_url)
            if target is None:
                continue  # 范围外/非 HTTP 等边在创建时已定终态，保持不变
            edge.status = terminal.get(target.status, edge.status)
            edge.reason = target.reason
            if target.status == STATUS_CAPTURED:
                edge.local_path = target.published_path or target.store_path

    # ============================================================
    # 发布阶段（改写 + 清单）
    # ============================================================

    def _finalize(self, state: ArchiveState) -> None:
        # 先对账：边创建时目标可能还在队列里（deferred），收尾时按目标最终
        # 状态重算边的终态与本地路径，避免清单里残留过期的 deferred 边
        self._reconcile_edges(state)
        captured = [r for r in state.records.values()
                    if r.status == STATUS_CAPTURED]
        page_records = [r for r in captured if r.kind == KIND_PAGE]
        css_records = [
            r for r in captured
            if (r.media_type or "") == "text/css"
            or r.url.split("?")[0].endswith(".css")
        ]

        # 先发布样式表，再发布页面：页面中的样式链接需要指向已就位的改写副本，
        # 循环 @import 也在这一步闭合到包内文件
        css_cache: dict[tuple[str, str], str] = {}
        # 先登记全部 CSS 发布路径，保证循环 @import 互相指向发布副本而非 store
        for record in css_records:
            cache_key = (record.sha256, record.url)
            if cache_key not in css_cache:
                css_cache[cache_key] = f"{CSS_DIR}/{record.sha256[:12]}.css"
            record.published_path = css_cache[cache_key]
        for record in css_records:
            raw = (self.output_dir / record.store_path).read_bytes()
            css_text = raw.decode(record.charset or "utf-8", errors="replace")
            rel = record.published_path
            target = self.output_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            rewritten = rewrite_css(
                css_text, record.url,
                lambda resolved: self._resolve_css_url(state, resolved, rel),
            )
            target.write_text(rewritten, encoding="utf-8")

        # 页面发布镜像：先登记全部镜像路径，再逐个改写，
        # 保证任意页面之间的链接都解析到 pages/ 而非 store/ 原件
        for record in page_records:
            record.published_path = safe_page_relpath(record.url)
        for record in page_records:
            rel = record.published_path
            raw = (self.output_dir / record.store_path).read_bytes()
            html_text = raw.decode(record.charset or "utf-8", errors="replace")
            rewritten = rewrite_html(
                html_text, record.url,
                lambda fetch_url: self._resolve_local(state, fetch_url, rel),
            )
            target = self.output_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rewritten, encoding="utf-8")

        # 根索引：第一个种子页面作为离线入口
        self._write_root_index(state, page_records)
        self._write_manifest(state)
        state.completed = True

    def _resolve_local(self, state, fetch_url: str, from_rel: str) -> str | None:
        record = state.get(normalize_url(fetch_url))
        if record is None or record.status != STATUS_CAPTURED:
            return None
        # 页面指向可读镜像；其余资源（图片/字体/ico）指向内容寻址原件
        target_rel = record.published_path or record.store_path
        return relative_href(from_rel, target_rel)

    def _resolve_css_url(self, state, resolved: str, css_rel: str) -> str | None:
        fetch_url, _frag = split_fragment(resolved)
        record = state.get(normalize_url(fetch_url))
        if record is None or record.status != STATUS_CAPTURED:
            return None
        # 任何已发布的副本（页面或改写后的 CSS）优先，保证循环 @import 闭合在包内
        target_rel = record.published_path or record.store_path
        return relative_href(css_rel, target_rel)

    def _write_root_index(self, state, page_records) -> None:
        seed = state.seeds[0] if state.seeds else None
        entry = next((r for r in page_records if r.url == seed), None) \
            or (page_records[0] if page_records else None)
        items = []
        for record in page_records:
            href = relative_href("index.html", record.published_path)
            items.append(f'<li><a href="{href}">{record.url}</a></li>')
        report_href = "offline-report.html"
        html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>离线展览档案 · 入口</title>
<style>body{{font-family:sans-serif;max-width:52rem;margin:2rem auto;line-height:1.6}}
code{{background:#f4f4f4;padding:.1rem .3rem}}</style></head>
<body>
<h1>离线展览档案</h1>
<p>本目录为限域采集产物。原始站点关闭后可直接从本页阅读，
核查报告见 <a href="{report_href}">offline-report.html</a>。</p>
<ol>
{''.join(items)}
</ol>
<p>清单文件：<code>manifest.json</code>；采集状态：<code>state.json</code>。</p>
</body></html>
"""
        (self.output_dir / "index.html").write_text(html, encoding="utf-8")

    def _write_manifest(self, state: ArchiveState) -> None:
        from .manifest import _atomic_write_json, MANIFEST_NAME

        pages, resources = [], []
        for record in state.records.values():
            entry = {
                "url": record.url,
                "kind": record.kind,
                "status": record.status,
                "reason": record.reason,
                "depth": record.depth,
                "parent": record.parent,
                "final_url": record.final_url,
                "media_type": record.media_type,
                "charset": record.charset,
                "sha256": record.sha256,
                "bytes": record.bytes,
                "store_path": record.store_path,
                "published_path": record.published_path,
                "shared_with": record.shared_with,
                "hops": [h.to_dict() for h in record.hops],
            }
            (pages if record.kind == KIND_PAGE else resources).append(entry)

        incomplete_reasons = self._incomplete_reasons(state)
        manifest = {
            "schema": "site-capsule/manifest/1",
            "generated_at": utc_now(),
            "seeds": state.seeds,
            "scope": {
                "page_prefixes": list(self.scope.page_prefixes),
                "resource_origins": sorted(self.scope.resource_origins),
            },
            "budgets": {
                "max_pages": self.budget.max_pages,
                "max_resources": self.budget.max_resources,
                "max_bytes": self.budget.max_bytes,
                "max_depth": self.budget.max_depth,
            },
            "counters": state.counters,
            "complete": not incomplete_reasons,
            "incomplete_reasons": incomplete_reasons,
            "pages": pages,
            "resources": resources,
            "edges": [edge.to_dict() for edge in state.edges],
            "failures": [failure.to_dict() for failure in state.failures],
        }
        _atomic_write_json(self.output_dir / MANIFEST_NAME, manifest)

    def _incomplete_reasons(self, state: ArchiveState) -> list[dict]:
        reasons: list[dict] = []
        # denied 表示“已尝试抓取、却在重定向途中越界被挡下”，属于真实缺口；
        # 普通范围外链接根本不建记录，属刻意保留，不影响 complete 判定
        for record in state.records.values():
            if record.status in (
                STATUS_FAILED, STATUS_DEFERRED, STATUS_DENIED
            ):
                reasons.append({
                    "url": record.url,
                    "kind": record.kind,
                    "status": record.status,
                    "reason": record.reason,
                })
        for edge in state.edges:
            if edge.status == EDGE_FAILED:
                reasons.append({
                    "url": edge.fetch_url,
                    "via": edge.source,
                    "status": EDGE_FAILED,
                    "reason": edge.reason,
                })
        return reasons


class _ScopeExit(Exception):
    def __init__(self, exit_url: str, reason: str, hops: list[model.Hop]):
        super().__init__(f"重定向越界：{exit_url}（{reason}）")
        self.exit_url = exit_url
        self.reason = reason
        self.hops = hops
