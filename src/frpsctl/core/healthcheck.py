"""从配置里解析健康检查所需的信息（设计文档 §3.7、§8.5）。

只读配置，不碰进程、不碰网络。三条语义值得注意：

1. `webServer.addr` 为空时 frp 的生效默认值是 `127.0.0.1`（`Complete()` 兜底），
   `webServer.port = 0` 表示**完全不启动 dashboard**——此时 L2 记 SKIPPED。
2. `httpPlugins[].addr` 是 URL 形式，探活要取 hostname/port，缺省端口按
   scheme 补（http→80、https→443）。
3. 拿不到 `webServer.password` 就无法访问 v2 Admin API；这不影响 L1/L2，
   但会让 L3 之外的统计采集降级——由调用方决定如何提示。
"""

from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..errors import UsageError

__all__ = [
    "DashboardInfo",
    "ListenInfo",
    "PluginTarget",
    "PORT_FIELDS",
    "is_loopback",
    "parse_bind",
    "parse_dashboard",
    "parse_listen",
    "parse_plugin_targets",
]

#: 需要做"端口可绑定性"探测的键（`doctor` §8.7 用）。
#: v0.3.1 从 `core/schema.py` 迁到此处：doctor 只用到这一个常量，而 import
#: schema 会拉起 pydantic（整个常见命令链上最重的一跳）；healthcheck 是本模块
#: 已有的轻量依赖。schema 保留 re-export 以免既有导入点失效。
PORT_FIELDS: tuple[str, ...] = (
    "bindPort",
    "kcpBindPort",
    "quicBindPort",
    "vhostHTTPPort",
    "vhostHTTPSPort",
    "webServer.port",
)


def parse_bind(bind: str, *, default_port: int) -> tuple[str, int]:
    """解析 `host:port` / `[v6]:port` / 裸 host 为 `(host, port)`。

    插件服务与 Web 管理台共用这一份解析（此前两处各写一份 host/port 属性，
    非法端口的报错行为也不一致）。端口非法时抛用法错误(2) 而不是让裸
    `ValueError` 冒到顶层。
    """
    text = bind.strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port_text = rest.lstrip(":")
        host = host or "127.0.0.1"
    elif ":" in text:
        host, _, port_text = text.rpartition(":")
    else:
        return text or "127.0.0.1", default_port
    if not port_text:
        return host, default_port
    try:
        port = int(port_text)
    except ValueError:
        raise UsageError(f"无法解析监听地址端口：{bind!r}") from None
    if not (0 <= port <= 65535):
        raise UsageError(f"监听端口越界（0..65535）：{bind!r}")
    return host, port


def is_loopback(addr: str) -> bool:
    """地址是否指向回环。兼容裸 host、`host:port`、`[v6]:port` 三种写法。

    **全项目共用一份判据**：schema 的"危险组合"拦截、doctor 的暴露面检查、
    插件的绑回环硬约束都依赖它。各写一份迟早出现"一处放行、一处拒绝"的
    安全缝隙（`127.0.0.2` 这类回环网段地址就是典型的分歧点）。
    """
    host = addr.strip()
    if host.startswith("["):  # [::1]:7500 形式
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:  # host:port 形式（IPv6 冒号多于一个，不切）
        host = host.rsplit(":", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

#: frp 的 `WebServerConfig.Complete()` 兜底地址。
DEFAULT_DASHBOARD_ADDR = "127.0.0.1"

#: frp 的生效默认监听（附录 A）：bindAddr = 0.0.0.0、bindPort = 7000。
DEFAULT_BIND_ADDR = "0.0.0.0"
DEFAULT_BIND_PORT = 7000


@dataclass(frozen=True)
class ListenInfo:
    """控制连接监听信息（`bindAddr:bindPort`）。"""

    addr: str
    port: int

    @property
    def display(self) -> str:
        host = f"[{self.addr}]" if ":" in self.addr and not self.addr.startswith("[") else self.addr
        return f"{host}:{self.port}"


@dataclass(frozen=True)
class DashboardInfo:
    """dashboard 连接信息。`port == 0` 表示未启用。"""

    addr: str
    port: int
    user: str
    password: str

    @property
    def enabled(self) -> bool:
        return self.port > 0

    @property
    def base_url(self) -> str:
        """v2 Admin API 的基址。IPv6 字面量需要加方括号。"""
        host = self.addr or DEFAULT_DASHBOARD_ADDR
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def auth_enabled(self) -> bool:
        """frp 是否对 dashboard 施加 Basic Auth。

        判据是"**任一非空即启用**"：user 与 password 同时为空时 frp 完全不鉴权。
        实测（真 frps 0.71.0）：

        | user | password | 无凭据请求 |
        |------|----------|-----------|
        | `"admin"` | 未设/空 | **401** |
        | 未设 | 未设 | **200** |
        | `"admin"` | `"secret"` | 401 |

        注意"启用了鉴权"不等于"有像样的口令"：上表第一行里 `admin:` + **空口令**
        就能拿到 200。空口令的强度问题由 `doctor` 单独告警（§8.7）。
        """
        return bool(self.user or self.password)


@dataclass(frozen=True)
class PluginTarget:
    """一个 httpPlugins 目标的探活地址。"""

    name: str
    host: str
    port: int
    raw: str


def parse_dashboard(config_path: Path) -> DashboardInfo:
    """读取 dashboard 配置；文件缺失或损坏时返回"未启用"。"""
    data = _load(config_path)
    section = data.get("webServer") or {}
    if not isinstance(section, dict):
        section = {}
    return DashboardInfo(
        addr=str(section.get("addr") or DEFAULT_DASHBOARD_ADDR),
        port=_as_int(section.get("port"), 0),
        user=str(section.get("user") or ""),
        password=str(section.get("password") or ""),
    )


def parse_listen(config_path: Path) -> ListenInfo | None:
    """解析控制端口监听（`bindAddr` / `bindPort`，附录 A 的生效默认值）。

    返回 None 仅表示"配置不可读/不可解析"（文件缺失、语法错误）；**合法的
    空配置**仍返回默认值 `0.0.0.0:7000`——那是 frp 的生效值，不是"无监听"。

    `bindPort <= 0` 同样回落到默认 7000：**实测确认**（真 frps 0.71.0，
    `bindPort = 0` 启动日志为 `frps tcp listen on 127.0.0.1:7000`）——
    与 `bindAddr` 的空值兜底是同一套 `Complete()` 逻辑，此前这里把它当作
    "无意义/无监听"，会漏报一个真实在监听的端口。
    """
    try:
        raw = config_path.read_bytes()
    except OSError:
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    addr = str(data.get("bindAddr") or DEFAULT_BIND_ADDR)
    port = _as_int(data.get("bindPort"), DEFAULT_BIND_PORT)
    if port <= 0:
        port = DEFAULT_BIND_PORT
    return ListenInfo(addr=addr, port=port)


def parse_plugin_targets(config_path: Path) -> list[PluginTarget]:
    """解析所有 httpPlugins 的探活地址（§3.7 的 L3）。

    URL 解析失败的条目**直接跳过**——L3 是告警层，不该因为配置里一个畸形
    地址就让 `status` 整体失败；`doctor` 会单独把这种条目报出来。
    """
    data = _load(config_path)
    plugins = data.get("httpPlugins") or []
    if not isinstance(plugins, list):
        return []

    targets: list[PluginTarget] = []
    for index, item in enumerate(plugins):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("addr") or "").strip()
        if not raw:
            continue
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        host = parsed.hostname
        if not host:
            continue
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        targets.append(
            PluginTarget(
                name=str(item.get("name") or f"plugin[{index}]"),
                host=host,
                port=port,
                raw=raw,
            )
        )
    return targets


def _load(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default
