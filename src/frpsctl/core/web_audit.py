"""Web 管理台操作审计（设计文档 §18，0.3.0 新增）。

管理台能改配置、停服务、回滚——这些操作此前**没有任何留痕**：config 快照只
记录"配置变了"这一事实，而"谁点的、从哪来、成功没有"无处可查。与插件审计
（`plugin/audit.py` 写侧 + `core/auditlog.py` 读侧）同规格：JSONL、`at_unix`
可排序、坏行不致命、读侧复用反向 tail。

**同步写，且失败不阻断**：Web 操作是低频人工动作（不是插件登录那条每个请求
都走的热路径），追加一行（<1ms）换"记录先于响应"的确定性；磁盘不可用时绝
不能让管理台的动作失败——返回 False，由调用方在 stderr 如实告警（降级必须
可见）。

**session_id 只存哈希前缀**：审计要能区分"同一个会话做了什么"，但会话 token
本身是凭据，绝不落盘。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .instance import Instance

__all__ = [
    "DEFAULT_WEB_AUDIT_FILE",
    "WebAuditSummary",
    "record",
    "resolve_path",
    "session_fingerprint",
    "summarize",
]

#: 审计文件名（实例目录内，与插件审计并列）。
DEFAULT_WEB_AUDIT_FILE = "web-audit.jsonl"

#: 写侧互斥：`ThreadingHTTPServer` 下两个并发动作可能同时走到"轮转 + 追加"
#: ——没有锁时 rename 序列会交错，归档文件可能被对方删掉（v0.3.0 review）。
_io_lock = threading.Lock()

#: 轮转阈值：Web 操作是低频事件，10MB 约等于数万条操作——足够久远，也
#: 不会让磁盘无人看管地增长。
DEFAULT_WEB_AUDIT_MAX_BYTES = 10 * 1024 * 1024

#: 按天轮转：超过该天数的审计归档轮换（0 = 禁用）。与 frp 日志 maxDays 同语义。
DEFAULT_WEB_AUDIT_MAX_AGE_DAYS = 7.0


def resolve_path(inst: Instance) -> Path:
    """Web 审计的落点：实例目录（与配置、快照、插件审计同处，迁移实例不丢）。"""
    return inst.dir / DEFAULT_WEB_AUDIT_FILE


def session_fingerprint(token: str | None) -> str:
    """会话指纹：sha256 前 12 位（可区分、不可反推 token）。"""
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def record(
    inst: Instance,
    *,
    action: str,
    target: str = "",
    params: dict[str, Any] | None = None,
    result: str = "ok",
    source: str = "",
    session_id: str = "",
) -> bool:
    """追加一条操作记录。返回是否成功落盘（失败**绝不抛异常**）。"""
    now = time.time()
    payload: dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "at_unix": round(now, 3),
        "action": action,
        "target": target,
        "params": params or {},
        "result": result,
        "source": source,
        "session_id": session_id,
    }
    path = resolve_path(inst)
    try:
        # 轮转 + 追加在**同一把锁**内：并发写入不会一个在写旧文件、另一个
        # 刚把它改名（插件侧 flush 用同一条纪律）。
        with _io_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            from .auditlog import rotate_if_needed

            rotate_if_needed(
                path,
                max_bytes=DEFAULT_WEB_AUDIT_MAX_BYTES,
                max_age_seconds=DEFAULT_WEB_AUDIT_MAX_AGE_DAYS * 86400.0,
            )
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
    except (OSError, TypeError, ValueError):
        # OSError：磁盘/权限；TypeError/ValueError：params 里有不可序列化或
        # 非法值（承诺"绝不抛异常"必须覆盖它们，v0.3.0 最终 review N6）。
        return False
    return True


@dataclass(frozen=True)
class WebAuditSummary:
    """Web 操作审计的统计（流式扫描，不驻留记录）。"""

    total: int = 0
    ok: int = 0
    error: int = 0
    bad_lines: int = 0
    first_at: float | None = None
    last_at: float | None = None
    #: 按动作计数（`action` 来自固定集合，天然有界）。
    by_action: dict[str, int] = field(default_factory=dict)
    #: 按来源计数（来源是 IP，输入驱动——上限保护与 auditlog 同一条纪律）。
    by_source: dict[str, int] = field(default_factory=dict)


#: 来源表上限：地址是输入驱动的（可伪造 XFF 制造任意来源），必须有界。
MAX_SOURCES = 200


def summarize(path: Path, *, since: float | None = None) -> WebAuditSummary:
    """流式统计 Web 操作审计（`--since` 与插件审计同语义）。"""
    total = ok = error = bad = 0
    first_at: float | None = None
    last_at: float | None = None
    by_action: dict[str, int] = {}
    by_source: dict[str, int] = {}

    from .auditlog import rotated_paths

    files = rotated_paths(path)
    if not files:
        return WebAuditSummary()
    for item_path in files:
        try:
            handle = open(item_path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
        except OSError:
            continue
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
                at = item.get("at_unix")
                if isinstance(at, bool) or not isinstance(at, (int, float)):
                    at = None
                if since is not None and at is not None and at < since:
                    continue
                if at is not None:
                    first_at = at if first_at is None else min(first_at, float(at))
                    last_at = at if last_at is None else max(last_at, float(at))
                total += 1
                if str(item.get("result") or "") == "ok":
                    ok += 1
                else:
                    error += 1
                action = str(item.get("action") or "")
                by_action[action] = by_action.get(action, 0) + 1
                source = str(item.get("source") or "")
                key = source if len(by_source) < MAX_SOURCES or source in by_source else "(其他)"
                by_source[key] = by_source.get(key, 0) + 1

    return WebAuditSummary(
        total=total,
        ok=ok,
        error=error,
        bad_lines=bad,
        first_at=first_at,
        last_at=last_at,
        by_action=by_action,
        by_source=by_source,
    )
