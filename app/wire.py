"""可重放的测试传输层。

保留最初的 Replay.get 接口（现有回归测试依赖），并新增 open() 流式接口，
支持“先吐出若干字节再中断”的响应序列，用于验证下载中断与续采。
重定向条目原样返回 3xx，由上层逐跳做范围检查，传输层不自动跟随。
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict
    body: bytes


class _ReplayStream:
    def __init__(self, replay: "Replay", url: str, entry: dict):
        self.url = url
        self.status = entry["status"]
        self.headers = deepcopy(entry.get("headers", {}))
        data = entry.get("body", "")
        self._body = base64.b64decode(data) if entry.get("base64") else data.encode("utf-8")
        self._pos = 0
        self._closed = False
        self._failure = entry.get("stream_failure")
        self._replay = replay

    def read(self) -> bytes:
        if self._failure:
            after = int(self._failure.get("after_bytes", 0))
            piece = self._body[self._pos:self._pos + after]
            self._pos += len(piece)
            self.close()
            raise ConnectionError(
                f"模拟连接中断：{self.url} 在 {self._pos} 字节后断开"
            )
        data = self._body[self._pos:]
        self._pos = len(self._body)
        return data

    def iter_chunks(self, chunk_size: int = 65536):
        if self._failure:
            after = int(self._failure.get("after_bytes", 0))
            piece = self._body[self._pos:min(len(self._body), self._pos + after)]
            self._pos += len(piece)
            if piece:
                yield piece
            self.close()
            raise ConnectionError(
                f"模拟连接中断：{self.url} 在 {self._pos} 字节后断开"
            )
        while self._pos < len(self._body):
            piece = self._body[self._pos:self._pos + chunk_size]
            self._pos += len(piece)
            yield piece

    def close(self) -> None:
        self._closed = True


class Replay:
    def __init__(self, path):
        self.routes = json.loads(Path(path).read_text(encoding="utf-8"))
        self.positions = {}
        self.requests = []

    def _next_entry(self, url: str):
        if url not in self.routes:
            return None
        entries = self.routes[url]
        index = self.positions.get(url, 0)
        if index >= len(entries):
            raise RuntimeError("响应序列已耗尽：" + url)
        self.positions[url] = index + 1
        return entries[index]

    def get(self, url, headers=None):
        self.requests.append({"url": url, "headers": deepcopy(headers or {})})
        entry = self._next_entry(url)
        if entry is None:
            return Reply(404, {}, b"")
        data = entry.get("body", "")
        body = base64.b64decode(data) if entry.get("base64") else data.encode("utf-8")
        return Reply(entry["status"], deepcopy(entry.get("headers", {})), body)

    def open(self, url, headers=None):
        """流式打开；找不到路由时返回 404 空响应流。"""

        self.requests.append({"url": url, "headers": deepcopy(headers or {})})
        entry = self._next_entry(url)
        if entry is None:
            return _ReplayStream(self, url, {"status": 404, "headers": {}, "body": ""})
        return _ReplayStream(self, url, entry)
