"""Web 管理台的 JSON API（设计文档 §18.2）。

所有 handler 只调 `core/`，返回 `(status, payload)`；异常由 `dispatch` 统一
映射为 HTTP 状态码——错误消息与 CLI 走**同一套** `FrpsctlError.render()`，
机密不会出现在响应里（core 层已保证）。

变更类操作的语义与 CLI 完全一致（同一套 core 函数）：

| 操作 | core 入口 | 与 CLI 的对应 |
|------|-----------|--------------|
| start / stop / restart | `Lifecycle` | 同名命令（含健康门控） |
| 配置预览 / 应用 | `plan_set_many` + `apply_sets`（CAS） | `config set` 的多键形态 |
| 回滚 | `rollback_to` | `config rollback` |
| 清理离线记录 | `AdminClient.clear_offline_proxies` | `prune` |

**配置编辑的两段式交互**（预览 → 应用）是 Web 特有的安全设计：预览时服务端
在锁内取"当前文件"的快照与 diff（打码后下发），应用时以 `expected_current`
做 CAS——页面开着的时候配置文件被 CLI 改过，应用会被拒绝而不是覆盖。
未打码的配置原文从不离开服务端。
"""

from __future__ import annotations

import contextlib
import json
import math
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from .. import report as report_mod
from ..core import auditlog
from ..core import config as cfg
from ..core import doctor as doc
from ..core import serve_guard
from ..core import serve_runtime
from ..core.admin import (
    TRAFFIC_MAX_PROXIES,
    AdminClient,
    aggregate_days,
    fetch_histories,
    traffic_total,
)
from ..core.healthcheck import parse_dashboard
from ..core.instance import Instance
from ..core.lifecycle import HealthReport, Lifecycle, State
from ..core.lock import instance_lock
from ..core.logs import resolve_log_target, tail_lines, tail_since
from ..core.transaction import apply_sets, rollback_to, snapshot_diff
from ..core import web_audit
from ..errors import (
    AdminUnreachable,
    ConfigError,
    ExitCode,
    FrpsctlError,
    UsageError,
)
from .auth import AuthManager, LoginAuditLimiter
from .cache import TTLCache
from .tasks import TaskRegistry

__all__ = ["WebContext", "dispatch", "http_status_for"]

#: 配置预览的存活时间：超过后必须重新预览（防"看着旧 diff 点应用"）。
PREVIEW_TTL = 600.0

#: 未消费的预览条目上限：已登录用户正常交互远小于它；上限防内存堆积。
MAX_PENDING_PREVIEWS = 32

#: 日志接口一次最多返回的行数（防止把大日志一次性拉爆浏览器）。
MAX_LOG_LINES = 2000

#: 各只读端点的服务端缓存时间（秒）。浏览器每 5 秒轮询，而 dashboard 查询
#: （clients/proxies 各一次全量翻页、traffic 最多 50+ 次并发查询）与日志文件
#: 读取都没有必要按秒重放；写操作会显式失效（见 action_payload / config_apply）。
#:
#: ⚠️ TTL 必须**大于等于**轮询间隔才有意义（v0.3.0 量化验证实测：2 秒 TTL 在
#: 5 秒轮询下命中率≈0，等于没有缓存）。列表取 6 秒：5 秒轮询几乎每轮命中上
#: 一轮，而"改完立刻可见"由写失效保证——TTL 只影响外部变化的可见延迟（≤6s）。
CACHE_TTL_LISTS = 6.0
CACHE_TTL_LOGS = 3.0
#: 趋势是逐日粒度：30 秒内重复查询只是把 50 个 HTTP 往返重复一遍。
TRAFFIC_CACHE_TTL = 30.0

#: `/api/status` 的服务端缓存时间（秒，v0.3.1）。与列表同口径：TTL 必须
#: **大于等于**浏览器轮询间隔（5s）才有意义；状态变化在写操作路径上即时失效，
#: 外部变化最多滞后一个 TTL（≤6s，README"已知边界"已注明）。
STATUS_CACHE_TTL = 6.0

#: 审计视图返回的记录条数（尾部）。
AUDIT_TAIL_LINES = 50

#: 代理名的长度上限（防御性；代理名来自 URL 路径片段）。
MAX_PROXY_NAME = 256


@dataclass
class _Preview:
    expected_current: str
    changes: tuple[tuple[str, str], ...]
    created_at: float
    #: 删除键（`config unset` 语义），与 changes 在同一事务里落盘。
    unsets: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestInfo:
    """一次变更请求的来源信息（用于 Web 操作审计；只读请求不携带）。"""

    source: str = ""
    session_id: str = ""


def _audit(ctx: WebContext, info: RequestInfo | None, *, action: str, target: str = "",
           params: dict[str, Any] | None = None, result: str = "ok") -> None:
    """记一条 Web 操作审计；落盘失败**不阻断动作**，但必须可见（stderr）。"""
    ok = web_audit.record(
        ctx.inst,
        action=action,
        target=target,
        params=params,
        result=result,
        source=info.source if info else "",
        session_id=info.session_id if info else "",
    )
    if not ok:
        import contextlib
        import sys

        with contextlib.suppress(OSError):
            sys.stderr.write("[web] ⚠ Web 操作审计写入失败（动作本身已执行）：检查实例目录权限与磁盘空间\n")
            sys.stderr.flush()


@dataclass
class WebContext:
    """一个 Web 服务进程的共享上下文（可注入 clock 供测试控制 TTL）。"""

    inst: Instance
    auth: AuthManager
    clock: Callable[[], float] = time.monotonic
    previews: dict[str, _Preview] = field(default_factory=dict)
    preview_lock: threading.Lock = field(default_factory=threading.Lock)
    #: 失败登录的审计限速（v0.3.1：每上下文一份，不再是模块级单例——
    #: 模块级状态让多实例/测试互相串味）。
    login_audit: LoginAuditLimiter = field(default_factory=LoginAuditLimiter)
    #: 只读端点的响应缓存（traffic/lists/logs）。经 lambda 取 clock：测试
    #: monkeypatch `ctx.clock` 后缓存同样读到假时钟，无需重建上下文。
    cache: TTLCache = field(init=False)
    #: 进程级复用的 Lifecycle：owner 探测有实例级 2s TTL，5 秒轮询不必
    #: 每轮 fork 两个 systemctl（见 `Lifecycle.resolve_owner`）。
    _lifecycle_instance: object | None = field(init=False, default=None)
    #: 后台任务表（v0.3.4：版本安装；单飞行 + 有界 + clock 可注入）
    tasks: TaskRegistry = field(default_factory=TaskRegistry)

    def __post_init__(self) -> None:
        self.cache = TTLCache(lambda: self.clock())
        self._lifecycle_instance = None

    def lifecycle(self) -> Lifecycle:
        """本 Web 进程共享的 `Lifecycle` 实例（懒创建）。"""
        if self._lifecycle_instance is None:
            self._lifecycle_instance = Lifecycle(self.inst)
        assert isinstance(self._lifecycle_instance, Lifecycle)
        return self._lifecycle_instance

    def prune_previews(self) -> None:
        """清理过期预览；超过上限时丢弃最旧的（防内存堆积）。"""
        now = self.clock()
        with self.preview_lock:
            for key in [
                key for key, item in self.previews.items() if now - item.created_at > PREVIEW_TTL
            ]:
                self.previews.pop(key, None)
            while len(self.previews) > MAX_PENDING_PREVIEWS:
                oldest = min(self.previews, key=lambda key: self.previews[key].created_at)
                self.previews.pop(oldest, None)


# ---------------------------------------------------------------------------
# 路由分发（server 只负责 HTTP 细节）
# ---------------------------------------------------------------------------


def dispatch(
    ctx: WebContext,
    *,
    method: str,
    path: str,
    query: dict[str, str],
    body: dict[str, Any],
    authenticated: bool,
    request_info: RequestInfo | None = None,
) -> tuple[int, dict]:
    """把一次 API 调用分发给对应 handler，并统一映射异常。

    认证/CSRF 的**强制**由 server 层在调用本函数前完成（它管 HTTP 细节），
    这里只按路径分发（`POST /api/login` 是唯一免认证的入口）。
    """
    try:
        return _route(
            ctx,
            method=method,
            path=path,
            query=query,
            body=body,
            authenticated=authenticated,
            request_info=request_info,
        )
    except FrpsctlError as exc:
        return http_status_for(exc), {
            "error": exc.message,
            "hint": exc.hint or "",
        }
    except (ValueError, TypeError) as exc:  # 请求体形状错误（如 changes 不是数组）
        return 400, {"error": f"请求格式错误：{exc}", "hint": ""}
    except Exception as exc:  # noqa: BLE001 - 兜底：不外泄细节，只报类型
        return 500, {"error": f"服务内部错误：{type(exc).__name__}", "hint": "查看 web 服务日志"}


#: `ExitCode` → HTTP 状态码的**单点映射**（表驱动；改动即契约变化）。
#: 与 CLI 退出码同源：脚本与前端都据此分支，新增退出码时忘记补映射会落到 500
#: （默认分支），而不是被无声地按某个旧码归类。
_HTTP_STATUS_BY_EXIT: dict[ExitCode, int] = {
    ExitCode.OK: 200,
    ExitCode.UNCLASSIFIED: 500,
    ExitCode.USAGE: 400,
    ExitCode.CONFIG_INVALID: 400,
    ExitCode.BINARY: 400,
    ExitCode.NOT_RUNNING: 409,
    ExitCode.ALREADY_RUNNING: 409,
    ExitCode.ADMIN_UNREACHABLE: 502,
    ExitCode.PERMISSION: 403,
    ExitCode.ROLLED_BACK: 502,
    ExitCode.STARTUP_FAILED: 502,
    ExitCode.OWNERSHIP_CONFLICT: 409,
    ExitCode.UNHEALTHY: 502,
}


def http_status_for(exc: FrpsctlError) -> int:
    """`FrpsctlError` → HTTP 状态码（语义对齐，脚本与前端都据此分支）。"""
    if isinstance(exc, AdminUnreachable):
        return 502  # 上游 dashboard 不可达/未鉴权（与 exit code 7 同义）
    return _HTTP_STATUS_BY_EXIT.get(exc.exit_code, 500)


def _route(
    ctx: WebContext,
    *,
    method: str,
    path: str,
    query: dict[str, str],
    body: dict[str, Any],
    authenticated: bool,
    request_info: RequestInfo | None = None,
) -> tuple[int, dict]:
    if method == "POST" and path == "/api/login":
        # 登录/登出由 server 层直接处理（涉及 Set-Cookie 的 HTTP 细节），
        # 走到这里说明 server 的路由与 api 的定义漂移了。
        return 500, {"error": "登录应由 server 层处理（路由漂移）"}
    if not authenticated:
        return 401, {"error": "未登录", "hint": "请先登录"}

    if method == "GET":
        if path == "/api/status":
            return 200, status_payload(ctx)
        if path == "/api/clients":
            return 200, clients_payload(ctx)
        if path.startswith("/api/clients/"):
            return 200, client_detail_payload(ctx, unquote(path[len("/api/clients/"):]))
        if path == "/api/proxies":
            return 200, proxies_payload(ctx)
        if path.startswith("/api/proxies/"):
            return 200, proxy_detail_payload(ctx, unquote(path[len("/api/proxies/"):]))
        if path == "/api/services":
            return 200, services_payload(ctx)
        if path == "/api/versions":
            return 200, versions_payload(ctx)
        if path == "/api/tasks":
            return 200, {"tasks": ctx.tasks.list_payloads()}
        if path.startswith("/api/tasks/") and path != "/api/tasks/install":
            # install 只接受 POST；GET 它应当是 404（而不是"任务不存在"）
            return 200, task_payload(ctx, path[len("/api/tasks/"):])
        if path == "/api/traffic":
            return 200, traffic_payload(ctx)
        if path.startswith("/api/traffic/"):
            return 200, traffic_one_payload(ctx, unquote(path[len("/api/traffic/") :]))
        if path == "/api/doctor":
            return 200, doctor_payload(ctx)
        if path == "/api/audit":
            return 200, audit_payload(ctx, query.get("scope", "plugin"), query.get("since", ""))
        if path == "/api/config":
            return 200, config_payload(ctx)
        if path == "/api/config/history":
            return 200, history_payload(ctx)
        if path.startswith("/api/config/history/") and path.endswith("/diff"):
            steps = path[len("/api/config/history/") : -len("/diff")]
            return 200, history_diff_payload(ctx, steps)
        if path == "/api/logs":
            return 200, logs_payload(ctx, query.get("lines"), query.get("since"))
        return 404, {"error": f"未知接口：GET {path}"}

    if method == "POST":
        if path.startswith("/api/actions/"):
            return 200, action_payload(ctx, path.rsplit("/", 1)[-1], body, request_info=request_info)
        if path == "/api/config/preview":
            return 200, config_preview(ctx, body)
        if path == "/api/config/apply":
            return 200, config_apply(ctx, body, request_info=request_info)
        if path == "/api/tasks/install":
            return 200, task_install(ctx, body, request_info=request_info)
        return 404, {"error": f"未知接口：POST {path}"}

    return 405, {"error": f"不支持的请求方法：{method}"}


# ---------------------------------------------------------------------------
# 只读
# ---------------------------------------------------------------------------


def _admin(ctx: WebContext) -> AdminClient:
    dash = parse_dashboard(ctx.inst.config)
    if not dash.enabled:
        raise AdminUnreachable(
            "dashboard 未启用（webServer.port = 0），无法读取统计",
            hint="在配置里设置 webServer.port",
        )
    return AdminClient(dash.base_url, dash.user, dash.password)


def _health(report: HealthReport | None) -> dict | None:
    """三层健康形状（0.3.0 起与 CLI 共用 `frpsctl.report.health_payload`）。"""
    return report_mod.health_payload(report)


def status_payload(ctx: WebContext) -> dict:
    """进程状态 + dashboard 统计（统计不可得时为 None，不影响状态本身）。

    带 6 秒服务端缓存（v0.3.1）：`/api/status` 是页面 5 秒轮询的主接口，而每次
    生成都要做 L2/L3 网络探针 + 一次 `server_info`——TTL 必须 ≥ 轮询间隔才有
    意义（v0.3.0 量化验证的教训），写操作经 `cache.invalidate()` 保证即时可见。
    """
    cached = ctx.cache.get("status", STATUS_CACHE_TTL)
    if cached is not None:
        return cached
    report = ctx.lifecycle().status()
    dashboard: dict[str, Any] | None = None
    if report.state in (State.RUNNING, State.SYSTEMD_ACTIVE):
        try:
            admin = _admin(ctx)
            with admin:
                info = admin.server_info()
        except FrpsctlError:
            pass  # 统计拿不到不影响状态展示
        else:
            dashboard = {
                "clients": info.client_counts,
                "proxy_type_counts": info.proxy_type_counts,
                "proxy_total": info.proxy_total,
                "cur_conns": info.cur_conns,
                "traffic_in": info.total_traffic_in,
                "traffic_out": info.total_traffic_out,
                "tls_force": info.tls_force,
                "version": info.version,
            }
    # 与 CLI `status --json` 同一份形状（差异仅在 include_paths=False：
    # 页面不需要 binary/config 的本机路径），公共部分由 report 单点生成。
    payload = report_mod.status_payload(report, dashboard=dashboard, include_paths=False)
    ctx.cache.put("status", payload)
    return payload


def clients_payload(ctx: WebContext) -> dict:
    cached = ctx.cache.get("clients", CACHE_TTL_LISTS)
    if cached is not None:
        return cached
    admin = _admin(ctx)
    with admin:
        page = admin.page_clients()
    payload = {
        "clients": page.items,
        "total": page.total,
        "truncated": page.truncated,
        "total_known": page.total_known,
    }
    ctx.cache.put("clients", payload)
    return payload


def proxies_payload(ctx: WebContext) -> dict:
    cached = ctx.cache.get("proxies", CACHE_TTL_LISTS)
    if cached is not None:
        return cached
    admin = _admin(ctx)
    with admin:
        page = admin.page_proxies()
    payload = {
        "proxies": [asdict(item) for item in page.items],
        "total": page.total,
        "truncated": page.truncated,
        "total_known": page.total_known,
    }
    ctx.cache.put("proxies", payload)
    return payload


def traffic_payload(ctx: WebContext) -> dict:
    """全部代理的**逐日汇总**（趋势图数据源）。

    单代理的 7 天明细由 `GET /api/traffic/{name}` 按需提供（前端点开某行才请求）
    ——此前一次响应携带最多 50 个代理 × 7 天明细，而前端只用它画一张汇总柱状图，
    展开行时再从内存里挑。现在响应体与"代理数 × 天数"解耦。

    查询本身是**并发**的（`fetch_histories`）并带 30 秒服务端缓存：浏览器 5 秒
    轮询一次，而趋势是逐日粒度——没有缓存时每轮都会把最多 50 个 dashboard 请求
    重放一遍。

    `truncated` / `total` 如实汇报截断（CLI `traffic` 超限会告警"仅统计前 N 个"，
    Web 同样不能静默）。
    """
    cached = ctx.cache.get("traffic", TRAFFIC_CACHE_TTL)
    if cached is not None:
        return cached

    admin = _admin(ctx)
    with admin:
        page = admin.page_proxies()
        truncated = page.total > TRAFFIC_MAX_PROXIES
        names = [item.name for item in page.items[:TRAFFIC_MAX_PROXIES]]
        fetched = fetch_histories(admin, names)
    payload = {
        "granularity": "day",
        "days": aggregate_days(fetched.series),
        "proxies": len(names),
        "total": page.total,
        "truncated": truncated,
        # v0.3.1：整体预算内未取全时如实标记（前端在图表提示里展示）
        "partial": fetched.partial,
        "limit": TRAFFIC_MAX_PROXIES,
    }
    ctx.cache.put("traffic", payload)
    return payload


def traffic_one_payload(ctx: WebContext, name: str) -> dict:
    """单个代理的 7 天流量明细（趋势图"点开某行"的数据源）。

    离线/不存在的代理返回空 history（真机语义是 404 = 无数据）——这是常态
    （已下线客户端、待清理的离线记录），不该升级成错误。
    """
    name = name.strip()
    if not name:
        raise UsageError("代理名不能为空")
    if len(name) > MAX_PROXY_NAME:
        raise UsageError(f"代理名过长（>{MAX_PROXY_NAME} 字符）")
    admin = _admin(ctx)
    with admin:
        history = admin.proxy_traffic(name)
    return {
        "name": name,
        "granularity": "day",
        "history": history,
        "total": traffic_total(history),
    }


def doctor_payload(ctx: WebContext) -> dict:
    """只读体检（与 CLI `doctor` 同一实现、同一形状）。

    ⚠️ 体检以 **Web 服务进程的身份**运行：端口可绑定性、二进制可执行性这类
    检查的结果可能与 root 下的 CLI 结果不同——前端会注明这一点。
    """
    return report_mod.doctor_payload(doc.run_doctor(ctx.inst))


def _web_audit_payload(ctx: WebContext, window: float | None = None) -> dict:
    """Web 操作审计视图（0.3.0；v0.3.4 支持 `since` 时间窗）。路径 + 统计 + 尾部记录。"""
    path = web_audit.resolve_path(ctx.inst)
    if not path.exists():
        return {
            "scope": "web",
            "path": str(path),
            "available": False,
            "reason": "尚无 Web 操作记录（变更类操作与登录会写入这里）",
            "stats": None,
            "tail": [],
            "bad_lines": 0,
        }
    summary = web_audit.summarize(path, since=window)
    tail = auditlog.read_tail(path, AUDIT_TAIL_LINES)
    return {
        "scope": "web",
        "path": str(path),
        "available": True,
        "reason": "",
        "stats": {
            "total": summary.total,
            "ok": summary.ok,
            "error": summary.error,
            # 全量扫描的坏行（与 CLI `web audit stats` 同口径；顶层 bad_lines
            # 是尾部 50 行的口径，v0.3.0 review 消除两个同名不同义的字段）
            "bad_lines": summary.bad_lines,
            "first_at": summary.first_at,
            "last_at": summary.last_at,
            "by_action": summary.by_action,
            "by_source": summary.by_source,
        },
        "tail": tail.records,
        "bad_lines": tail.bad_lines,
    }


def audit_payload(ctx: WebContext, scope: str = "plugin", since_raw: str = "") -> dict:
    """审计视图：`scope=plugin`（默认，兼容）或 `scope=web`（操作审计）。

    `since`（v0.3.4）：`24h / 7d / 30m`、ISO 时间或 unix 时间戳——与 CLI
    `--since` 同一解析器（`auditlog.parse_since`）；非法值 400（不猜测）。

    策略文件缺失/不合法时也返回 200（`available=false` + reason）——审计视图
    的职责是"展示现状"，不是替 `plugin check` 做严格校验；一条 400 只会让页面
    失去"为什么看不到审计"的解释。非法 scope 同样是 400（不猜测调用意图）。
    """
    window: float | None = None
    if since_raw:
        window = auditlog.parse_since(since_raw)
    if scope == "web":
        return _web_audit_payload(ctx, window)
    if scope != "plugin":
        raise UsageError(f"未知的审计范围：{scope!r}", hint="可用：plugin / web")
    view = auditlog.load_view(ctx.inst)
    payload: dict[str, Any] = {
        "scope": "plugin",
        "policy_path": str(view.policy_path),
        "available": view.available,
        "enabled": view.enabled,
        "path": str(view.path) if view.path is not None else None,
        "reason": view.reason,
        "stats": None,
        "tail": [],
        "bad_lines": 0,
    }
    if view.available and view.enabled and view.path is not None:
        summary = auditlog.summarize(view.path, since=window)
        tail = auditlog.read_tail(view.path, AUDIT_TAIL_LINES)
        payload["stats"] = report_mod.audit_summary_payload(summary)
        payload["tail"] = tail.records
        payload["bad_lines"] = tail.bad_lines
    return payload


def _plain(value: Any) -> Any:
    """tomlkit 包装类型 → 可 JSON 化的原生值。"""
    return value.unwrap() if hasattr(value, "unwrap") else value


def config_payload(ctx: WebContext) -> dict:
    """配置树（打码后的值 + `masked` 标记）。**原文永不下发浏览器。**"""
    doc = cfg.load_config(ctx.inst.config)
    entries = []
    for key, value in cfg.flatten_tree(doc):
        secret = cfg.is_secret_key(key)
        entries.append(
            {
                "key": key,
                "value": cfg.mask_value(_plain(value)) if secret else _plain(value),
                "masked": secret,
            }
        )
    return {"entries": entries}


def history_payload(ctx: WebContext) -> dict:
    """配置快照列表（新 → 旧）。`steps` 与 `config rollback N` 语义一致。

    只读 `meta.json`（时间 / 动作 / 是否有配置），**不读快照里的配置原文**：
    快照是完整配置副本（含 token 与口令），没有理由把它们读进 Web 进程再考虑
    "要不要打码下发"。回滚仍由 `rollback_to` 在服务端执行（它与 CLI 共用）。
    """
    entries = []
    for index, entry in enumerate(ctx.inst.history_entries()):
        meta: dict = {}
        meta_path = entry / "meta.json"
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text("utf-8"))
                if isinstance(data, dict):
                    meta = data
            except (OSError, ValueError):
                meta = {}  # 元数据损坏不该让整个历史列表不可用
        entries.append(
            {
                "steps": index + 1,
                "name": entry.name,
                "at": str(meta.get("at") or ""),
                "action": str(meta.get("action") or ""),
                "has_config": (entry / "frps.toml").exists(),
            }
        )
    return {"entries": entries}


def history_diff_payload(ctx: WebContext, steps_raw: str) -> dict:
    """某份快照 vs 当前配置的 diff（打码后下发）。

    回滚是**危险操作**：看不到"会改什么"就确认，等于盲操作。CLI 的
    `config diff --steps N` 一直有这个能力，Web 此前没有——这里补上，
    并复用 core 的 `snapshot_diff`（与 CLI 同一实现、同一锁边界）。
    """
    try:
        steps = int(steps_raw)
    except ValueError:
        raise UsageError(f"快照步数必须是整数：{steps_raw!r}") from None
    if steps < 1:
        raise UsageError(f"快照步数必须 >= 1：{steps_raw!r}")
    result = snapshot_diff(ctx.inst, steps=steps)
    return {
        "steps": steps,
        "snapshot": result.snapshot.name,
        "diff": cfg.mask_diff(result.diff),
    }


def logs_payload(ctx: WebContext, lines_raw: str | None, since_raw: str | None = None) -> dict:
    """日志尾部（全量）或**增量**（`since` 为上次返回的字节 offset，v0.3.4）。

    - 全量（since 缺省/非法）：保留 3 秒服务端缓存（轮询命中友好），
      `reset=true` 且带当前 offset；
    - 增量：**不缓存**（每次结果都不同），只返回新增的完整行——
      长日志不再每 5 秒整段重传与重渲染。
    """
    try:
        lines = int(lines_raw) if lines_raw else 200
    except ValueError:
        lines = 200
    lines = max(1, min(lines, MAX_LOG_LINES))
    target = resolve_log_target(ctx.inst)
    since: int | None = None
    if since_raw:
        try:
            since = max(0, int(since_raw))
        except ValueError:
            since = None
    if since is None:
        cache_key = f"logs:{lines}"
        cached = ctx.cache.get(cache_key, CACHE_TTL_LOGS)
        if cached is not None:
            return cached
        try:
            offset = target.stat().st_size
        except OSError:
            # 拿不到大小 → 让前端下次也走全量（若给 0，增量会从文件头重复投递）
            offset = None
        payload = {
            "path": str(target),
            "lines": tail_lines(target, lines),
            "offset": offset,
            "reset": True,
        }
        ctx.cache.put(cache_key, payload)
        return payload
    new_lines, new_offset, reset = tail_since(target, since, max_lines=lines)
    return {"path": str(target), "lines": new_lines, "offset": new_offset, "reset": reset}


# ---------------------------------------------------------------------------
# 变更
# ---------------------------------------------------------------------------


def action_payload(
    ctx: WebContext,
    action: str,
    body: dict[str, Any],
    *,
    request_info: RequestInfo | None = None,
) -> dict:
    """执行一次变更动作，并写 Web 操作审计（成功与失败都留痕）。"""
    # 变更类操作：只读缓存（traffic/lists/logs）在动作前清空，保证界面刷新
    # 立刻看到新状态，而不是等 TTL 过期。
    ctx.cache.invalidate()
    try:
        payload = _perform_action(ctx, action, body)
    except Exception as exc:
        _audit(
            ctx,
            request_info,
            action=action,
            params=_audit_params(body),
            result=f"error:{type(exc).__name__}",
        )
        raise
    _audit(ctx, request_info, action=action, params=_audit_params(body))
    return payload


def _audit_params(body: dict[str, Any]) -> dict[str, Any]:
    """审计参数只保留标量（防未来动作塞入大对象/敏感结构）。"""
    out: dict[str, Any] = {}
    for key, value in body.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)] = value
        else:
            out[str(key)] = f"<{type(value).__name__}>"
    return out


def _perform_action(ctx: WebContext, action: str, body: dict[str, Any]) -> dict:
    lc = ctx.lifecycle()
    health_timeout = _bounded_float(body.get("health_timeout"), 10.0)

    if action in ("plugin-start", "plugin-stop", "plugin-restart"):
        return _plugin_service_action(ctx, action)
    if action == "start":
        report = lc.start(health_timeout=health_timeout)
        return report_mod.start_payload(report)
    if action == "stop":
        lc.stop(timeout=_bounded_float(body.get("timeout"), 10.0))
        return {"stopped": True}
    if action == "restart":
        report = lc.restart(health_timeout=health_timeout)
        return report_mod.start_payload(report)
    if action == "prune":
        # frp 没有强制下线在线代理的 API（DELETE /api/proxies 实为清理离线
        # 记录）——UI 的"清理离线记录"按钮走这里。返回清理条数（清理前后
        # 各数一次离线记录），让界面能说"清掉了多少"而不是永远只报成功。
        admin = _admin(ctx)
        with admin:
            outcome = admin.prune_offline_proxies()
        return {"cleared": True, "count": outcome.cleared, "before": outcome.before}
    if action == "rollback":
        outcome = rollback_to(
            ctx.inst,
            steps=_bounded_int(body.get("steps"), 1),
            lifecycle=lc,
            restart=True,
            health_timeout=health_timeout,
        )
        return {
            "target": str(outcome.after),
            "restarted": outcome.restarted,
            "diff": cfg.mask_diff(outcome.diff),
        }
    raise UsageError(f"未知操作：{action!r}")


def _normalize_changes(raw: Any) -> list[tuple[str, str]]:
    """接受 `[[key, value], ...]` 或 `[{key, value}, ...]`，其余形状直接拒绝。

    `None` 视为"没有这一项"（空数组同理）——整体非空由 `config_preview` 判定，
    因为单独的 `unsets` 也是合法变更。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise UsageError("changes 必须是数组")
    changes: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            changes.append((str(item[0]), str(item[1])))
        elif isinstance(item, dict) and "key" in item and "value" in item:
            changes.append((str(item["key"]), str(item["value"])))
        else:
            raise UsageError(f"无法识别的变更条目：{item!r}（应为 [key, value] 或 {{key, value}}）")
    return changes


def _normalize_unsets(raw: Any) -> list[str]:
    """`unsets`：要删除的键名数组（`config unset` 语义）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise UsageError("unsets 必须是数组")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise UsageError(f"无法识别的删除条目：{item!r}（应为非空键名字符串）")
        out.append(item.strip())
    return out


def config_preview(ctx: WebContext, body: dict[str, Any]) -> dict:
    """锁内取当前快照 + 生成打码 diff，并登记一次待应用的预览。"""
    changes = _normalize_changes(body.get("changes"))
    unsets = _normalize_unsets(body.get("unsets"))
    if not changes and not unsets:
        raise UsageError("changes 与 unsets 至少要有一个非空项")
    with instance_lock(ctx.inst.lock):
        current = cfg.read_config_text(ctx.inst.config)
        plan = cfg.plan_change_many(ctx.inst.config, changes, unsets)

    preview_id = secrets.token_urlsafe(16)
    ctx.prune_previews()
    with ctx.preview_lock:
        ctx.previews[preview_id] = _Preview(
            expected_current=current,
            changes=tuple(changes),
            unsets=tuple(unsets),
            created_at=ctx.clock(),
        )

    keys = []
    for key, _raw in changes:
        secret = cfg.is_secret_key(key)
        before = plan.before.get(key)
        after = plan.after.get(key)
        keys.append(
            {
                "key": key,
                "before": cfg.mask_value(before) if secret else before,
                "after": cfg.mask_value(after) if secret else after,
                "deleted": False,
            }
        )
    for key in unsets:
        secret = cfg.is_secret_key(key)
        before = plan.before.get(key)
        keys.append(
            {
                "key": key,
                "before": cfg.mask_value(before) if secret else before,
                "after": None,
                "deleted": True,
            }
        )
    return {
        "preview_id": preview_id,
        "noop": plan.is_noop,
        "diff": cfg.mask_diff(plan.diff),
        "keys": keys,
    }


def config_apply(
    ctx: WebContext,
    body: dict[str, Any],
    *,
    request_info: RequestInfo | None = None,
) -> dict:
    """按预览应用（CAS）：预览之后文件被改过 → 拒绝而不是覆盖。"""
    preview_id = str(body.get("preview_id") or "")
    ctx.cache.invalidate()
    ctx.prune_previews()
    with ctx.preview_lock:
        preview = ctx.previews.pop(preview_id, None)
    if preview is None:
        raise ConfigError("预览已过期或不存在", hint="请重新提交变更并预览")

    keys = [key for key, _ in preview.changes] + [f"-{key}" for key in preview.unsets]
    label = ", ".join(keys) or "(空变更)"
    try:
        outcome = apply_sets(
            ctx.inst,
            changes=preview.changes,
            unsets=preview.unsets,
            expected_current=preview.expected_current,
            lifecycle=ctx.lifecycle(),
            restart=True,
            health_timeout=10.0,
        )
    except Exception as exc:
        _audit(
            ctx,
            request_info,
            action="config-apply",
            target=label,
            result=f"error:{type(exc).__name__}",
        )
        raise
    _audit(ctx, request_info, action="config-apply", target=label, result="ok")
    return {
        "applied": outcome.applied,
        "restarted": outcome.restarted,
        "noop": outcome.noop,
        "diff": cfg.mask_diff(outcome.diff),
        "plugin_warning": outcome.plugin_warning,
        "note": outcome.note,
    }


def _bounded_float(value: Any, default: float, *, minimum: float = 0.0, maximum: float = 3600.0) -> float:
    """动作参数里的秒数：缺失/None → 默认值；给出但越界或非数值 → 用法错误(400)。

    **不能宽松回退默认**：`stop` 的 `timeout` 负值会让 `_wait_gone` 的 deadline
    落在过去、直接升级 SIGKILL（CLI 侧的同类问题在 v0.2.0 已修，这里是它的
    镜像）。参数笔误绝不该造成不可逆动作，因此越界一律拒绝并说明范围。

    `bool` 必须显式排除：`float(True) == 1.0`，否则 `{"timeout": true}` 会被
    当成"1 秒"静默接受（与 `_bounded_int` 同一条纪律）。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise UsageError(f"数值参数不能是布尔值：{value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise UsageError(f"数值参数非法：{value!r}") from None
    if math.isnan(parsed) or not (minimum <= parsed <= maximum):
        raise UsageError(f"数值越界（{minimum:g}..{maximum:g}）：{value!r}")
    return parsed


def _bounded_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 1000) -> int:
    """整数动作参数（rollback steps）：语义同 `_bounded_float`。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            text = str(value).strip()
            parsed = int(text)
        except (TypeError, ValueError):
            raise UsageError(f"整数参数非法：{value!r}") from None
    else:
        parsed = value
    if not (minimum <= parsed <= maximum):
        raise UsageError(f"整数越界（{minimum}..{maximum}）：{value!r}")
    return parsed


# ---------------------------------------------------------------------------
# v0.3.4 新增：详情 / 服务 / 版本 / 任务 / 诊断导出
# ---------------------------------------------------------------------------


def client_detail_payload(ctx: WebContext, key: str) -> dict:
    """单客户端详情（v2 透传；前端抽屉展示）。"""
    key = key.strip()
    if not key:
        raise UsageError("客户端 key 不能为空")
    if len(key) > MAX_PROXY_NAME:
        raise UsageError(f"客户端 key 过长（>{MAX_PROXY_NAME} 字符）")
    admin = _admin(ctx)
    with admin:
        detail = admin.client_detail(key)
    return {"key": key, "detail": detail}


def proxy_detail_payload(ctx: WebContext, name: str) -> dict:
    """单代理详情（v2 透传；离线/不存在返回空对象——与 traffic 的 404 语义一致）。"""
    name = name.strip()
    if not name:
        raise UsageError("代理名不能为空")
    if len(name) > MAX_PROXY_NAME:
        raise UsageError(f"代理名过长（>{MAX_PROXY_NAME} 字符）")
    admin = _admin(ctx)
    with admin:
        detail = admin.proxy_detail(name)
    return {"name": name, "detail": detail}


def services_payload(ctx: WebContext) -> dict:
    """三个服务的托管状态（frps / Web 管理台 / 服务端插件，v0.3.4 F1）。

    视图语义与 CLI `web status` 相同：systemd 优先、direct 次之；探测失败
    如实降级（`detail` 带错误文本，不猜）。
    """
    from ..core.systemd import PluginService, WebService

    inst = ctx.inst
    report = ctx.lifecycle().status()
    services: dict[str, dict[str, Any]] = {
        "frps": {
            "owner": report.owner.value,
            "active": report.state in (State.RUNNING, State.SYSTEMD_ACTIVE),
            "state": report.state.value,
            "pid": report.pid,
            "uptime_seconds": report.uptime_seconds,
            "listen": None
            if report.listen is None
            else {"addr": report.listen.addr, "port": report.listen.port},
            "systemd_unit": report.systemd_unit,
            "detail": report.systemd_probe_error or "",
        }
    }
    pairs = (
        (serve_runtime.WEB_SPEC, WebService(inst)),
        (serve_runtime.PLUGIN_SPEC, PluginService(inst)),
    )
    for spec, service in pairs:
        systemd_active = False
        probe_error = ""
        if service.available:
            try:
                systemd_active = service.is_active()
            except FrpsctlError as exc:
                probe_error = exc.message
        direct = serve_runtime.probe(inst, spec)
        if systemd_active:
            owner = "systemd"
        elif direct.running:
            owner = "direct"
        elif direct.owner is serve_runtime.ServeOwner.CORRUPTED:
            owner = "corrupted"
        elif direct.owner is serve_runtime.ServeOwner.FOREIGN:
            owner = "foreign"
        else:
            owner = "none"
        pid: int | None = None
        if systemd_active:
            with contextlib.suppress(FrpsctlError):
                pid = service.main_pid()
        elif direct.running and direct.state is not None:
            pid = direct.state.pid
        services[spec.key] = {
            "owner": owner,
            "active": systemd_active or direct.running,
            "state": "运行中" if (systemd_active or direct.running) else "未运行",
            "pid": pid,
            "uptime_seconds": direct.uptime_seconds if direct.running else None,
            "bind": (direct.state.args.get("bind") if direct.state else None),
            "log": (direct.state.log if direct.state else None),
            "systemd_unit": service.unit_name,
            "detail": direct.error or probe_error or "",
        }
    return {"instance": inst.name, "services": services}


def _plugin_direct_start(ctx: WebContext, fallback_args: dict | None = None) -> dict:
    """direct 模式启动插件（参数来源与 CLI `plugin start` 同一套规则）。"""
    from ..core.auditlog import resolve_policy_path
    from ..core.healthcheck import is_loopback, parse_bind

    inst = ctx.inst
    last, error = serve_runtime.read_state(inst, serve_runtime.PLUGIN_SPEC)
    if error is not None:
        raise ConfigError(error, hint="删除状态文件后重试（会重建）")
    old = dict(last.args) if last is not None else dict(fallback_args or {})
    # 复用旧参数里的策略路径（restart 场景：CLI 也有同一条"旧参数优先"规则），
    # 并用与 `plugin check/start` 同一套解析校验策略内容（v0.3.4 review 修复）
    policy_raw = old.get("policy")
    policy_file = Path(str(policy_raw)) if policy_raw else resolve_policy_path(inst)
    # 绝对化：旧 state 可能记录相对路径（历史版本），按实例目录解析不可靠，
    # 统一转绝对后再用于校验与 argv（与 CLI 启动时的写入规则一致）
    policy_file = policy_file.resolve()
    if not policy_file.exists():
        raise ConfigError(
            f"插件策略文件不存在：{policy_file}",
            hint="先在 CLI 运行 `frpsctl plugin init` 生成策略",
        )
    from ..plugin.policy import PluginPolicy

    try:
        PluginPolicy.load(policy_file)
    except FrpsctlError:
        raise
    except Exception as exc:  # noqa: BLE001 - 策略解析异常统一转契约错误
        raise ConfigError(f"策略文件无法解析：{policy_file}（{exc}）") from None
    bind = str(old.get("bind") or "127.0.0.1:8080")
    handler_path = str(old.get("path") or "/handler")
    access_log = bool(old.get("access_log", False))
    host, port = parse_bind(bind, default_port=8080)
    if not is_loopback(host):
        # 插件协议没有任何认证：非回环直接拒绝（与 CLI 同一判据）
        raise UsageError(
            f"插件拒绝绑定非回环地址：{bind}",
            hint="请在 CLI 用 `plugin start --bind 127.0.0.1:...` 修正启动参数",
        )
    argv = serve_runtime.build_serve_argv(
        serve_runtime.frpsctl_executable(),
        subcommand="plugin",
        bind=bind,
        policy=policy_file,
        handler_path=handler_path,
        access_log=access_log,
    )
    args = {"policy": str(policy_file), "bind": bind, "path": handler_path, "access_log": access_log}
    state = serve_runtime.start_background(
        inst, serve_runtime.PLUGIN_SPEC, argv=argv, args=args, host=host, port=port
    )
    return {"owner": "direct", "action": "start", "pid": state.pid, "bind": bind, "log": state.log}


def _plugin_service_action(ctx: WebContext, action: str) -> dict:
    """插件服务的 start/stop/restart（Web 与 CLI 同一套 core 语义，v0.3.4 F1）。

    按当前托管模式分派：systemd unit 存在 → systemctl 委托；否则 direct。
    两个方向都经 **core 守卫单点**（这是它从 CLI 下沉 core 的直接动机：
    Web 的启停此前会绕过互斥，见 `core/serve_guard.py`）。
    """
    from ..core.systemd import PluginService

    verb = action.split("-", 1)[1]
    if verb not in ("start", "stop", "restart"):
        raise UsageError(f"未知插件动作：{action!r}")
    inst = ctx.inst
    service = PluginService(inst)
    spec = serve_runtime.PLUGIN_SPEC
    if service.unit_exists():
        serve_guard.guard_against_direct(inst, spec)
        getattr(service, verb)()
        return {"owner": "systemd", "action": verb, "unit": service.unit_name}
    if verb == "start":
        serve_guard.guard_against_systemd(inst, spec)
        return _plugin_direct_start(ctx)
    if verb == "stop":
        state = serve_runtime.stop_background(inst, spec)
        return {"owner": "direct", "action": "stop", "stopped": True, "pid": state.pid}
    # restart：先读后停（与 CLI 的 review 修复同一条纪律）
    serve_guard.guard_against_systemd(inst, spec)
    last, error = serve_runtime.read_state(inst, spec)
    if error is not None:
        raise ConfigError(error, hint="删除状态文件后重试（会重建）")
    with contextlib.suppress(FrpsctlError):
        serve_runtime.stop_background(inst, spec)
    return _plugin_direct_start(ctx, fallback_args=dict(last.args) if last is not None else None)


def versions_payload(ctx: WebContext) -> dict:
    """版本信息（frpsctl / 运行中 / 磁盘；版本管理页与安装表单预填，v0.3.4 F11）。"""
    from .. import __version__
    from ..core.version import MINIMUM_VERSION, RECKONED_VERSION

    report = ctx.lifecycle().status()
    running = report.binary_version
    disk = report.disk_version
    return {
        "frpsctl": __version__,
        "binary": {
            "running": running,
            "disk": disk,
            "match": bool(running and disk and running == disk),
        },
        "minimum": ".".join(str(part) for part in MINIMUM_VERSION),
        "reckoned": ".".join(str(part) for part in RECKONED_VERSION),
        "hint": report.version_hint or "",
    }


def task_install(
    ctx: WebContext, body: dict[str, Any], *, request_info: RequestInfo | None = None
) -> dict:
    """提交 frps 安装任务（后台线程执行；单飞行；v0.3.4 F11）。"""
    version = str(body.get("version") or "")
    only_download = bool(body.get("only_download", False))
    try:
        task = ctx.tasks.submit_install(
            ctx.inst, version=version, only_download=only_download, mirrors=None
        )
    except Exception as exc:
        _audit(
            ctx,
            request_info,
            action="install",
            target=version,
            result=f"error:{type(exc).__name__}",
        )
        raise
    _audit(ctx, request_info, action="install", target=task.version)
    return {"task_id": task.id, "state": task.state, "version": task.version}


def task_payload(ctx: WebContext, task_id: str) -> dict:
    """查询任务状态（前端 1 秒轮询）。"""
    task = ctx.tasks.get(task_id)
    if task is None:
        raise ConfigError(
            f"任务不存在：{task_id}",
            hint="任务表有界（最近 8 条），过旧的任务会被淘汰",
        )
    return task.payload()


def diagnostics_text(ctx: WebContext) -> str:
    """诊断报告（F9）：状态 + 体检 + 打码配置 + 日志尾部。

    纯文本导出——配置值经 `config.mask_value` 打码；日志**不脱敏**（它由 frp
    写入，可能包含客户端信息），页面与 README 都提示自行检查。
    """
    import time as _time

    from .. import __version__

    inst = ctx.inst
    report = ctx.lifecycle().status()
    lines: list[str] = [
        "# frpsctl 诊断报告",
        f"# 生成时间：{_time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"# frpsctl：{__version__}",
        f"# 实例：{inst.name}",
        "",
        "## 状态（JSON）",
        json.dumps(
            report_mod.status_payload(report, include_paths=True),
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        "",
        "## 体检（与 CLI doctor 同一实现）",
    ]
    doctor_report = doc.run_doctor(inst)
    findings = doctor_report.sorted_findings()
    if not findings:
        lines.append("（无发现项）")
    for finding in findings:
        lines.append(f"[{finding.severity.value}] {finding.check}：{finding.message}")
        if finding.hint:
            lines.append(f"    ↳ {finding.hint}")
    lines += ["", "## 配置（敏感值已打码；完整原文请用 CLI `config get --reveal`）"]
    try:
        for key, value in cfg.flatten_tree(cfg.load_config(inst.config)):
            shown = cfg.mask_value(_plain(value)) if cfg.is_secret_key(key) else _plain(value)
            lines.append(f"{key} = {shown}")
    except FrpsctlError as exc:
        lines.append(f"（配置读取失败：{exc.message}）")
    lines += ["", "## 日志尾部（最多 200 行；请自行检查敏感内容）"]
    target = resolve_log_target(inst)
    lines.append(f"# 来源：{target}")
    lines.extend(line.rstrip("\n") for line in tail_lines(target, 200))
    return "\n".join(lines) + "\n"
