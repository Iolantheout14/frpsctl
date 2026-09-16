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

import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

__all__ = ["DashboardInfo", "PluginTarget", "parse_dashboard", "parse_plugin_targets"]

#: frp 的 `WebServerConfig.Complete()` 兜底地址。
DEFAULT_DASHBOARD_ADDR = "127.0.0.1"


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
