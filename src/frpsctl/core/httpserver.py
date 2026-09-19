"""有界并发的 HTTP 服务（Web 管理台与插件服务共用，v0.3.1）。

`ThreadingHTTPServer` 每请求一线程且**没有上限**：慢请求或连接风暴会让线程数
无界增长。Web 管理台在 v0.3.0 收口了这一点（worker 上限 + 超限 503），而插件
服务（登录单点、frp 侧对插件 HTTP 客户端没有超时）一直是裸 `ThreadingHTTPServer`
——同一套部署栈、同一类风险，两处却两套行为。本模块把它提取为共享实现。

过载语义：worker 用尽时，连接立即收到 `503` 并断开，而不是无限排队。对插件
而言"非 200 = 该次操作失败"（frp 的判定），方向正确：过载时**快速失败**，
线程数有界，登录链路不被排队拖死（fail-closed 的合理过载表现）。
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

__all__ = ["BoundedThreadingHTTPServer"]


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """有界并发的 HTTP 服务：worker 用尽时直接 503 并断开。

    `daemon_threads=True`：进程退出不等待存量线程（与两个服务原实现一致）。
    worker 槽位按**请求**占用（`process_request_thread` 的 finally 释放），
    因此只要响应方 write 完成槽位就归还——keep-alive 连接不会占死额度。

    **过载语义（v0.3.1 实测校准）**：

    - worker 用尽 → 立即回 `503` 并关闭，不排队；
    - 高并发冲撞下**部分超限连接会表现为 TCP 重置**（服务端 close 时接收
      缓冲仍有未读请求体，内核按 TCP 语义发 RST；实测 60 并发约 1/3）——
      客户端（frp）看到的同样是"该操作失败"，只是不是 HTTP 503；
    - **刻意不做"吸掉请求体再回包"的等待**：实测非阻塞排空无法消除该竞态
      （数据还在路上），而带等待的排空会拖慢 accept 循环——frp 对插件请求
      没有超时，让登录链路变慢的代价高于 RST 本身；
    - 已接住的请求正常完成，线程数由 `max_workers` 约束。
    """

    daemon_threads = True

    def __init__(self, *args: object, max_workers: int = 32, **kwargs: object) -> None:
        self._workers = threading.BoundedSemaphore(max(1, max_workers))
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:  # noqa: ANN001
        if not self._workers.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # 派生线程失败（极端情形）必须归还名额，否则额度会随失败次数泄漏。
            self._workers.release()
            raise

    def process_request_thread(self, request, client_address) -> None:  # noqa: ANN001
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()
