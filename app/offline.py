"""离线核查入口。

不发起任何网络请求，只对已生成的包做静态检查：

1. 清单存在且 complete 标记可信（重新计算失败/延期关系）；
2. 每个已发布 HTML/CSS 中的本地相对引用都能在包内找到文件，
   且不存在借助 ".." 逃出输出目录的路径；
3. 残留的 http(s) 绝对 URL 必须能在清单边中找到“刻意保留”的理由
   （范围外、重定向越界、音视频排除、非 HTTP 协议、采集失败等）；
4. 清单登记的 store 文件全部存在；
5. 生成 offline-report.html，解释每一条外部链接为何保留外部访问。

退出码：0 表示包完整且自洽；非 0 表示不可作为离线成品发布。
"""

from __future__ import annotations

import html as html_mod
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cssrewrite import extract_css_references
from .htmlrewrite import parse_html
from .model import (
    EDGE_CAPTURED,
    EDGE_EXCLUDED,
    EDGE_EXTERNAL,
    EDGE_FAILED,
)
from .scope import normalize_url

_ABS_URL_RE = re.compile(r"https?://", re.IGNORECASE)


@dataclass
class CheckResult:
    ok: bool = True
    violations: list[str] = field(default_factory=list)
    external_links: list[dict] = field(default_factory=list)
    local_refs_checked: int = 0
    files_checked: int = 0

    def fail(self, message: str) -> None:
        self.ok = False
        self.violations.append(message)


def check_package(output_dir: str | Path) -> CheckResult:
    root = Path(output_dir).resolve()
    result = CheckResult()
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        result.fail("缺少 manifest.json，无法证明包的来源与范围")
        return result
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    explainable = _build_external_index(manifest)
    published = _collect_published(root, manifest, result)

    for rel_path, kind in published:
        result.files_checked += 1
        text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
        if kind == "html":
            _check_html(root, rel_path, text, explainable, result)
        else:
            _check_css(root, rel_path, text, explainable, result)

    _check_store(root, manifest, result)
    _check_manifest_completeness(manifest, result)
    _write_report(root, manifest, result)
    return result


# ------------------------------------------------------------------

def _build_external_index(manifest: dict) -> dict[str, dict]:
    """fetch_url -> 可解释理由（来自清单边与失败关系）。"""

    index: dict[str, dict] = {}
    for edge in manifest.get("edges", []):
        if edge["status"] in (EDGE_EXTERNAL, EDGE_EXCLUDED, EDGE_FAILED):
            index.setdefault(edge["fetch_url"], {
                "reasons": set(),
                "sources": set(),
                "attributes": set(),
                "status": edge["status"],
            })
            entry = index[edge["fetch_url"]]
            entry["reasons"].add(edge.get("reason") or edge["status"])
            entry["sources"].add(edge["source"])
            entry["attributes"].add(edge["attribute"])
    for failure in manifest.get("failures", []):
        index.setdefault(failure["url"], {
            "reasons": set(),
            "sources": set(),
            "attributes": set(),
            "status": EDGE_FAILED,
        })
        entry = index[failure["url"]]
        entry["reasons"].add(failure["reason"])
        if failure.get("parent"):
            entry["sources"].add(failure["parent"])
    return index


def _collect_published(root: Path, manifest: dict, result: CheckResult):
    files = []
    for record in manifest.get("pages", []) + manifest.get("resources", []):
        rel = record.get("published_path")
        if not rel:
            continue
        if not (root / rel).exists():
            result.fail(f"清单声明的发布文件缺失：{rel}（{record['url']}）")
            continue
        kind = "html" if rel.startswith("pages/") or rel.endswith(".html") else "css"
        files.append((rel, kind))
    return files


def _resolve_local(root: Path, doc_rel: str, href: str) -> Path:
    base_dir = (root / doc_rel).parent
    if href.startswith("/"):
        # 包根相对路径（输出内不应出现，但按根目录安全解析）
        target = root / href.lstrip("/")
    else:
        target = base_dir / unquote(href)
    return target.resolve()


def _check_local(root: Path, doc_rel: str, href: str, result: CheckResult,
                 package_relative: bool = False) -> None:
    if href.startswith("#") or href == "":
        return
    if package_relative or href.startswith("/"):
        # 包根相对路径：相对输出根解析，绝不允许逃出
        target = (root / href.lstrip("/")).resolve()
    else:
        target = _resolve_local(root, doc_rel, href)
    try:
        target.relative_to(root)
    except ValueError:
        result.fail(f"{doc_rel} 中的引用逃出包目录：{href}")
        return
    result.local_refs_checked += 1
    if not target.exists():
        result.fail(f"{doc_rel} 引用的本地文件不存在：{href}")


def _check_external(url: str, explainable: dict, doc_rel: str,
                    result: CheckResult) -> None:
    key = normalize_url(url.split("#", 1)[0])
    entry = explainable.get(key)
    if entry is None:
        result.fail(
            f"{doc_rel} 残留未解释的远程引用 {url}：离线打开会静默联网"
        )
        return
    result.external_links.append({
        "url": key,
        "status": entry["status"],
        "reasons": sorted(entry["reasons"]),
        "sources": sorted(entry["sources"]),
        "attributes": sorted(entry["attributes"]),
    })


def _check_html(root: Path, doc_rel: str, text: str,
                explainable: dict, result: CheckResult) -> None:
    # 已改写文件的 base 即其自身目录；用文件 URL 作 base，
    # 使其中的相对引用解析为 file: 本地身份而非远程绝对 URL
    doc_url = (root / doc_rel).as_uri()
    parsed = parse_html(text, doc_url)
    for ref in parsed.references:
        scheme = urlsplit(ref.fetch_url).scheme.lower()
        if scheme in ("http", "https"):
            _check_external(ref.fetch_url, explainable, doc_rel, result)
        elif scheme == "file":
            _check_local(root, doc_rel,
                         _file_url_to_rel(root, ref.fetch_url),
                         result, package_relative=True)
    # srcset 与普通属性中的相对路径统一做存在性校验（相对文档目录）
    for raw in _raw_local_href_values(text):
        _check_local(root, doc_rel, raw.split("#", 1)[0], result)


def _file_url_to_rel(root: Path, file_url: str) -> str:
    from urllib.parse import urlparse, unquote

    path = unquote(urlparse(file_url).path)
    return str(Path(path).relative_to(root.resolve()))


def _check_css(root: Path, doc_rel: str, text: str,
               explainable: dict, result: CheckResult) -> None:
    doc_url = (root / doc_rel).as_uri()
    for ref in extract_css_references(text, doc_url):
        scheme = urlsplit(ref.resolved).scheme.lower()
        if scheme in ("http", "https"):
            _check_external(ref.resolved, explainable, doc_rel, result)
        elif scheme in ("data", "blob"):
            continue
        elif scheme == "file":
            # 改写后的 CSS 引用一律为包根相对路径（store/ 或 assets/）
            _check_local(root, doc_rel,
                         _file_url_to_rel(root, ref.resolved),
                         result, package_relative=True)
        else:
            _check_local(root, doc_rel, ref.target, result)


def _raw_local_href_values(html_text: str) -> list[str]:
    """提取属性中出现的相对路径（改写后应全部为包内相对引用）。"""

    from .htmlrewrite import _TAG_RE, _attributes

    locals_ = []
    for tag_match in _TAG_RE.finditer(html_text):
        name = tag_match.group("name")
        if name is None:
            continue
        body = tag_match.group("body") or ""
        for attr_name, value, _, _ in _attributes(body):
            if not value or attr_name not in (
                "href", "src", "poster", "data", "srcset"
            ):
                continue
            candidates = value.split(",") if attr_name == "srcset" else [value]
            for candidate in candidates:
                token = candidate.strip().split(" ")[0]
                if token and not _ABS_URL_RE.search(token) and not token.startswith(
                    ("#", "data:", "mailto:", "tel:", "javascript:")
                ):
                    locals_.append(token)
    return locals_


def _check_store(root: Path, manifest: dict, result: CheckResult) -> None:
    for record in manifest.get("pages", []) + manifest.get("resources", []):
        store_rel = record.get("store_path")
        if store_rel and not (root / store_rel).exists():
            result.fail(f"内容寻址文件缺失：{store_rel}（{record['url']}）")


def _check_manifest_completeness(manifest: dict, result: CheckResult) -> None:
    if not manifest.get("complete", False):
        result.fail(
            "manifest.complete=false：存在失败或延期关系，"
            f"原因 {len(manifest.get('incomplete_reasons', []))} 条，"
            "包不完整，不能宣称可完全离线阅读"
        )
    for edge in manifest.get("edges", []):
        if edge["status"] == EDGE_CAPTURED and not edge.get("local_path"):
            result.fail(f"边标记为已采集但缺少本地路径：{edge}")


# ------------------------------------------------------------------

def _write_report(root: Path, manifest: dict, result: CheckResult) -> None:
    seen: dict[str, dict] = {}
    for item in result.external_links:
        seen.setdefault(item["url"], item)
    rows = []
    reason_labels = {
        "out_of_scope_page_prefix": "页面路径超出授权前缀",
        "out_of_scope_resource_origin": "静态资源 origin 未授权",
        "redirect_left_scope": "重定向逐跳检查时越界",
        "media_intentionally_excluded": "音视频按策略不采集",
        "non_http_scheme": "非 HTTP 协议，刻意保留",
        "fetch_error": "采集失败（可重试）",
        "http_error_status": "远端返回错误状态",
        "budget_max_pages": "页面数量预算耗尽（延期）",
        "budget_max_resources": "资源数量预算耗尽（延期）",
        "budget_max_bytes": "体积预算耗尽（延期）",
        "budget_max_depth": "深度预算耗尽（延期）",
        "external_base_href": "外部 base href，未跟随",
        "failed": "采集失败",
        "external": "范围外，未采集",
        "excluded": "刻意排除",
    }
    for item in sorted(seen.values(), key=lambda x: x["url"]):
        reasons = "；".join(
            reason_labels.get(r, r) for r in item["reasons"]
        )
        rows.append(
            "<tr><td>{url}</td><td>{status}</td><td>{reasons}</td>"
            "<td>{sources}</td><td>{attrs}</td></tr>".format(
                url=html_mod.escape(item["url"]),
                status=html_mod.escape(item["status"]),
                reasons=html_mod.escape(reasons),
                sources=html_mod.escape("，".join(item["sources"])),
                attrs=html_mod.escape("，".join(item["attributes"])),
            )
        )
    failure_rows = "".join(
        "<tr><td>{url}</td><td>{reason}</td><td>{detail}</td></tr>".format(
            url=html_mod.escape(f["url"]),
            reason=html_mod.escape(reason_labels.get(f["reason"], f["reason"])),
            detail=html_mod.escape(f.get("detail") or ""),
        )
        for f in manifest.get("failures", [])
    )
    banner = (
        '<p class="ok">核查通过：所有本地引用齐备，外部链接均有留档理由。</p>'
        if result.ok else
        '<p class="bad">核查未通过：存在离线完整性问题，见下方违例列表，'
        '不得作为离线成品发布。</p>'
    )
    violations = "".join(
        f"<li>{html_mod.escape(v)}</li>" for v in result.violations
    ) or "<li>无</li>"
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>离线核查报告</title>
<style>
body{{font-family:sans-serif;max-width:70rem;margin:2rem auto;line-height:1.5}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}
th,td{{border:1px solid #bbb;padding:.35rem .5rem;text-align:left;
vertical-align:top;word-break:break-all}}
.ok{{background:#e6f6e6;padding:.8rem;border-left:4px solid #2a7}}
.bad{{background:#fdecea;padding:.8rem;border-left:4px solid #c33}}
code{{background:#f4f4f4;padding:.1rem .3rem}}
</style></head><body>
<h1>离线核查报告</h1>
{banner}
<p>生成时间：{html_mod.escape(manifest.get('generated_at', ''))}
｜检查文件 {result.files_checked} 个｜本地引用 {result.local_refs_checked} 条
｜外部保留链接 {len(seen)} 条｜失败关系 {len(manifest.get('failures', []))} 条</p>
<h2>违例</h2><ul>{violations}</ul>
<h2>刻意保留外部访问的链接</h2>
<table><thead><tr><th>URL</th><th>状态</th><th>未采集原因</th>
<th>引用来源</th><th>出现位置</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=5>无</td></tr>'}</tbody></table>
<h2>失败关系</h2>
<table><thead><tr><th>URL</th><th>原因</th><th>详情</th></tr></thead>
<tbody>{failure_rows or '<tr><td colspan=3>无</td></tr>'}</tbody></table>
<h2>范围与预算</h2>
<pre>{html_mod.escape(json.dumps(
    {'scope': manifest.get('scope'), 'budgets': manifest.get('budgets'),
     'counters': manifest.get('counters')}, ensure_ascii=False, indent=2))}</pre>
<p>本报告由静态检查生成，过程中不发起任何网络请求。</p>
</body></html>
"""
    (root / "offline-report.html").write_text(html, encoding="utf-8")
