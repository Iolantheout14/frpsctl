"""三层健康判定（设计文档 §3.7）。

**为什么不能只探一个 `/healthz`**：结合 §11.2 的插件事实——插件 fail-closed，
且 frp 侧对插件 HTTP 客户端**没有超时**——存在两种"进程活着但业务已死"：

- frps 在跑但 dashboard 没起（`webServer.port = 0`）→ 可观测性为零；
- frps 在跑但插件服务挂了 → **所有客户端都无法登录**，而 `/healthz` 依然 200。

| 层 | 探针 | 回答的问题 |
|----|------|-----------|
| L1 进程 | `pid_alive` + `/proc/<pid>/stat` 身份校验 | 进程还在，且确实是我们的 |
| L2 控制面 | `GET /healthz`（免认证） | frp 的 HTTP 服务在正常应答 |
| L3 插件面 | 对 `httpPlugins[].addr` 做 TCP connect | 登录链路是否可能成功 |

**最关键的约束：回滚判据 = L1 ∧ L2，L3 永不参与。** 否则会出现最坏情形——
插件服务临时抖动，frpsctl 把一份完全正确的配置回滚掉，而那份配置恰恰是
用于修复问题的那一份。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from enum import Enum

from .healthcheck import PluginTarget

__all__ = ["HealthLayer", "HealthReport", "probe_plugins"]


class HealthLayer(Enum):
    OK = "ok"
    FAIL = "fail"
    SKIPPED = "skipped"  # 未配置该层（webServer.port = 0 / 无 httpPlugins）
    UNKNOWN = "unknown"  # 未运行 / 拿不到判断依据


@dataclass(frozen=True)
class HealthReport:
    """三层健康报告。回滚与启动成功判据只看 L1 与 L2。"""

    l1_process: HealthLayer
    l2_control: HealthLayer
    l3_plugin: HealthLayer
    detail: str = ""
    ms: float = 0.0

    @property
    def gate(self) -> bool:
        """回滚/启动成功判据：L1 ∧ L2。SKIPPED 与 UNKNOWN 视为通过。

        UNKNOWN 为什么算通过：它表示"没有依据判定失败"（例如 dashboard 未启用
        时无法探 L2），把它当失败会让 `webServer.port = 0` 的合法配置永远
        无法通过启动检查。真正的失败必须是**观测到的失败**。
        """
        return self.l1_process is not HealthLayer.FAIL and self.l2_control is not HealthLayer.FAIL

    @property
    def plugin_warning(self) -> str | None:
        """L3 失败时的告警文本：不改变退出码，但必须显著提示。"""
        if self.l3_plugin is not HealthLayer.FAIL:
            return None
        return f"插件不可达：{self.detail or '见配置'} —— 客户端将无法登录（fail-closed），请先恢复插件服务"

    def render(self) -> str:
        """一行式展示（§7.4）。

        `detail` 在 **L2 或 L3 失败时**附加显示：L2 失败的原因是"dashboard 为什么
        没起来"，那恰恰是先要修的信息（控制面恢复前连统计都拿不到）。
        """
        parts = [
            f"L1 process {self.l1_process.value}",
            f"L2 control {self.l2_control.value}",
            f"L3 plugin {self.l3_plugin.value}",
        ]
        line = "  ".join(parts)
        if self.detail and (
            self.l2_control is HealthLayer.FAIL or self.l3_plugin is HealthLayer.FAIL
        ):
            line += f" ({self.detail})"
        return line


def probe_plugins(
    targets: list[PluginTarget],
    *,
    timeout: float = 1.0,
    connect=None,
) -> tuple[HealthLayer, str]:
    """L3 探针：**只做 TCP connect，不发 HTTP 请求**。

    两个原因：插件可能只接受 POST 且要求 `op` 参数，发 GET 会拿到 405/422
    这类"服务正常但语义不符"的噪声；更重要的是——绝不给插件增加一次真实
    业务调用（§11.2 要求插件 handler 内绝不做慢速外部调用）。

    `connect` 可注入，便于单测覆盖多地址/不可达/超时分支。

    返回 `(结论, 失败目标的展示串)`。
    """
    if not targets:
        return HealthLayer.SKIPPED, ""
    connector = connect or socket.create_connection
    for target in targets:
        try:
            with connector((target.host, target.port), timeout):
                continue
        except OSError:
            return HealthLayer.FAIL, f"{target.name} {target.host}:{target.port}"
    return HealthLayer.OK, ""
