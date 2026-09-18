"""Web API 的服务端响应缓存（0.3.0）。

仪表盘每 5 秒轮询 status/clients/proxies/traffic/logs：其中 clients/proxies/
traffic 每次都要打 dashboard（最多 50+ 个 HTTP 往返），logs 每次读文件。
而趋势是逐日粒度、列表是秒级变化——重复重算只是把同一份数据又取一遍。

设计取舍：

- **只缓存只读端点**：写操作（actions / config apply / rollback）调用
  `invalidate()` 主动失效，保证"改完立刻看得见"；
- **clock 可注入**：测试用假时钟推进 TTL，不必 sleep；
- **与 ETag 互补**（见 server.py）：TTL 省掉 dashboard 往返，ETag/304 省掉
  响应体传输与 JSON 序列化——浏览器对 `Cache-Control: no-cache` 的响应会
  自动带 `If-None-Match`，前端代码无需感知。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

__all__ = ["TTLCache"]


#: 条目上限：与项目"输入驱动的表必须有界"同一条纪律（会话表/失败来源表/预览表
#: 都有上限）。日志缓存键含行数参数，没有上限时一个已登录调用方遍历不同行数
#: 就能让缓存常驻大量日志文本（v0.3.0 review 实测 2000 个条目）。
DEFAULT_MAX_ENTRIES = 64


class TTLCache:
    """线程安全的 TTL 键值缓存（键为短字符串，值为任意 payload）。

    有界（`max_entries`）且**写入时做全表过期扫描**——惰性淘汰只清被访问的
    键，过期条目会在缓存里留到进程结束。
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, Any]] = {}
        self._max_entries = max(1, max_entries)

    def get(self, key: str, ttl: float) -> Any | None:
        """命中且未过期时返回缓存值；否则 None（过期条目顺手删除）。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            at, value = entry
            if self._clock() - at > ttl:
                del self._entries[key]
                return None
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), value)
            self._evict()

    def _evict(self) -> None:
        """超限时按写入时间淘汰最旧的条目（FIFO；持锁调用）。

        每条目 TTL 不同，这里不按 TTL 判断"过期"——那需要为每个键单独记录
        TTL；条目上限本身就是内存护栏，超限淘汰最旧即可。
        """
        while len(self._entries) > self._max_entries:
            oldest = min(self._entries, key=lambda key: self._entries[key][0])
            del self._entries[oldest]

    def invalidate(self) -> None:
        """清空全部条目（写操作后调用，保证下一次读是新鲜的）。"""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
