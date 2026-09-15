"""实例模型与目录布局（设计文档 §6）。

**实例**是配置、状态、日志、锁的最小隔离单位，一台机器可以跑多个 frps。

目录布局（`bin/` 全局共享、`instances/` 实例私有）：

    <data_home>/frpsctl/
    ├── bin/
    │   ├── frps-0.71.0            # 已校验的二进制，按版本并存
    │   └── frps -> frps-0.71.0    # 当前版本软链，唯一被 install 切换的对象
    └── instances/
        └── default/
            ├── frps.toml          # 0600
            ├── state.json         # 所有权判定的权威来源
            ├── frps.pid           # 人类可读副本
            ├── frps.log           # 由 frp 自己写并轮转（ADR-5）
            ├── .lock
            ├── config-history/    # 最近 10 份配置快照 + meta.json
            └── startup/           # 最近 3 份启动日志
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import BinaryNotFound, UsageError

__all__ = [
    "Instance",
    "HISTORY_KEEP",
    "STARTUP_KEEP",
    "resolve_data_home",
    "resolve_instances_root",
]

#: 配置快照保留份数（§9 第 6 步）。
HISTORY_KEEP = 10

#: 启动日志保留份数（ADR-5）。
STARTUP_KEEP = 3

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def resolve_data_home() -> Path:
    """`frpsctl` 的数据根目录，遵循 XDG（可用 FRPSCTL_DATA_HOME 覆盖）。"""
    override = os.environ.get("FRPSCTL_DATA_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "frpsctl"


def resolve_instances_root() -> Path:
    """默认 instances 根目录（可用 FRPSCTL_ROOT 覆盖）。"""
    override = os.environ.get("FRPSCTL_ROOT")
    return Path(override).expanduser() if override else resolve_data_home() / "instances"


def validate_name(name: str) -> str:
    """实例名会被拼进路径，必须先收紧字符集（防止 `../` 穿越）。"""
    if not _NAME_RE.match(name):
        raise UsageError(
            f"非法实例名：{name!r}",
            hint="只允许字母、数字、下划线、点、连字符，且以字母或数字开头（最长 64）",
        )
    return name


@dataclass(frozen=True)
class Instance:
    """一个 frps 实例的全部路径。构造不做 I/O，便于测试。

    `data_home` 单独保存而不是从 `instances_root` 推导：systemd 场景下
    instances 可能在 `/etc/frps/instances`，而二进制仍应留在数据目录（§6）。

    `config_override` 对应全局选项 `--config`：**只覆盖配置文件路径**，
    state / 锁 / 历史仍按实例走（否则会与实例所有权判定脱节）。
    """

    name: str
    instances_root: Path
    data_home: Path = field(default_factory=resolve_data_home)
    config_override: Path | None = None

    def __post_init__(self) -> None:
        validate_name(self.name)

    # --- 路径 ----------------------------------------------------------

    @property
    def dir(self) -> Path:
        return self.instances_root / self.name

    @property
    def config(self) -> Path:
        return self.config_override or (self.dir / "frps.toml")

    @property
    def state(self) -> Path:
        """所有权判定的权威来源（ADR-1）。"""
        return self.dir / "state.json"

    @property
    def pidfile(self) -> Path:
        """兼容性副本：仅 pid，方便人工排查，**不参与任何判定**。"""
        return self.dir / "frps.pid"

    @property
    def lock(self) -> Path:
        return self.dir / ".lock"

    @property
    def history_dir(self) -> Path:
        return self.dir / "config-history"

    @property
    def startup_dir(self) -> Path:
        return self.dir / "startup"

    @property
    def log_file(self) -> Path:
        """frp 自己写入的日志（路径来自配置里的 `log.to`，此处是默认约定）。"""
        return self.dir / "frps.log"

    @property
    def bin_dir(self) -> Path:
        """全局共享的二进制目录（非实例私有）。"""
        return self.data_home / "bin"

    @property
    def bin_link(self) -> Path:
        """当前版本软链：`install --switch` 唯一改动的对象（§8.6.1）。"""
        return self.bin_dir / "frps"

    # --- 二进制 --------------------------------------------------------

    def active_binary(self) -> Path:
        """解软链得到**真实版本文件路径**。

        必须 `resolve()`：写进 `state.json` 的是真实路径而非软链路径，否则软链
        一换，`is_ours()` 的命令行比对就会失配，把自家进程判成 FOREIGN（R13）。
        """
        link = self.bin_link
        if not link.exists():
            raise BinaryNotFound(link)
        return link.resolve(strict=True)

    def versioned_binary(self, version: str) -> Path:
        return self.bin_dir / f"frps-{version}"

    # --- 目录准备 ------------------------------------------------------

    def ensure_dirs(self) -> None:
        """创建实例目录（0700：内含 token 与 dashboard 口令）。"""
        for path in (self.dir, self.history_dir, self.startup_dir, self.bin_dir):
            path.mkdir(parents=True, exist_ok=True)
        try:
            self.dir.chmod(0o700)
        except OSError:
            pass

    # --- 状态文件 ------------------------------------------------------

    def read_state(self) -> dict | None:
        """读 state.json；损坏或缺失返回 None（由调用方决定如何处置）。"""
        try:
            raw = self.state.read_text("utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def write_state(self, payload: dict) -> None:
        """原子写 state.json。**这个文件不参与 is_ours() 的命令行比对来源**，
        它只是记录"我们曾经启动过什么"。"""
        tmp = self.state.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.state)

    def clear_state(self) -> None:
        """清除陈旧状态。`missing_ok` 语义，重复调用安全。"""
        for path in (self.state, self.pidfile):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # --- 启动日志 ------------------------------------------------------

    def new_startup_log(self) -> Path:
        """新开一份启动日志，并修剪到最近 STARTUP_KEEP 份（ADR-5）。"""
        self.startup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.startup_dir / f"startup-{stamp}-{os.getpid()}.log"
        # 先修剪到 KEEP-1，再创建新文件 → 最终恰好保留 KEEP 份
        self._prune_startup_logs(keep=STARTUP_KEEP - 1)
        return path

    def latest_startup_log(self) -> Path | None:
        logs = sorted(self.startup_dir.glob("startup-*.log"))
        return logs[-1] if logs else None

    def _prune_startup_logs(self, *, keep: int) -> None:
        logs = sorted(self.startup_dir.glob("startup-*.log"))
        for stale in logs[: max(0, len(logs) - keep)]:
            try:
                stale.unlink()
            except OSError:
                pass

    # --- 备份历史 ------------------------------------------------------

    def next_history_slot(self) -> Path:
        """返回下一个快照目录（序号递增，序号最大的最新）。

        用序号而非时间戳：同一秒内连续两次变更不会撞名，rollback N 的语义
        也变成确定的"数第 N 个"。
        """
        self.history_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(self.history_dir.glob("[0-9][0-9][0-9][0-9]-*"))
        seq = 1
        if existing:
            seq = int(existing[-1].name.split("-", 1)[0]) + 1
        return self.history_dir / f"{seq:04d}-{time.strftime('%Y%m%d-%H%M%S')}"

    def history_entries(self) -> list[Path]:
        """所有快照，**从新到旧**（便于 rollback [N] 取第 N 个）。"""
        if not self.history_dir.exists():
            return []
        return sorted(self.history_dir.glob("[0-9][0-9][0-9][0-9]-*"), reverse=True)

    def prune_history(self) -> list[Path]:
        """保留最近 HISTORY_KEEP 份，返回被删除的目录。

        必须**递归**删除：每个快照目录里除了 `frps.toml` 还有 `meta.json`，
        只 unlink 配置再 `rmdir` 会因目录非空而静默失败——快照就会无限增长，
        而"保留 10 份"这条约定也不会生效。
        """
        import shutil

        removed: list[Path] = []
        for stale in self.history_entries()[HISTORY_KEEP:]:
            shutil.rmtree(stale, ignore_errors=True)
            removed.append(stale)
        return removed
