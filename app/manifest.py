"""采集状态与归档清单的持久化。

输出目录布局：

    output/
      state.json              断点续采状态（运行中持续原子更新）
      manifest.json           最终清单（采集完成或显式收尾时生成）
      store/aa/<sha>.ext      内容寻址的原始响应字节
      partials/<sha>.part     下载中断的分片（下次运行重新抓取该身份）
      pages/...               改写后的可读页面镜像
      assets/css/<sha>.css    改写后的样式表（原始字节仍在 store/ 留证）
      offline-report.html     离线核查报告
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .model import Edge, Failure, Record

STATE_NAME = "state.json"
MANIFEST_NAME = "manifest.json"
PARTIALS_DIR = "partials"
STORE_DIR = "store"
PAGES_DIR = "pages"
CSS_DIR = "assets/css"
REPORT_NAME = "offline-report.html"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ArchiveState:
    output_dir: str
    seeds: list[str]
    queue: list[str] = field(default_factory=list)
    records: dict[str, Record] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=lambda: {
        "pages": 0,
        "resources": 0,
        "stored_bytes": 0,
    })
    cancelled: bool = False
    completed: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    # ---- 基本操作 -------------------------------------------------

    def get(self, url: str) -> Record | None:
        return self.records.get(url)

    def upsert(self, record: Record) -> None:
        self.records[record.url] = record

    def add_edge(self, edge: Edge) -> None:
        # 同一 (source, target, attribute) 只保留一条，重复出现更新状态
        for existing in self.edges:
            if (existing.source == edge.source
                    and existing.target == edge.target
                    and existing.attribute == edge.attribute):
                existing.status = edge.status
                existing.reason = edge.reason
                existing.local_path = edge.local_path
                return
        self.edges.append(edge)

    def add_failure(self, failure: Failure) -> None:
        for existing in self.failures:
            if existing.url == failure.url and existing.reason == failure.reason:
                existing.detail = failure.detail
                existing.hops = failure.hops
                return
        self.failures.append(failure)

    # ---- 持久化 ---------------------------------------------------

    def state_path(self) -> Path:
        return Path(self.output_dir) / STATE_NAME

    def save(self) -> None:
        self.updated_at = utc_now()
        path = self.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "seeds": self.seeds,
            "queue": self.queue,
            "records": {url: rec.to_dict() for url, rec in self.records.items()},
            "edges": [edge.to_dict() for edge in self.edges],
            "failures": [failure.to_dict() for failure in self.failures],
            "counters": self.counters,
            "cancelled": self.cancelled,
            "completed": self.completed,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        _atomic_write_json(path, payload)

    @classmethod
    def load(cls, path: str | Path) -> "ArchiveState":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        state = cls(
            output_dir=str(Path(path).parent),
            seeds=list(data["seeds"]),
            queue=list(data.get("queue", [])),
            records={url: Record.from_dict(rec) for url, rec in
                     data.get("records", {}).items()},
            edges=[Edge(**edge) for edge in data.get("edges", [])],
            failures=[Failure.from_dict(f) for f in data.get("failures", [])],
            counters=dict(data.get("counters", {
                "pages": 0, "resources": 0, "stored_bytes": 0
            })),
            cancelled=bool(data.get("cancelled", False)),
            completed=bool(data.get("completed", False)),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )
        # 续采：上次中断在半路上的身份回到队列，分片由采集器清理后重抓
        for record in state.records.values():
            if record.status == "in_progress":
                record.status = "pending"
                record.retryable = True
                if record.url not in state.queue:
                    state.queue.append(record.url)
        return state


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
