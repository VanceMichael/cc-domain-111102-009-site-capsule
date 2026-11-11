"""采集器使用的传输抽象。

两种实现共享同一接口：

* ReplayTransport：回放 fixtures 里的合成响应，离线可测、可模拟中断；
* HttpTransport：基于 http.client 的真实抓取，不自动跟随重定向，
  每一跳交回采集器做范围授权。

传输层只负责“取响应”，不做任何范围判断或 URL 跟随，保证策略单一。
"""

from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlsplit

from .wire import Replay


class TransportError(Exception):
    """可重试的传输层错误（连接中断、超时等）。"""


class Stream:
    status: int
    headers: dict[str, str]

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


@dataclass
class ReplayStream(Stream):
    inner: object

    @property
    def status(self) -> int:
        return self.inner.status

    @property
    def headers(self) -> dict[str, str]:
        return self.inner.headers

    def iter_chunks(self, chunk_size: int = 65536) -> Iterator[bytes]:
        yield from self.inner.iter_chunks(chunk_size)

    def close(self) -> None:
        self.inner.close()


class ReplayTransport:
    """回放型传输，测试与离线验证用，绝不发起真实网络请求。"""

    def __init__(self, replay: Replay):
        self._replay = replay

    def open(self, url: str) -> Stream:
        return ReplayStream(self._replay.open(url))

    def close(self) -> None:
        pass


class _HttpStream(Stream):
    def __init__(self, conn, response):
        self._conn = conn
        self._resp = response
        self.status = response.status
        self.headers = {k: v for k, v in response.getheaders()}

    def iter_chunks(self, chunk_size: int = 65536) -> Iterator[bytes]:
        try:
            while True:
                chunk = self._resp.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        except (socket.timeout, OSError, http.client.HTTPException) as exc:
            raise TransportError(f"读取响应中断：{exc}") from exc

    def close(self) -> None:
        try:
            self._resp.close()
        finally:
            self._conn.close()


class HttpTransport:
    """真实 HTTP/HTTPS 传输；3xx 原样返回，由上层逐跳授权。"""

    def __init__(self, timeout: float = 20.0, user_agent: str = "site-capsule/0.1"):
        self._timeout = timeout
        self._user_agent = user_agent

    def open(self, url: str) -> Stream:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            raise TransportError(f"不支持的协议：{parts.scheme}")
        conn_cls = (
            http.client.HTTPSConnection if parts.scheme.lower() == "https"
            else http.client.HTTPConnection
        )
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        conn = conn_cls(parts.netloc, timeout=self._timeout)
        try:
            conn.request(
                "GET",
                target,
                headers={"User-Agent": self._user_agent, "Accept": "*/*"},
            )
            resp = conn.getresponse()
        except (socket.timeout, OSError, http.client.HTTPException) as exc:
            conn.close()
            raise TransportError(f"请求失败 {url}: {exc}") from exc
        return _HttpStream(conn, resp)

    def close(self) -> None:
        pass


def parse_content_type(header: str | None) -> tuple[str | None, str | None]:
    """解析 Content-Type，返回 (媒体类型, 字符集)。"""

    if not header:
        return None, None
    media_type = None
    charset = None
    for index, piece in enumerate(header.split(";")):
        piece = piece.strip()
        if index == 0:
            media_type = piece.lower() or None
        elif piece.lower().startswith("charset="):
            charset = piece.split("=", 1)[1].strip().strip('"') or None
    return media_type, charset
