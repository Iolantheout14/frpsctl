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
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import UsageError
from .instance import Instance
from .logs import tail_lines

__all__ = [
    "AUDIT_KEEP",
    "DEFAULT_AUDIT_FILE",
    "DEFAULT_POLICY_FILE",
    "MAX_QUERY_LIMIT",
    "rotate_if_needed",
    "rotated_paths",
    "AuditQuery",
    "AuditSummary",
    "AuditTail",
    "AuditView",
    "load_view",
    "parse_since",
    "query",
    "read_tail",
    "resolve_audit_path",
    "resolve_policy_path",
    "summarize",
    "to_csv",
    "to_jsonl",
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

#: 审计轮转：超过阈值时 `path` → `path.1`（`.1` → `.2`，最旧的删除）。
#: 保留份数固定为 2——审计是"最近发生了什么"的取证材料，两份历史足够翻查，
#: 而无限增长的文件会让磁盘与读取都失控（写侧原先完全没有轮转）。
AUDIT_KEEP = 2


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


def rotated_paths(path: Path) -> list[Path]:
    """按**新 → 旧**列出审计文件与它的轮转版本（只含存在的）。"""
    candidates = [path] + [Path(f"{path}.{index}") for index in range(1, AUDIT_KEEP + 1)]
    return [item for item in candidates if item.exists()]


def rotate_if_needed(
    path: Path,
    *,
    max_bytes: int = 0,
    max_age_seconds: float = 0,
    keep: int = AUDIT_KEEP,
    now: float | None = None,
) -> bool:
    """审计文件**超过大小或超过年龄**时轮转；返回是否发生了轮转。

    两个维度各自独立（0 = 该维度禁用）：`max_bytes` 防单文件无限增长，
    `max_age_seconds` 保证"太久以前的审计"会被归档轮换（与 frp 日志的
    `maxDays` 同语义）。任何一维触发即轮转。

    轮转失败（权限/磁盘）**不抛异常**——审计写入方（插件后台线程 / Web
    动作路径）绝不能被轮转问题拖垮；失败时继续向现有文件追加，下次再试。
    `now` 可注入，测试用假时钟或 `os.utime` 控制。
    """
    if max_bytes <= 0 and max_age_seconds <= 0:
        return False
    import time as _time

    try:
        st = path.stat()
    except OSError:
        return False
    due_size = max_bytes > 0 and st.st_size >= max_bytes
    due_age = max_age_seconds > 0 and (
        (now if now is not None else _time.time()) - st.st_mtime
    ) >= max_age_seconds
    if not (due_size or due_age):
        return False
    try:
        for index in range(keep, 0, -1):
            src = path if index == 1 else Path(f"{path}.{index - 1}")
            dst = Path(f"{path}.{index}")
            if dst.exists():
                dst.unlink()
            if src.exists():
                src.replace(dst)
    except OSError:
        return False
    return True


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
    100MB 的审计文件同样只读最后一个块。**跨轮转**：当前文件不足时向
    `.1` / `.2` 追溯（结果按时间升序，与单文件时的行为一致）。
    """
    # 每个文件各取 `lines` 行（最多 3 个文件，多读一点以保证跨文件凑够
    # **记录数**——坏行不应吃掉配额，v0.3.0 review 修正），合并后从最新行
    # 向前解析，凑够 `lines` 条有效记录为止。
    #
    # 口径说明：配额按文件计——同一文件尾部若全是坏行，该文件内更早的有效
    # 记录不会被跨行追溯（跨**文件**才有追溯）；`bad_lines` 只统计扫描窗口
    # （凑够记录前）内的坏行，是下界。
    chunks: list[list[str]] = []
    for item in rotated_paths(path):  # 新 → 旧
        part = tail_lines(item, lines)
        if part:
            chunks.append(part)
    ordered = [line for chunk in reversed(chunks) for line in chunk]  # 旧 → 新

    records: list[dict] = []
    bad = 0
    for line in reversed(ordered):  # 从最新行向前
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
            if len(records) >= lines:
                break
        else:
            bad += 1
    records.reverse()
    return AuditTail(path=path, records=records, bad_lines=bad)


#: 单次查询返回的记录上限（分页参数 `limit` 会被收敛到这个值）。
MAX_QUERY_LIMIT = 1000

#: 一次查询最多保留的**匹配**记录数（内存护栏）：超出时丢弃最旧的匹配并置
#: `truncated=True`——审计文件最大约 30MB（本体 + 2 份轮转），正常远达不到，
#: 但"输入驱动的内存"必须有界（与 by_user / by_op 同一条纪律）。
MAX_QUERY_MATCHES = 20000


@dataclass(frozen=True)
class AuditQuery:
    """过滤 + 分页后的审计记录（v0.3.5，Web 审计视图的过滤/翻页数据源）。

    口径：

    - `records` 是本页记录，按**时间升序**（与 `read_tail` 一致）；
    - `matched` 是扫描到的匹配总数（`truncated=True` 时是**下界**）；
    - `has_more` 表示再往旧翻还有匹配（前端"加载更多"按钮的依据）；
    - `bad_lines` 是扫描窗口内的坏行数（下界）。
    """

    records: list[dict] = field(default_factory=list)
    matched: int = 0
    bad_lines: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False
    truncated: bool = False


def _audit_matches(
    record: dict,
    *,
    since: float | None,
    until: float | None,
    filters: dict[str, str] | None,
) -> bool:
    """一条记录是否命中过滤（字段值**大小写不敏感子串**匹配）。"""
    at = _as_unix(record.get("at_unix"))
    if since is not None and at is not None and at < since:
        return False
    if until is not None and at is not None and at > until:
        return False
    for key, expected in (filters or {}).items():
        if not expected:
            continue
        actual = record.get(key)
        if isinstance(actual, (dict, list)):
            actual = json.dumps(actual, ensure_ascii=False)
        if expected.lower() not in str(actual if actual is not None else "").lower():
            return False
    return True


def query(
    path: Path,
    *,
    since: float | None = None,
    until: float | None = None,
    filters: dict[str, str] | None = None,
    limit: int = 200,
    offset: int = 0,
    max_limit: int = MAX_QUERY_LIMIT,
) -> AuditQuery:
    """跨轮转的**过滤 + 分页**查询（从新到旧偏移，返回页内时间升序）。

    与 `read_tail` 的差别：`read_tail` 回答"最近 N 条"，本函数回答"**符合条件**
    的第 offset..offset+limit 条"——审计视图的过滤与"加载更多"都依赖它。
    过滤键由调用方给出（Web 审计用 `action/result/source`，插件审计用
    `op/user/decision`），实现不做字段名假设。

    `max_limit` 只约束**本次 `limit`（页大小）**：界面分页用默认的
    `MAX_QUERY_LIMIT`，导出可显式放宽到 `MAX_EXPORT_ROWS`（此前导出被默认上限
    静默钳到 1000，却对外声称 10000——v0.3.5 review 抓到的真实缺陷）。
    内存保留窗口始终是 `MAX_QUERY_MATCHES`（与 `max_limit` 无关）。
    """
    hard_cap = max(1, int(max_limit))
    limit = max(1, min(int(limit), hard_cap))
    offset = max(0, int(offset))
    # deque(maxlen=…)：超过上限时头部丢弃是 O(1)。此前用 list + `del [0]`，
    # 每条超限记录都要搬移 2 万个指针（实测 60 万行查询 1.6 秒，且持 GIL
    # 阻塞同进程其他请求）——v0.3.5 review 的性能修复。
    matched: deque[dict] = deque(maxlen=MAX_QUERY_MATCHES)
    bad = 0
    truncated = False
    for item in reversed(rotated_paths(path)):  # 旧 → 新（保持时间序）
        try:
            handle = open(item, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
        except OSError:
            continue
        with handle:
            for raw in handle:
                text = raw.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if not isinstance(record, dict):
                    bad += 1
                    continue
                if not _audit_matches(record, since=since, until=until, filters=filters):
                    continue
                if len(matched) == matched.maxlen:
                    truncated = True
                matched.append(record)
    newest_first = list(reversed(matched))  # deque → list（已是时间升序）
    page = newest_first[offset : offset + limit]
    # `has_more` 只回答"本页之后还有（在扫描窗口内的）记录吗"——它决定前端是否
    # 显示"加载更多"。**不能**把 `truncated` 并进来：那会让按钮永不消失、每次
    # 点击都空转并触发一次全量扫描（v0.3.5 复审抓到的副作用）。"扫描窗口之外
    # 可能还有更旧的记录"由 `truncated` 单独表达，前端用文字提示（缩小时间窗）。
    has_more = len(newest_first) > offset + limit
    return AuditQuery(
        records=list(reversed(page)),
        matched=len(matched),
        bad_lines=bad,
        offset=offset,
        limit=limit,
        has_more=has_more,
        truncated=truncated,
    )


#: CSV 导出的列（插件审计与 Web 审计字段的**并集**；缺列留空）。
_CSV_COLUMNS = (
    "at",
    "at_unix",
    "op",
    "decision",
    "user",
    "proxy_name",
    "proxy_type",
    "remote_port",
    "source",
    "elapsed_ms",
    "quota_source",
    "suppressed",
    "action",
    "target",
    "result",
    "params",
    "session_id",
)

#: 纯数值形态（负数/小数不加前缀——它们的首字符是 `-`，但不可能被当公式）。
_CSV_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

#: 直接被 Excel/Sheets 当公式求值的首字符（含制表/换行/垂直制表/表单/空字符——
#: 它们会被忽略后再看下一个字符，`"\t=1+1"` 同样危险）。
_CSV_DANGEROUS_FIRST = "=+-@\t\r\n\v\f\x00"
#: 前导空白之后仍会触发公式的字符。
_CSV_DANGEROUS_AFTER_SPACE = "=+-@"


def _csv_cell(value: object) -> str:
    """单元格渲染（含公式注入防护与容器类型 JSON 化）。

    规则：**危险首字符**（`=`/`+`/`-`/`@`/各类空白与控制符）或"前导空白之后
    仍是危险字符"的单元格前置 `'`；纯数值（含负数、科学计数）放行，避免把
    `-5` 变成文本；容器类型先 JSON 化（`{`/`[` 开头本身安全）。
    """
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    elif value is None:
        text = ""
    else:
        text = str(value)
    if not text or _CSV_NUMERIC_RE.fullmatch(text):
        return text
    # ⚠️ lstrip 的集合**不能包含 `=+-@`**：否则 stripped 的首字符永远不可能是
    # 危险字符，`_CSV_DANGEROUS_AFTER_SPACE` 分支就成了死代码（" =1+1" 会漏过）。
    stripped = text.lstrip(" \t\r\n\v\f\x00")
    if text[0] in _CSV_DANGEROUS_FIRST or stripped[:1] in _CSV_DANGEROUS_AFTER_SPACE:
        text = "'" + text
    return text


def to_jsonl(records: list[dict]) -> str:
    """JSONL 导出（逐行原始 JSON，与审计文件同格式，可被 jq/grep 直接消费）。"""
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def to_csv(records: list[dict]) -> str:
    """CSV 导出（列 = 两类审计字段的并集；公式注入已防护）。"""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)
    for record in records:
        writer.writerow([_csv_cell(record.get(column)) for column in _CSV_COLUMNS])
    return buffer.getvalue()


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


def summarize(
    path: Path, *, since: float | None = None, until: float | None = None
) -> AuditSummary:
    """跨轮转文件的全量统计（`path` + `.1` + `.2`）。

    `since` / `until` 是闭开时间窗（v0.3.5 补 `until`：审计视图的"自定义起止"
    要能同时限定两端，否则统计口径会与记录列表不一致）。
    """
    accumulator = _SummaryAccumulator()
    files = rotated_paths(path)
    if not files:
        return AuditSummary()
    for item in files:
        accumulator.consume_file(item, since=since, until=until)
    return accumulator.build()


class _SummaryAccumulator:
    """流式统计的累加器（跨多个轮转文件共用一份计数状态）。"""

    def __init__(self) -> None:
        self.total = self.allow = self.deny = self.bad = self.suppressed = 0
        self.first_at: float | None = None
        self.last_at: float | None = None
        self.by_user: dict[str, dict[str, int]] = {}
        self.by_op: dict[str, int] = {}
        self.elapsed_total = 0.0
        self.elapsed_max = 0.0
        self.elapsed_count = 0

    def consume_file(
        self, path: Path, *, since: float | None, until: float | None = None
    ) -> None:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
        except OSError:
            return
        with handle:
            for line in handle:
                self._consume_line(line, since=since, until=until)

    def _consume_line(self, raw: str, *, since: float | None, until: float | None = None) -> None:
        text = raw.strip()
        if not text:
            return
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            self.bad += 1
            return
        if not isinstance(item, dict):
            self.bad += 1
            return

        at = _as_unix(item.get("at_unix"))
        if since is not None and at is not None and at < since:
            return
        if until is not None and at is not None and at > until:
            return
        if at is not None:
            self.first_at = at if self.first_at is None else min(self.first_at, at)
            self.last_at = at if self.last_at is None else max(self.last_at, at)

        self.total += 1
        if item.get("decision") == "deny":
            self.deny += 1
        else:
            self.allow += 1
        self.suppressed += int(item.get("suppressed") or 0)

        user = str(item.get("user") or "")
        key = user if len(self.by_user) < MAX_STAT_USERS or user in self.by_user else _OTHER
        bucket = self.by_user.setdefault(key, {"allow": 0, "deny": 0})
        bucket["deny" if item.get("decision") == "deny" else "allow"] += 1

        op = str(item.get("op") or "")
        op_key = op if len(self.by_op) < MAX_STAT_OPS or op in self.by_op else _OTHER
        self.by_op[op_key] = self.by_op.get(op_key, 0) + 1

        elapsed = item.get("elapsed_ms")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            self.elapsed_total += float(elapsed)
            self.elapsed_max = max(self.elapsed_max, float(elapsed))
            self.elapsed_count += 1

    def build(self) -> AuditSummary:
        return AuditSummary(
            total=self.total,
            allow=self.allow,
            deny=self.deny,
            bad_lines=self.bad,
            suppressed_total=self.suppressed,
            first_at=self.first_at,
            last_at=self.last_at,
            by_user=self.by_user,
            by_op=self.by_op,
            elapsed_avg_ms=(self.elapsed_total / self.elapsed_count) if self.elapsed_count else 0.0,
            elapsed_max_ms=self.elapsed_max,
            elapsed_count=self.elapsed_count,
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
        parsed = float(text)
    except ValueError:
        pass
    else:
        if not math.isfinite(parsed):
            raise UsageError(
                f"无法解析时间：{spec!r}",
                hint="时间戳必须是有限数值（nan / inf 不是合法时间窗）",
            )
        return parsed
    from datetime import datetime

    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise UsageError(
            f"无法解析 --since：{spec!r}",
            hint="支持相对时间（24h / 7d / 30m）、Unix 时间戳或 ISO 时间（2026-09-17T10:00:00）",
        ) from None
