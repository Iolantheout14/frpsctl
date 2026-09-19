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
    #: 配额计数的来源（`dashboard` 权威 / `local` 退化 / 空表示未涉及配额）。
    quota_source: str = ""
    #: 本条之前**被限速抑制**的同类拒绝条数（`reject_log_burst`）。
    #: 拒绝风暴时限速器停止逐条记录，但把条数累计到下一条上如实汇报——
    #: "降级必须可见"：审计可以降采样，但绝不能让人以为拒绝只发生了那几次。
    suppressed: int = 0

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
            "quota_source": self.quota_source,
            "suppressed": self.suppressed,
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
        max_bytes: int = 0,
        max_age_seconds: float = 0,
        clock=time.monotonic,
    ) -> None:
        self.path = path
        self.enabled = enabled
        #: 文件超过该大小或该年龄就轮转（`path` → `path.1`，保留 2 份）；
        #: 0 = 该维度禁用（两个维度独立生效）。
        self.max_bytes = max(0, max_bytes)
        self.max_age_seconds = max(0.0, max_age_seconds)
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
        #: 缓冲攒够时用它唤醒后台线程立刻刷盘（而不是让请求线程自己写）
        self._wake = threading.Event()
        #: 串行化写盘，与 _lock 分开（见 flush 的说明）
        self._io_lock = threading.Lock()
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
            self._thread = threading.Thread(target=self._run, name="frpsctl-audit", daemon=True)
            self._thread.start()
        else:
            self._file_writable = False

    # --- 写入 ----------------------------------------------------------

    def record(self, item: AuditRecord) -> None:
        """入队一条记录。**这个方法必须保持 O(1) 且无 I/O。**

        哪怕缓冲已满到 `flush_every`，也**只唤醒后台线程**，绝不在这里写盘：
        frp 侧对插件 HTTP 客户端没有超时，一旦磁盘变慢，同步写就会把所有人
        的登录链路一起拖住（§11.2）。"审计走异步队列"必须是真的异步。
        """
        if not self.enabled or self._closed:
            if self._closed:
                with self._lock:
                    self._dropped += 1
            return
        with self._lock:
            if len(self._buffer) >= self.max_buffer:
                self._buffer.popleft()
                self._dropped += 1
            self._buffer.append(item)
            full = len(self._buffer) >= self.flush_every
        if full:
            self._wake.set()

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

        # 用**独立的 I/O 锁**串行化写盘：两个 flush 并发时各持一个 fd 交叉写，
        # 会让 JSONL 的物理行序与裁决顺序不一致（实测小批插进大批中间），
        # 取证时会误导。不能复用 _lock——那样 record() 会被写盘阻塞。
        with self._io_lock:
            # 轮转检查必须在写入前、且在同一把 I/O 锁内：并发 flush 不会
            # 一个在写旧文件、另一个刚把它改名。
            from ..core.auditlog import rotate_if_needed

            rotate_if_needed(
                self.path,
                max_bytes=self.max_bytes,
                max_age_seconds=self.max_age_seconds,
            )
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    for item in pending:
                        handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
                    handle.flush()
            except OSError:
                # 写不进去也不能让登录链路失败：把记录放回缓冲区，等下次再试。
                # 这是"审计可用性"与"服务可用性"之间的取舍——服务优先。
                with self._lock:
                    self._buffer.extendleft(reversed(pending))
                    # 回灌也要尊重缓冲上限，否则磁盘长期不可用时会无界增长
                    while len(self._buffer) > self.max_buffer:
                        self._buffer.pop()
                        self._dropped += 1
                return 0

            with self._lock:
                self._written += len(pending)
        return len(pending)

    def close(self, timeout: float = 2.0) -> None:
        """停止后台线程并刷干净。

        线程若在超时内没退出（例如卡在没有超时的写盘上），**保留引用**并留下
        一条痕迹——把 `_thread` 置 None 只会让"它还活着"这件事再也无法被观察。
        """
        self._closed = True
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                import contextlib
                import sys

                with contextlib.suppress(OSError):
                    sys.stderr.write("[plugin] 审计线程未在超时内退出（可能卡在写盘），残留记录可能未落盘\n")
                    sys.stderr.flush()
            else:
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
        while not self._stop.is_set():
            # 被 record() 唤醒（缓冲满）或每 0.2s 轮询一次到期时间
            self._wake.wait(0.2)
            self._wake.clear()
            if self._stop.is_set():
                break
            if self._clock() - self._last_flush >= self.flush_interval:
                self.flush()
        self.flush()
