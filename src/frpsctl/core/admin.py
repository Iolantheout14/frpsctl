"""v2 Admin API 客户端（设计文档 §8.5、ADR-3）。

**只走 v2**：版本门槛（§3.6）保证目标二进制 ≥ 0.70.0，而 v2 API 正是 0.70.0
引入的，因此不存在"端点可能缺失"的正常情形——也就没有降级路径。

拿到 404 时的行为是**报错并附实测版本**，而不是退回 v1：那说明二进制与预期
不符（例如用户用 `--binary` 指了一个自编译的怪版本），此时猜一个降级路径
只会让状态显示悄悄出错（ADR-7 的"不猜测"）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..errors import AdminUnreachable, ApiVersionMismatch

__all__ = ["AdminClient", "ProxyStat", "ServerInfo", "V2Proxy", "sum_proxy_types"]


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

    def clients(self) -> list[dict]:
        """客户端列表。v2 的信封里是 `data.items`（真机实测），不是 `data.clients`。

        ⚠️ **这是分页结果**：默认 `pageSize` 只有 50，超过就只返回前 50 条。
        需要"有多少"时请用 `page_total()`——把分页片段当成全量是静默错误。
        """
        return _page_items(self._unwrap(self._get("/api/v2/clients")))

    def proxies(self, ptype: str) -> list[ProxyStat]:
        """某个类型的全部代理。`proxyTypeCount` 的交叉验证靠它（§13.1 C1）。"""
        resp = self._get(f"/api/proxy/{ptype}")
        if resp.status_code == 404:
            return []
        body = resp.json()
        items = body.get("proxies") if isinstance(body, dict) else body
        return [ProxyStat.from_api(item) for item in (items or [])]

    def page_total(self, endpoint: str) -> int:
        """取某个分页端点的**总数**（`data.total`），而不是当前页条数。

        frp 的 v2 列表端点统一返回 `{total, page, pageSize, items}`，`total` 是
        过滤后的总数。要"按用户统计代理数"就必须用它——逐页拉取既慢，又容易
        在 pageSize 上悄悄截断。
        """
        payload = self._unwrap(self._get(endpoint))
        if isinstance(payload, dict) and isinstance(payload.get("total"), int):
            return int(payload["total"])
        return len(_page_items(payload))

    # --- 全量列表（自动翻页） ------------------------------------------

    def list_clients(self, *, page_size: int = 200) -> list[dict]:
        """**全量**客户端列表（自动翻页）。

        与 `clients()` 的区别：后者只取一页（默认 50 条，真机实测），用于
        "看一眼"；本方法是 `frpsctl clients` 的实现，会把所有页拉全——
        把分页片段当成全量是静默错误（§11.2.4 的教训）。
        """
        return self._paged("/api/v2/clients", page_size=page_size)

    def list_proxies(self, *, user: str = "", page_size: int = 200) -> list[V2Proxy]:
        """**全量**代理列表（v2 形状，自动翻页）。"""
        params = {"user": user} if user else None
        raw = self._paged("/api/v2/proxies", params=params, page_size=page_size)
        return [V2Proxy.from_api(item) for item in raw]

    def proxy_traffic(self, name: str) -> list[dict]:
        """某代理的流量历史（**日粒度**，实测固定返回近 7 天）。

        真机 `GET /api/v2/proxies/{name}/traffic` 的形状：

        ```json
        {"name": "alice.alice-ssh", "unit": "bytes", "granularity": "day",
         "history": [{"date": "2026-09-11", "trafficIn": 0, "trafficOut": 0}, ...]}
        ```

        `granularity` 查询参数**无效**（实测传 hour 仍返回 day）——趋势图按天画。
        名称里的 `/` 等字符必须转义后才能拼进路径。
        """
        from urllib.parse import quote

        path = f"/api/v2/proxies/{quote(name, safe='')}/traffic"
        payload = self._unwrap(self._get(path))
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
    ) -> list[dict]:
        """逐页拉取直到 total 满足；带页数上限防止服务端异常时死循环。"""
        items: list[dict] = []
        for page in range(1, max_pages + 1):
            query = dict(params or {})
            query.update({"page": str(page), "page_size": str(page_size)})
            payload = self._unwrap(self._get(path, params=query))
            batch = _page_items(payload)
            items.extend(batch)
            total = payload.get("total") if isinstance(payload, dict) else None
            if not batch or not isinstance(total, int) or len(items) >= total:
                break
        return items

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
    的裸数组）。**只取 items 而不校验 total**，因为调用方若关心总数应当用
    `page_total()`——这里保持"列表就是列表"的单一语义。
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
