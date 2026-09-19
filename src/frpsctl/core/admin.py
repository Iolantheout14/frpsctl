"""v2 Admin API 客户端（设计文档 §8.5、ADR-3）。

**只走 v2**：版本门槛（§3.6）保证目标二进制 ≥ 0.70.0，而 v2 API 正是 0.70.0
引入的，因此不存在"端点可能缺失"的正常情形——也就没有降级路径。

拿到 404 时的行为是**报错并附实测版本**，而不是退回 v1：那说明二进制与预期
不符（例如用户用 `--binary` 指了一个自编译的怪版本），此时猜一个降级路径
只会让状态显示悄悄出错（ADR-7 的"不猜测"）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from ..errors import AdminUnreachable, ApiVersionMismatch, FrpsctlError

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    import httpx

#: ⚠️ `httpx` 只在真正构造/调用客户端时导入（v0.3.1）：它是本模块唯一的
#: 重依赖，而本模块被 `cli.runtime` / `report` 等常见链顶层引用——放在顶层会
#: 让 `frpsctl status`（实例未开 dashboard 时根本不需要 HTTP）也付出它的
#: 导入成本（实测冷启动 0.75s → 其中 httpx 链路占大头）。运行时用到的地方
#: （`__init__` / `healthz` / `_get`）各自函数内导入；注解由
#: `from __future__ import annotations` 字符串化，不受影响。

__all__ = [
    "AdminClient",
    "PROXY_TYPES",
    "PageResult",
    "PruneOutcome",
    "ProxyStat",
    "ServerInfo",
    "V2Proxy",
    "aggregate_days",
    "fetch_histories",
    "sum_proxy_types",
    "traffic_total",
    "TRAFFIC_MAX_PROXIES",
]

#: 趋势/汇总类查询最多覆盖多少个代理（每个代理一次 dashboard 请求）。
#: 放在 core 是因为 CLI `traffic` 与 Web 趋势接口共用同一约束——两处各写一个
#: 数字迟早漂移，而"查了 50 个还是 100 个"直接决定响应时间。
TRAFFIC_MAX_PROXIES = 50

#: 趋势查询的并发度：dashboard 是本机 HTTP 服务，串行查 50 个代理要 50 个往返；
#: 并发只要不把 dashboard 打爆即可（它同样是 ThreadingHTTPServer）。
TRAFFIC_WORKERS = 8

#: 一次趋势查询的**整体预算**（秒，v0.3.1）。50 个代理 × 单请求 3s 超时、8 并发
#: 下最坏接近 19 秒——单个 `/api/traffic` 请求会占住 Web worker 这么久（几个
#: 这样的请求就能把有界并发排满），CLI 也要干等。预算内返回**已完成的部分**
#: 并如实标记（`partial=True`），未完成的代理记空曲线（与离线代理同一降级路径）。
TRAFFIC_DEADLINE = 6.0

#: frp v0.71.0 的全部代理类型（`frps` 文档与 v2 Admin API 一致）。
#: CLI `proxies --type` 用它做输入校验：拼错类型过去会静默返回空列表，
#: 用户以为"没有代理"——ADR-7 不猜测，非法输入必须变成明确报错。
PROXY_TYPES: frozenset[str] = frozenset(
    {"tcp", "udp", "http", "https", "stcp", "xtcp", "tcpmux", "sudp"}
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class PageResult(Generic[_T]):
    """一次**全量翻页**的结果：条目 + 服务端声明的总数。

    `total` 来自 v2 分页信封（第一页就带），因此"共 N 条"不需要额外请求。
    `truncated` 为真说明翻页被 `max_pages` 上限截断（服务端 total 异常大时的
    防御）——调用方应当如实展示"还有 N 条未加载"，而不是假装拿到了全量
    （旧实现对上限截断完全静默）。
    """

    items: list[_T]
    total: int

    @property
    def truncated(self) -> bool:
        return self.total > len(self.items)


@dataclass(frozen=True)
class PruneOutcome:
    """一次"清理离线代理记录"的结果。

    frp 的 `DELETE /api/proxies` 只回 code/msg、不回条数，因此清理数由
    "清理前后的离线记录数之差"推导——`exact=False` 表示列表被翻页上限截断，
    这个差值只是下界。
    """

    before: int
    cleared: int
    exact: bool = True


def _is_loopback_url(base_url: str) -> bool:
    """目标是本机吗？（决定是否让 httpx 去读环境代理变量）

    **为什么本机必须绕开 env 代理**：dashboard 默认绑 `127.0.0.1`（§3.3），
    对它的请求无论如何都不该走代理。而 `HTTP_PROXY` / `NO_PROXY` 这类环境变量
    在实践中一旦写法不规范（例如 `NO_PROXY` 里写了带方括号的 IPv6 `[::1]`），
    httpx 在构造客户端时就会直接抛 `InvalidURL` —— 那会让**每一次健康检查**
    都变成"dashboard 不可达"，而真正的原因与 dashboard 毫无关系。

    非本机地址仍尊重环境代理：那种部署下代理可能就是到达 dashboard 的唯一路径。
    """
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""} or host.startswith("127.")


@dataclass(frozen=True)
class ServerInfo:
    """`/api/v2/system/info` 的关键字段（字段名与形状均在真机 v0.71.0 上核对过）。

    ⚠️ 两个极易踩错的地方：

    1. `proxy_type_counts` 的 JSON 键是 **`proxyTypeCount`**（不是 `proxyCount`），
       且它是**按代理类型的计数字典**（如 `{"tcp":7,"http":3}`），**不是总数**。
       要展示总数必须 `sum_proxy_types()`——直接当整数用会打印出一个 dict。
    2. `client_counts` 的 JSON 键是 `clientCounts`，它是**整数**（在线客户端数），
       不是字典。按字典处理会直接 `TypeError`。

    字段名写错的代价特别隐蔽：`dict[str, int]` 的默认值让解析"看起来成功了"，
    只是数字永远是 0。§13.1 C1 就是为这种情况设的门禁。
    """

    version: str
    client_counts: int = 0
    proxy_type_counts: dict[str, int] = field(default_factory=dict)
    cur_conns: int = 0
    total_traffic_in: int = 0
    total_traffic_out: int = 0
    tls_force: bool = False

    @property
    def proxy_total(self) -> int:
        return sum_proxy_types(self.proxy_type_counts)


@dataclass(frozen=True)
class ProxyStat:
    """代理条目。字段随 frp 版本可能增减，故用宽松解析。"""

    name: str = ""
    type: str = ""
    conf: dict = field(default_factory=dict)
    status: str = ""
    today_traffic_in: int = 0
    today_traffic_out: int = 0
    cur_conns: int = 0
    last_start_time: str = ""

    @classmethod
    def from_api(cls, item: dict) -> ProxyStat:
        return cls(
            name=str(item.get("name", "")),
            type=str(item.get("type", "")),
            conf=item.get("conf") or {},
            status=str(item.get("status", "")),
            today_traffic_in=int(item.get("todayTrafficIn") or 0),
            today_traffic_out=int(item.get("todayTrafficOut") or 0),
            cur_conns=int(item.get("curConns") or 0),
            last_start_time=str(item.get("lastStartTime") or ""),
        )


@dataclass(frozen=True)
class V2Proxy:
    """`/api/v2/proxies` 的代理条目（**嵌套** `spec`/`status`，与 v1 形状不同）。

    字段名在真机 v0.71.0 上实测（起真 frpc 建代理后抓取）：

    ```json
    {"name": "alice.test-tcp", "user": "alice", "clientID": "...",
     "spec": {"type": "tcp", "tcp": {"remotePort": 26000, ...}},
     "status": {"phase": "online", "todayTrafficIn": 0, "curConns": 0, ...}}
    ```

    端口在 `spec.<type>.remotePort` 下（http/https 走域名，没有该字段）；
    状态在 `status.phase`（`online` / `offline`）——不是 v1 的顶层 `status` 字符串。
    """

    name: str = ""
    user: str = ""
    client_id: str = ""
    type: str = ""
    remote_port: int = 0
    phase: str = ""
    today_traffic_in: int = 0
    today_traffic_out: int = 0
    cur_conns: int = 0
    last_start_at: int = 0

    @classmethod
    def from_api(cls, item: dict) -> V2Proxy:
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        ptype = str(spec.get("type") or "")
        section = spec.get(ptype)
        if not isinstance(section, dict):
            section = {}
        return cls(
            name=str(item.get("name") or ""),
            user=str(item.get("user") or ""),
            client_id=str(item.get("clientID") or ""),
            type=ptype,
            remote_port=_as_int(section.get("remotePort")),
            phase=str(status.get("phase") or ""),
            today_traffic_in=_as_int(status.get("todayTrafficIn")),
            today_traffic_out=_as_int(status.get("todayTrafficOut")),
            cur_conns=_as_int(status.get("curConns")),
            last_start_at=_as_int(status.get("lastStartAt")),
        )


def sum_proxy_types(counts: dict[str, int]) -> int:
    """把类型计数字典加成总数。

    独立成函数是为了让 §13.1 C1 的交叉验证与生产代码走**同一个**口径：
    测试断言 `sum(counts) == 各类型列表长度之和`，就不会出现"测试用一套口径、
    生产用另一套"的假绿灯。
    """
    return sum(int(v) for v in counts.values())


def traffic_total(points: list[dict]) -> dict[str, int]:
    """逐日流量点的合计（in/out）。CLI `traffic` 与 Web 趋势图共用。"""
    return {
        "in": sum(int(point.get("in") or 0) for point in points),
        "out": sum(int(point.get("out") or 0) for point in points),
    }


def aggregate_days(series: list[list[dict]]) -> list[dict]:
    """把多个代理的日粒度历史按日期求和（按日期排序）。

    这是 Web 趋势图与 CLI `traffic` 的**同一份**聚合实现：此前它只存在于
    CLI，Web 是在前端 JS 里重新聚合的——两处口径一旦漂移（日期解析、缺字段
    处理），同一份数据在两个界面上的合计会不一样。
    """
    days: dict[str, dict[str, int]] = {}
    for history in series:
        for point in history:
            date = str(point.get("date") or "")
            if not date:
                continue
            bucket = days.setdefault(date, {"date": date, "in": 0, "out": 0})
            bucket["in"] += int(point.get("in") or 0)
            bucket["out"] += int(point.get("out") or 0)
    return [days[key] for key in sorted(days)]


@dataclass(frozen=True)
class HistoryFetch:
    """一次多代理流量查询的结果。

    `series` 与 `names` 等长且同序；`partial=True` 表示有代理在整体预算内
    没有返回（`series` 里对应位置为空曲线）——调用方必须**如实展示**，
    不能把部分数据当全量（"降级必须可见"）。
    """

    series: list[list[dict]]
    partial: bool = False


def fetch_histories(
    client: AdminClient,
    names: list[str],
    *,
    workers: int = TRAFFIC_WORKERS,
    deadline: float = TRAFFIC_DEADLINE,
) -> HistoryFetch:
    """并发取多个代理的流量历史；单个代理失败记空曲线（不拖垮整体）。

    `httpx.Client` 是线程安全的（官方保证），因此可以直接共享一个 AdminClient。
    串行查 50 个代理要 50 个 HTTP 往返——dashboard 稍慢就能把一次趋势刷新拖到
    秒级；并发后总耗时约等于最慢的那一个。

    **整体预算**（v0.3.1）：`deadline` 秒内完成多少算多少，未完成的记空曲线
    并以 `partial=True` 上报。此前用 `pool.map` 无预算：慢 dashboard 下最坏
    ~19 秒才返回，长占调用方（Web worker / 终端）。

    ⚠️ `shutdown(wait=False)` 只保证**函数**按预算返回；线程池线程是非 daemon
    的，解释器退出时会 atexit join 未完成任务——CLI `traffic` 命令返回后，
    进程退出最多再等**单个请求**的超时（`AdminClient.timeout`，默认 3s；
    实测预算 0.3s 返回、进程因一个 5s 慢任务拖到 5.1s）。Web 服务进程常驻、
    无此影响；CLI 侧这个尾巴小于修复前的最坏 19s，且没有干净的取消手段
    （Python 无法强杀运行中的线程），如实记录为已知行为。
    """
    from concurrent.futures import ThreadPoolExecutor, wait

    results: list[list[dict]] = [[] for _ in names]
    if not names:
        return HistoryFetch(series=results)

    def fetch(name: str) -> list[dict]:
        try:
            return client.proxy_traffic(name)
        except FrpsctlError:
            # 离线/已删除的代理返回"无数据"是常态，不拖垮整体（真机语义 404）。
            return []

    if len(names) == 1:
        return HistoryFetch(series=[fetch(names[0])])

    pool = ThreadPoolExecutor(max_workers=min(workers, len(names)))
    try:
        futures = {pool.submit(fetch, name): index for index, name in enumerate(names)}
        done, pending = wait(futures, timeout=max(0.0, deadline))
        for future in done:
            results[futures[future]] = future.result()
        for future in pending:
            future.cancel()
        partial = bool(pending)
    finally:
        # wait=False：不能等未完成的任务（那会让预算失效）；
        # cancel_futures 清掉尚在队列中的提交。
        pool.shutdown(wait=False, cancel_futures=True)
    return HistoryFetch(series=results, partial=partial)


class AdminClient:
    """v2-only dashboard 客户端。"""

    def __init__(
        self,
        base_url: str,
        user: str = "",
        password: str = "",
        timeout: float = 3.0,
        *,
        trust_env: bool | None = None,
    ) -> None:
        import httpx  # 惰性：见模块顶部说明

        auth = (user, password) if (user or password) else None
        self._base_url = base_url.rstrip("/")
        if trust_env is None:
            trust_env = not _is_loopback_url(self._base_url)
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            auth=auth,
            trust_env=trust_env,
        )

    # --- 生命周期 ------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AdminClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 探针 ----------------------------------------------------------

    def healthz(self) -> tuple[bool, float]:
        """L2 控制面探针：`/healthz` **免认证**（§3.2）。

        返回 `(是否健康, 耗时毫秒)`。任何网络异常都算不健康，不向上抛——
        调用方关心的是"活着吗"，而不是"为什么没活着"。
        """
        import time

        import httpx  # 惰性：见模块顶部说明

        started = time.monotonic()
        try:
            ok = self._client.get("/healthz", timeout=1.5).status_code == 200
        except httpx.HTTPError:
            ok = False
        return ok, (time.monotonic() - started) * 1000

    # --- 数据 ----------------------------------------------------------

    def server_info(self) -> ServerInfo:
        """唯一的统计入口。404 → `ApiVersionMismatch`，**不降级**。"""
        resp = self._get("/api/v2/system/info")
        if resp.status_code == 404:
            raise ApiVersionMismatch(
                "该 frps 不提供 /api/v2/system/info",
                hint=(
                    f"实测 dashboard 地址 {self._base_url} 返回 404，"
                    "而最低支持版本 0.70.0 应当提供该端点；"
                    "请确认二进制版本（`frps -v`）与 --binary 指向是否一致"
                ),
            )
        payload = self._unwrap(resp)
        status = payload.get("status") or {}
        config = payload.get("config") or {}
        return ServerInfo(
            version=str(payload.get("version", "")),
            client_counts=_as_int(status.get("clientCounts")),
            proxy_type_counts=_as_int_map(status.get("proxyTypeCount")),
            cur_conns=_as_int(status.get("curConns")),
            total_traffic_in=_as_int(status.get("totalTrafficIn")),
            total_traffic_out=_as_int(status.get("totalTrafficOut")),
            tls_force=bool(config.get("tlsForce", False)),
        )

    def proxies(self, ptype: str) -> list[ProxyStat]:
        """某个类型的全部代理。`proxyTypeCount` 的交叉验证靠它（§13.1 C1）。"""
        resp = self._get(f"/api/proxy/{ptype}")
        if resp.status_code == 404:
            return []
        body = resp.json()
        items = body.get("proxies") if isinstance(body, dict) else body
        return [ProxyStat.from_api(item) for item in (items or [])]

    # --- 全量列表（自动翻页） ------------------------------------------

    def list_clients(self, *, page_size: int = 200) -> list[dict]:
        """**全量**客户端列表（自动翻页）。

        需要总数（"共 N 条"、截断检测）时用 `page_clients()`——它保留信封里的
        `total` 并报告翻页上限截断；本方法只为"只要列表"的调用点保留。
        """
        return self.page_clients(page_size=page_size).items

    def page_clients(self, *, page_size: int = 200) -> PageResult[dict]:
        """全量客户端列表 + 总数（见 `_paged`）。"""
        return self._paged("/api/v2/clients", page_size=page_size)

    def list_proxies(self, *, user: str = "", page_size: int = 200) -> list[V2Proxy]:
        """**全量**代理列表（v2 形状，自动翻页）。"""
        return self.page_proxies(user=user, page_size=page_size).items

    def page_proxies(self, *, user: str = "", page_size: int = 200) -> PageResult[V2Proxy]:
        """全量代理列表 + 总数（见 `_paged`）。"""
        params = {"user": user} if user else None
        raw = self._paged("/api/v2/proxies", params=params, page_size=page_size)
        return PageResult(
            items=[V2Proxy.from_api(item) for item in raw.items],
            total=raw.total,
        )

    def prune_offline_proxies(self) -> PruneOutcome:
        """清理离线代理记录并**如实汇报清理条数**。

        frp 的 DELETE 不返回条数，因此做"清理前后各数一次"——代价是两次全量
        翻页，而 prune 是低频人工操作，准确性值得这个代价：CLI 与 Web 都会
        把条数展示给用户，报错一个假的 0 比不报更糟。
        """
        before_page = self.page_proxies()
        before = sum(1 for item in before_page.items if item.phase == "offline")
        self.clear_offline_proxies()
        after_page = self.page_proxies()
        after = sum(1 for item in after_page.items if item.phase == "offline")
        return PruneOutcome(
            before=before,
            cleared=max(0, before - after),
            exact=not (before_page.truncated or after_page.truncated),
        )

    def proxy_traffic(self, name: str) -> list[dict]:
        """某代理的流量历史（**日粒度**，实测固定返回近 7 天）。

        真机 `GET /api/v2/proxies/{name}/traffic` 的形状：

        ```json
        {"name": "alice.alice-ssh", "unit": "bytes", "granularity": "day",
         "history": [{"date": "2026-09-11", "trafficIn": 0, "trafficOut": 0}, ...]}
        ```

        `granularity` 查询参数**无效**（实测传 hour 仍返回 day）——趋势图按天画。
        名称里的 `/` 等字符必须转义后才能拼进路径。

        **404 = 无数据**（真机语义，契约 C10）：离线/不存在的代理返回
        `no proxy info found`。这是常态（已下线的客户端、待清理的离线记录），
        因此返回空列表而不是把它升级成错误——CLI `traffic` 与 Web 趋势图
        都依赖"一个离线代理不拖垮整体"。
        """
        from urllib.parse import quote

        path = f"/api/v2/proxies/{quote(name, safe='')}/traffic"
        resp = self._get(path)
        if resp.status_code == 404:
            return []
        payload = self._unwrap(resp)
        history = payload.get("history") if isinstance(payload, dict) else None
        if not isinstance(history, list):
            return []
        return [
            {
                "date": str(item.get("date") or ""),
                "in": _as_int(item.get("trafficIn")),
                "out": _as_int(item.get("trafficOut")),
            }
            for item in history
            if isinstance(item, dict)
        ]

    def _paged(
        self,
        path: str,
        *,
        params: dict | None = None,
        page_size: int = 200,
        max_pages: int = 100,
    ) -> PageResult[dict]:
        """逐页拉取直到 total 满足；带页数上限防止服务端异常时死循环。

        返回的 `total` 取自信封（第一页即可得）；若循环耗尽 `max_pages` 仍未
        拉全，`PageResult.truncated` 会如实为真——旧实现只返回 items，调用方
        无从知道"还有多少没拿到"（静默截断）。
        """
        items: list[dict] = []
        total = 0
        for page in range(1, max_pages + 1):
            query = dict(params or {})
            query.update({"page": str(page), "page_size": str(page_size)})
            payload = self._unwrap(self._get(path, params=query))
            batch = _page_items(payload)
            items.extend(batch)
            raw_total = payload.get("total") if isinstance(payload, dict) else None
            total = int(raw_total) if isinstance(raw_total, int) else len(items)
            if not batch or len(items) >= total:
                break
        return PageResult(items=items, total=total)

    def proxy_count_for_user(self, user: str) -> int:
        """某用户当前的代理数（插件 `max_proxies` 配额用）。

        用 `total` 而不是 items 长度：后者受 pageSize 限制（默认 50），
        会在大户身上把配额判断变成"永远没超"。
        """
        payload = self._unwrap(self._get("/api/v2/proxies", params={"user": user, "page_size": "1"}))
        if isinstance(payload, dict) and isinstance(payload.get("total"), int):
            return int(payload["total"])
        return len(_page_items(payload))

    def clear_offline_proxies(self) -> None:
        """清理 dashboard 统计里的**离线代理记录**（frp 唯一的代理写操作）。

        ⚠️ 这不是"下线在线代理"：frp v0.71.0 的路由表里**没有任何**强制断开
        在线代理的 API——`DELETE /api/proxies` 的实现是 `ClearOfflineProxies()`，
        且只接受 `?status=offline`（源码 `server/http/controller.go`，真机实测
        无参数时返回 `400 status only support offline`）。此前把它当作
        "kick（下线代理）"是一个**从未真正工作过**的功能。
        """
        resp = self._client.delete("/api/proxies", params={"status": "offline"})
        if resp.status_code >= 400:
            raise AdminUnreachable(
                f"清理离线代理记录失败：HTTP {resp.status_code}",
                hint=resp.text.strip()[:200],
            )

    # --- 内部 ----------------------------------------------------------

    def _get(self, path: str, *, params: dict | None = None) -> httpx.Response:
        import httpx  # 惰性：见模块顶部说明

        try:
            return self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise AdminUnreachable(
                f"dashboard 不可达（{self._base_url}{path}）：{exc}",
                hint=("确认 webServer.port > 0 且实例在运行；若 dashboard 绑在非回环地址，检查网络与防火墙"),
            ) from None

    def _unwrap(self, resp: httpx.Response) -> dict:
        """拆 v2 信封 `{code,msg,data}`。

        401 单独给出"鉴权失败"的提示——口令为空与口令错误的表现都是 401，
        但处置方式完全不同（前者要设口令，后者要改口令）。
        """
        if resp.status_code == 401:
            raise AdminUnreachable(
                "dashboard 鉴权失败（HTTP 401）",
                hint="检查 webServer.user / webServer.password，或用 --admin-password 覆盖",
            )
        if resp.status_code >= 400:
            raise AdminUnreachable(f"dashboard 返回 HTTP {resp.status_code}")
        body = resp.json()
        if isinstance(body, dict) and "data" in body:
            return body["data"] or {}
        return body if isinstance(body, dict) else {}


def _page_items(payload: object) -> list[dict]:
    """从 v2 分页信封里取 items。

    兼容两种形状：`{total,page,pageSize,items}`（真机）与直接是列表（v1 风格
    的裸数组）。`total` 由 `_paged` 在信封层面读取——这里保持"列表就是列表"
    的单一语义。
    """
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("clients") or []
        return [item for item in items if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _as_int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(k): _as_int(v) for k, v in value.items()}
