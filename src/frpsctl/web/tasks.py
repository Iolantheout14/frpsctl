"""Web 管理台的进程内后台任务（v0.3.4）。

第一个（也是目前唯一）任务类型是 **frps 二进制安装**：`core.release.install`
是长阻塞的网络操作（数十秒~分钟级），不能占住请求 worker（有界 32）——
提交到后台线程执行，前端按 1 秒轮询进度。

设计约束（与 `LoginAuditLimiter` / `TTLCache` 同一纪律）：

- **有界**：条目上限（默认 8，丢最旧）；同时只允许**一个** install 在跑
  （单飞行，重复提交报"任务进行中"——两个安装进程会互相踩 bin/ 目录）；
- **clock 可注入**：测试不依赖真实时钟；
- **失败可见**：任务状态机 `queued → running → done|failed`，错误文本进
  `error` 字段（不吞、不重试）；
- **不取消**：下载中断需要 core 级取消协议（协作式 read 循环 + 事件），
  本轮记账不做（前端提示"请等待完成"）。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import FrpsctlError, UsageError
from ..core.instance import Instance

__all__ = ["Task", "TaskRegistry"]

#: 任务条目上限（超出丢最旧；任务是有界低频事件）。
MAX_TASK_ENTRIES = 8


@dataclass
class Task:
    """一个后台任务的对外视图。"""

    id: str
    kind: str
    version: str
    state: str = "queued"          # queued / running / done / failed
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str = ""
    started_at: float = 0.0
    finished_at: float | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "version": self.version,
            "state": self.state,
            "progress": dict(self.progress),
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class TaskRegistry:
    """进程内任务表（线程安全、有界、install 单飞行）。"""

    def __init__(self, *, clock=time.time, max_entries: int = MAX_TASK_ENTRIES) -> None:
        self._clock = clock
        self._max_entries = max(1, max_entries)
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}

    # --- 查询 -----------------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_payloads(self) -> list[dict[str, Any]]:
        """最近任务（新在前）。"""
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda task: task.started_at, reverse=True)
            return [task.payload() for task in tasks]

    def _evict_locked(self) -> None:
        """淘汰超出上限的**已结束**任务（持锁调用）。

        正在跑/排队的任务绝不被淘汰——否则前端轮询会"任务丢失"，而安装
        其实还在进行（防御性设计；单飞行下 running 存在时不会有新提交，
        但未来新增任务类型时这条纪律必须已经在）。
        """
        while len(self._tasks) > self._max_entries:
            candidates = [
                item
                for item in self._tasks.values()
                if item.state not in ("queued", "running")
            ]
            if not candidates:
                break
            oldest = min(candidates, key=lambda item: item.started_at)
            self._tasks.pop(oldest.id, None)

    # --- 提交 -----------------------------------------------------------

    def submit_install(
        self,
        inst: Instance,
        *,
        version: str,
        only_download: bool,
        mirrors: tuple[str, ...] | None = None,
    ) -> Task:
        """提交一个安装任务并立刻返回（执行在守护线程）。

        前置校验（在提交线程完成，错误同步抛给调用方 = 400/409 而不是
        任务失败）：版本格式、bin 目录可写、单飞行。
        """
        from ..core import release

        if mirrors is None:
            mirrors = release.DEFAULT_MIRRORS
        version = version.strip()
        if not version or not all(part.isdigit() for part in version.split(".")) or version.count(".") != 2:
            raise UsageError(f"版本号格式应为 x.y.z：{version!r}")

        bin_dir = inst.bin_dir
        bin_dir.mkdir(parents=True, exist_ok=True)
        probe = bin_dir / ".write-probe"
        try:
            probe.write_text("", "utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise FrpsctlError(
                f"数据目录不可写：{bin_dir}（{exc}）",
                hint="Web 服务用户对 bin/ 没有写权限；请用 CLI 以 root 安装",
            ) from None

        task = Task(
            id=secrets.token_urlsafe(12),
            kind="install",
            version=version,
            state="queued",
            started_at=self._clock(),
        )
        # "检查单飞行 + 插入 + 淘汰"必须在**同一把锁内**：拆开会让两个并发
        # 提交都通过检查（v0.3.4 review 修复的竞态）
        with self._lock:
            if any(
                item.kind == "install" and item.state in ("queued", "running")
                for item in self._tasks.values()
            ):
                raise UsageError(
                    "已有安装任务在进行中",
                    hint="等待完成后再提交（避免两个进程互踩 bin 目录）",
                )
            self._tasks[task.id] = task
            self._evict_locked()

        def _progress(phase: str, received: int = 0, total: int | None = None) -> None:
            task.progress = {"phase": phase, "received": received, "total": total}

        def _run() -> None:
            task.state = "running"
            _progress("download")
            try:
                result = release.install(
                    bin_dir=bin_dir,
                    version=version,
                    mirrors=list(mirrors),
                    insecure=False,
                    force=False,
                    switch=not only_download,
                    with_frpc=False,
                    on_progress=_progress,
                )
            except FrpsctlError as exc:
                task.state = "failed"
                task.error = exc.render()
            except Exception as exc:  # noqa: BLE001 - 后台线程绝不能静默死掉
                task.state = "failed"
                task.error = f"未分类错误：{type(exc).__name__}: {exc}"
            else:
                task.state = "done"
                task.result = {
                    "version": result.version,
                    "binary": str(result.binary),
                    "switched": result.switched,
                    "downloaded": result.downloaded,
                }
            finally:
                task.finished_at = self._clock()
                task.progress = {**task.progress, "phase": "完成" if task.state == "done" else "失败"}

        threading.Thread(target=_run, name=f"frpsctl-install-{task.id[:6]}", daemon=True).start()
        return task
