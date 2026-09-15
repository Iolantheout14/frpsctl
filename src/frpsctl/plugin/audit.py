"""审计日志：异步、定长、绝不阻塞登录链路（设计文档 §11.2、§11.3）。

**冲突点**：审计要"每次裁决都留痕"，而 §11.2 要求 handler 内**绝不做慢速外部
调用**——插件一 hang，全部客户端的登录链路跟着 hang（frp 侧对插件 HTTP 客户端
没有设置任何 timeout）。

**解法**：`record()` 只做一次入队（O(1)、无 I/O），真正的写盘交给守护线程。
这样即使磁盘卡住，登录链路也只会看到"内存里多了一条记录"，不会阻塞。

**为什么是 JSONL**：每行一个独立 JSON 对象，追加写、可被 `tail`/`grep` 直接消费，
且进程被 kill 时最多丢最后一行——不会像"单个大 JSON 数组"那样整份损坏。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["AuditRecord", "AuditLog"]


@dataclass(frozen=True)
class AuditRecord:
    """一条裁决记录。字段刻意保持扁平——它以后要被 jq/grep 处理。"""

    op: str
    user: str
    decision: str  # "allow" / "deny"
    reason: str = ""
    proxy_name: str = ""
    proxy_type: str = ""
    remote_port: int = 0
    reqid: str = ""
    client_id: str = ""
    source: str = ""
    #: 该次裁决的耗时（毫秒）。用于回答"插件拖慢了登录吗"。
    elapsed_ms: float = 0.0

    @property
    def at(self) -> float:
        return self._at

    _at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._at)),
            "at_unix": round(self._at, 3),
            "op": self.op,
            "user": self.user,
            "decision": self.decision,
            "reason": self.reason,
            "proxy_name": self.proxy_name,
            "proxy_type": self.proxy_type,
            "remote_port": self.remote_port,
            "reqid": self.reqid,
            "client_id": self.client_id,
            "source": self.source,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


class AuditLog:
    """带缓冲的追加式审计日志。

    - `record()` 永不阻塞（只入队）
    - 后台线程按 `flush_every` / `flush_interval` 刷盘
    - `close()` 时同步刷干净，测试与正常退出都靠它
    """

    def __init__(
        self,
        path: Path | None,
        *,
        enabled: bool = True,
        flush_every: int = 32,
        flush_interval: float = 2.0,
        max_buffer: int = 10_000,
        clock=time.monotonic,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.flush_every = max(1, flush_every)
        self.flush_interval = max(0.05, flush_interval)
        # 缓冲上限：磁盘长时间不可用时不至于把内存吃光。**溢出时丢最旧的**，
        # 因为最近的记录才是排查时最需要的。
        self.max_buffer = max_buffer

        self._buffer: deque[AuditRecord] = deque()
        self._lock = threading.Lock()
        self._clock = clock
        self._last_flush = clock()
        self._written = 0
        self._dropped = 0
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 磁盘可写性。为 False 时审计降级为"仅内存"，不影响服务可用性。
        self._file_writable = False

        if self.enabled and self.path is not None:
            # 建目录都可能失败（父路径是个普通文件、权限不足、磁盘只读…）。
            # **审计不可用绝不能让插件起不来**——插件起不来 = 所有人登录不了，
            # 而审计只是"少了一份记录"。因此这里降级为"仅内存"继续跑。
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._file_writable = False
            else:
                self._file_writable = True
            self._thread = threading.Thread(
                target=self._run, name="frpsctl-audit", daemon=True
            )
            self._thread.start()
        else:
            self._file_writable = False

    # --- 写入 ----------------------------------------------------------

    def record(self, item: AuditRecord) -> None:
        """入队一条记录。**这个方法必须保持 O(1) 且无 I/O。**"""
        if not self.enabled:
            return
        with self._lock:
            if len(self._buffer) >= self.max_buffer:
                self._buffer.popleft()
                self._dropped += 1
            self._buffer.append(item)
            should_flush = len(self._buffer) >= self.flush_every
        if should_flush:
            self.flush()

    def flush(self) -> int:
        """把缓冲刷到磁盘。返回写入条数。"""
        if not self.enabled or self.path is None:
            with self._lock:
                self._buffer.clear()
            return 0
        if not self._file_writable:
            # 目录建不出来：记录留在内存里，服务照常工作。
            return 0

        with self._lock:
            pending = list(self._buffer)
            self._buffer.clear()
            self._last_flush = self._clock()
        if not pending:
            return 0

        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                for item in pending:
                    handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
                handle.flush()
        except OSError:
            # 写不进去也不能让登录链路失败：把记录放回缓冲区，等下次再试。
            # 这是"审计可用性"与"服务可用性"之间的取舍——服务优先（fail-open
            # 只影响审计，而 fail-closed 会让所有人登录不了）。
            with self._lock:
                self._buffer.extendleft(reversed(pending))
            return 0

        with self._lock:
            self._written += len(pending)
        return len(pending)

    def close(self, timeout: float = 2.0) -> None:
        """停止后台线程并刷干净。"""
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.flush()

    # --- 观测 ----------------------------------------------------------

    @property
    def written(self) -> int:
        with self._lock:
            return self._written

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buffer)

    def describe(self) -> str:
        if not self.enabled:
            return "审计：关闭"
        if self.path is None or not self._file_writable:
            reason = "未配置 path" if self.path is None else f"无法写入 {self.path}"
            return f"审计：仅内存（{reason}；已记录 {self.written + self.pending} 条）"
        extra = f"，丢弃 {self.dropped} 条" if self.dropped else ""
        return f"审计：{self.path}（已写入 {self.written} 条{extra}）"

    # --- 后台线程 ------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(0.2):
            if self._clock() - self._last_flush >= self.flush_interval:
                self.flush()
        self.flush()
