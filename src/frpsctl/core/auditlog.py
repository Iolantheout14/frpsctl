"""插件审计日志的**读取**（CLI `plugin audit` 与 Web 审计视图共用）。

与 `plugin/audit.py`（写入侧：异步队列 + JSONL 追加）的分工：本模块只读，
回答两个问题——"最近发生了什么"（tail）与"总共怎么分布"（stats）。它不引入
任何写入路径，因此 `plugin serve` 的登录链路完全不受影响。

**路径解析是唯一权威**（`resolve_audit_path`）：策略里 `audit.path` 的相对路径
一律相对**策略文件所在目录**解析。这修正了一个历史上不一致的行为——
`plugin serve` 手工前台运行时写到 CWD、systemd 托管时写到 WorkingDirectory
（= 实例目录），同一份策略因启动方式不同落在两个地方。现在两者一致：审计与
策略文件"结伴"，迁移实例不会漏掉审计。

**策略文件定位规则**（`resolve_policy_path`）同样只有这一份实现：`--policy`
参数 > `FRPSCTL_PLUGIN_POLICY` 环境变量 > `<实例目录>/plugin-policy.json`。
CLI 与 Web 因此指向同一个文件（此前只有 CLI 知道这条规则）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import UsageError
from .instance import Instance
from .logs import tail_lines

__all__ = [
    "DEFAULT_AUDIT_FILE",
    "DEFAULT_POLICY_FILE",
    "AuditSummary",
    "AuditTail",
    "AuditView",
    "load_view",
    "parse_since",
    "read_tail",
    "resolve_audit_path",
    "resolve_policy_path",
    "summarize",
]

#: 策略与审计的默认文件名（相对实例目录 / 策略文件目录）。
#: `plugin/policy.py` 的 `AuditSettings` 默认值引用这里，保证"写入侧的默认
#: 落点"与"读取侧的查找目标"是同一个常量。
DEFAULT_POLICY_FILE = "plugin-policy.json"
DEFAULT_AUDIT_FILE = "plugin-audit.jsonl"

#: `by_user` 统计表的上限：`user` 字段是客户端自报的任意字符串，拒绝风暴里
#: 攻击者可以喷出任意多的"用户名"（输入驱动的表必须有界——与 web/auth.py 的
#: 会话表、失败来源表同一条纪律）。超出部分并入 `(其他)`，总量不受影响。
MAX_STAT_USERS = 200

#: `by_op` 的上限。`op` 来自请求 URL 的 query（未知 op 会被引擎拒绝，但**拒绝
#: 本身也会写审计**）——恶意请求可以制造任意 op 字符串，因此同样必须有界。
MAX_STAT_OPS = 64

#: `(其他)` 归类桶的键名。
_OTHER = "(其他)"


def resolve_policy_path(inst: Instance, override: Path | None = None) -> Path:
    """插件策略文件路径：`--policy` > 环境变量 > 实例目录下的默认文件。

    默认放进实例目录，是为了让"这个实例的策略"跟它的配置、历史待在一起——
    迁移实例时不会漏掉鉴权规则。CLI 与 Web 必须走同一入口（此前这条规则只
    存在于 CLI 的私有函数里）。
    """
    if override is not None:
        return override.expanduser()
    env = os.environ.get("FRPSCTL_PLUGIN_POLICY")
    if env:
        return Path(env).expanduser()
    return inst.dir / DEFAULT_POLICY_FILE


def resolve_audit_path(policy_path: Path, raw: object) -> Path | None:
    """把策略里 `audit.path` 的原始值解析成审计文件路径。

    - `None` → `None`（"仅内存"是显式语义）；
    - 绝对路径 → 原样；
    - 相对路径 → **相对策略文件所在目录**（见模块文档的说明）。
    """
    if raw is None:
        return None
    if isinstance(raw, Path):
        text = str(raw)
    elif isinstance(raw, str):
        text = raw
    else:
        raise UsageError(f"audit.path 必须是字符串或 null，实际是 {type(raw).__name__}")
    if not text:
        return None
    candidate = Path(text).expanduser()
    return candidate if candidate.is_absolute() else policy_path.parent / candidate


@dataclass(frozen=True)
class AuditView:
    """审计配置的只读视图（`plugin audit stats` / Web 审计卡片的数据源）。

    `available=False` 表示策略文件缺失或不可解析（`reason` 说明原因）；
    `enabled=False` 表示策略里明确关闭了审计，或审计配置本身不合法。
    """

    policy_path: Path
    available: bool
    enabled: bool
    path: Path | None
    reason: str = ""


def load_view(inst: Instance, *, policy_override: Path | None = None) -> AuditView:
    """读策略文件并提取审计设置。

    **宽容解析**：这里的目标是"能不能展示审计"，而不是替 `plugin check` 做
    严格校验——策略文件缺失、JSON 坏、字段类型不对，都如实给出 `reason`，
    而不是抛异常让 Web 页面挂掉。字段名与默认值与 `plugin/policy.py` 的
    `AuditSettings.parse` 保持一致（有一条测试钉住两者不漂移）。
    """
    policy_path = resolve_policy_path(inst, policy_override)
    try:
        raw = json.loads(policy_path.read_text("utf-8"))
    except FileNotFoundError:
        return AuditView(policy_path, False, False, None, f"策略文件不存在：{policy_path}")
    except (OSError, json.JSONDecodeError) as exc:
        return AuditView(policy_path, False, False, None, f"策略文件不可读或不合法：{exc}")
    if not isinstance(raw, dict):
        return AuditView(policy_path, False, False, None, "策略文件根节点必须是对象")

    audit = raw.get("audit")
    if audit is None:
        audit = {}
    if not isinstance(audit, dict):
        return AuditView(
            policy_path, True, False, None, "audit 不是对象（用 `plugin check` 查看详情）"
        )
    enabled = audit.get("enabled", True)
    if not isinstance(enabled, bool):
        return AuditView(
            policy_path, True, False, None, "audit.enabled 不是布尔值（用 `plugin check` 查看详情）"
        )
    raw_path = audit.get("path", DEFAULT_AUDIT_FILE)
    if raw_path is not None and not isinstance(raw_path, str):
        return AuditView(
            policy_path, True, False, None, "audit.path 不是字符串（用 `plugin check` 查看详情）"
        )
    path = resolve_audit_path(policy_path, raw_path)
    return AuditView(policy_path, True, enabled, path)


@dataclass(frozen=True)
class AuditTail:
    """审计尾部若干条记录。坏行被跳过并计数（不因一行坏 JSON 丢掉整段历史）。"""

    path: Path
    records: list[dict] = field(default_factory=list)
    bad_lines: int = 0


def read_tail(path: Path, lines: int) -> AuditTail:
    """读审计文件尾部 `lines` 行并逐行解析（复用 `logs.tail_lines` 的反向读）。

    审计文件与日志同规格（追加写、按行 JSON），因此反扫实现共用——一个
    100MB 的审计文件同样只读最后一个块。
    """
    records: list[dict] = []
    bad = 0
    for line in tail_lines(path, lines):
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(item, dict):
            records.append(item)
        else:
            bad += 1
    return AuditTail(path=path, records=records, bad_lines=bad)


@dataclass(frozen=True)
class AuditSummary:
    """审计统计（流式扫描全文件的计数结果）。"""

    total: int = 0
    allow: int = 0
    deny: int = 0
    bad_lines: int = 0
    suppressed_total: int = 0
    first_at: float | None = None
    last_at: float | None = None
    by_user: dict[str, dict[str, int]] = field(default_factory=dict)
    by_op: dict[str, int] = field(default_factory=dict)
    #: 裁决耗时（`elapsed_ms`）——审计记录这个字段的目的就是回答"插件拖慢了
    #: 登录吗"，没有聚合出口时它等于没记。
    elapsed_avg_ms: float = 0.0
    elapsed_max_ms: float = 0.0
    elapsed_count: int = 0


def summarize(path: Path, *, since: float | None = None) -> AuditSummary:
    """流式统计审计文件（只计数，不驻留记录）。

    - `since` 是 unix 时间戳下界（`parse_since` 的输出）；缺 `at_unix` 字段的
      记录仍计入总量（它们来自更早版本的写入者，忽略等于丢证据）；
    - `by_user` 有上限（`MAX_STAT_USERS`），超出并入 `(其他)`；
    - 坏行计入 `bad_lines`——审计文件被截断（进程被 kill）时最后一行必然是
      半截 JSON，那是常态而非异常。
    """
    total = allow = deny = bad = suppressed = 0
    first_at: float | None = None
    last_at: float | None = None
    by_user: dict[str, dict[str, int]] = {}
    by_op: dict[str, int] = {}
    elapsed_total = 0.0
    elapsed_max = 0.0
    elapsed_count = 0

    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError:
        return AuditSummary()

    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(item, dict):
                bad += 1
                continue

            at = _as_unix(item.get("at_unix"))
            if since is not None and at is not None and at < since:
                continue
            if at is not None:
                first_at = at if first_at is None else min(first_at, at)
                last_at = at if last_at is None else max(last_at, at)

            total += 1
            if item.get("decision") == "deny":
                deny += 1
            else:
                allow += 1
            suppressed += int(item.get("suppressed") or 0)

            user = str(item.get("user") or "")
            key = user if len(by_user) < MAX_STAT_USERS or user in by_user else _OTHER
            bucket = by_user.setdefault(key, {"allow": 0, "deny": 0})
            bucket["deny" if item.get("decision") == "deny" else "allow"] += 1

            op = str(item.get("op") or "")
            op_key = op if len(by_op) < MAX_STAT_OPS or op in by_op else _OTHER
            by_op[op_key] = by_op.get(op_key, 0) + 1

            elapsed = item.get("elapsed_ms")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                elapsed_total += float(elapsed)
                elapsed_max = max(elapsed_max, float(elapsed))
                elapsed_count += 1

    return AuditSummary(
        total=total,
        allow=allow,
        deny=deny,
        bad_lines=bad,
        suppressed_total=suppressed,
        first_at=first_at,
        last_at=last_at,
        by_user=by_user,
        by_op=by_op,
        elapsed_avg_ms=(elapsed_total / elapsed_count) if elapsed_count else 0.0,
        elapsed_max_ms=elapsed_max,
        elapsed_count=elapsed_count,
    )


def _as_unix(value: object) -> float | None:
    """`at_unix` 字段（float 或 int，bool 除外）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


#: 相对时间写法：`30s` / `90m` / `24h` / `7d` / `2w`。
_RELATIVE_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}


def parse_since(spec: str, *, now: float | None = None) -> float:
    """把 `--since` 的三种写法统一成 unix 时间戳：

    - 相对时间：`24h` / `7d` / `30m` / `90s` / `2w`；
    - Unix 时间戳：`1758000000`；
    - ISO 8601：`2026-09-17T10:00:00`（本地时区，与 `at` 字段同格式）。

    解析失败一律用法错误(2)——"看不懂就当作没给"会让 `--since 24` 静默统计
    全量（用户以为限了窗口）。
    """
    text = spec.strip()
    if not text:
        raise UsageError(
            "--since 不能为空",
            hint="示例：--since 24h、--since 7d、--since 2026-09-17T10:00:00",
        )
    match = _RELATIVE_RE.match(text)
    if match:
        seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        return (now if now is not None else time.time()) - seconds
    try:
        return float(text)
    except ValueError:
        pass
    from datetime import datetime

    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise UsageError(
            f"无法解析 --since：{spec!r}",
            hint="支持相对时间（24h / 7d / 30m）、Unix 时间戳或 ISO 时间（2026-09-17T10:00:00）",
        ) from None
