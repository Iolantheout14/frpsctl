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

import json
import math
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import unquote

from .. import report as report_mod
from ..core import auditlog
from ..core import config as cfg
from ..core import doctor as doc
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
from ..core.logs import resolve_log_target, tail_lines
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
        if path == "/api/proxies":
            return 200, proxies_payload(ctx)
        if path == "/api/traffic":
            return 200, traffic_payload(ctx)
        if path.startswith("/api/traffic/"):
            return 200, traffic_one_payload(ctx, unquote(path[len("/api/traffic/") :]))
        if path == "/api/doctor":
            return 200, doctor_payload(ctx)
        if path == "/api/audit":
            return 200, audit_payload(ctx, query.get("scope", "plugin"))
        if path == "/api/config":
            return 200, config_payload(ctx)
        if path == "/api/config/history":
            return 200, history_payload(ctx)
        if path.startswith("/api/config/history/") and path.endswith("/diff"):
            steps = path[len("/api/config/history/") : -len("/diff")]
            return 200, history_diff_payload(ctx, steps)
        if path == "/api/logs":
            return 200, logs_payload(ctx, query.get("lines"))
        return 404, {"error": f"未知接口：GET {path}"}

    if method == "POST":
        if path.startswith("/api/actions/"):
            return 200, action_payload(ctx, path.rsplit("/", 1)[-1], body, request_info=request_info)
        if path == "/api/config/preview":
            return 200, config_preview(ctx, body)
        if path == "/api/config/apply":
            return 200, config_apply(ctx, body, request_info=request_info)
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
    payload = {"clients": page.items, "total": page.total, "truncated": page.truncated}
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


def _web_audit_payload(ctx: WebContext) -> dict:
    """Web 操作审计视图（0.3.0）：路径 + 统计 + 尾部记录。"""
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
    summary = web_audit.summarize(path)
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


def audit_payload(ctx: WebContext, scope: str = "plugin") -> dict:
    """审计视图：`scope=plugin`（默认，兼容）或 `scope=web`（操作审计）。

    策略文件缺失/不合法时也返回 200（`available=false` + reason）——审计视图
    的职责是"展示现状"，不是替 `plugin check` 做严格校验；一条 400 只会让页面
    失去"为什么看不到审计"的解释。非法 scope 同样是 400（不猜测调用意图）。
    """
    if scope == "web":
        return _web_audit_payload(ctx)
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
        summary = auditlog.summarize(view.path)
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


def logs_payload(ctx: WebContext, lines_raw: str | None) -> dict:
    try:
        lines = int(lines_raw) if lines_raw else 200
    except ValueError:
        lines = 200
    lines = max(1, min(lines, MAX_LOG_LINES))
    cache_key = f"logs:{lines}"
    cached = ctx.cache.get(cache_key, CACHE_TTL_LOGS)
    if cached is not None:
        return cached
    target = resolve_log_target(ctx.inst)
    payload = {"path": str(target), "lines": tail_lines(target, lines)}
    ctx.cache.put(cache_key, payload)
    return payload


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
