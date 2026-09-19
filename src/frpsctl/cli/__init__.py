"""frpsctl 命令层（设计文档 §5、§7）。

**薄层约束**：`cli/` 不直接调用 `subprocess` 或 `httpx`，只调用 `core/`；
`core/` 不打印任何东西。这条约束让 `--json` 与人读输出共享同一份逻辑，
也让"命令的行为"可以脱离终端被测试。

0.3.0 起实现拆分为命令包（本模块保留兼容 re-export，一个版本周期后收敛）：

| 位置 | 职责 |
|------|------|
| `cli/app.py` | Typer 应用、全局选项与回调（命令树组装点） |
| `cli/runtime.py` | 共享依赖装配（实例/生命周期/Admin/策略/可执行文件） |
| `cli/commands/*` | 按域拆分的命令实现（导入即注册） |
| `cli/ui.py` | 输出渲染（人读 / `--json`） |
| `frpsctl/report.py` | CLI 与 Web 共享的响应形状（表示层） |
"""

from __future__ import annotations

import os as _os
import sys as _sys


def _is_version_fastpath() -> bool:
    """`--version` 快路径的**触发条件**（v0.3.1 review 收窄）。

    除"恰好单个 `--version`"外，还要求**当前进程以 frpsctl 身份入口**：
    - console script / uv-tool shim：`argv[0]` 以 `frpsctl` 开头；
    - `python -m frpsctl`：`__main__` 的 spec 是 `frpsctl.__main__`。

    `frpsctl.cli` 也被 `capabilities.command_paths()` 作为库 import——若无条件
    判断 argv，任何宿主程序只要命令行恰为 `--version` 就会被劫持成"打印
    frpsctl 版本并退出"。这条限定让快路径只服务于真正的 CLI 入口。
    """
    if _sys.argv[1:] != ["--version"]:
        return False
    if _os.path.basename(_sys.argv[0] or "").startswith("frpsctl"):
        return True
    main_mod = _sys.modules.get("__main__")
    spec = getattr(main_mod, "__spec__", None)
    return getattr(spec, "name", "") == "frpsctl.__main__"


# `--version` 快路径（v0.3.1）：这是唯一能在**任何重 import 之前**拦截的
# 高频脚本调用（探测工具是否存在/版本）。完整命令链要加载 typer /
# pydantic / httpx（实测冷启动 0.3–1.4 秒），版本查询不该为它们付费。
# 其它组合（`--version --json` 等）照旧走 Click 的 eager 解析，行为一致。
if _is_version_fastpath():
    from .. import __version__

    _sys.stdout.write(f"frpsctl {__version__}\n")
    raise SystemExit(0)

from . import ui
from .app import (
    _GLOBAL_BOOL_FLAGS,
    _GLOBAL_VALUE_FLAGS,
    _AnywhereGroup,
    _complete_instance,
    _root,
    _show_version,
    app,
    config_app,
    plugin_app,
    plugin_audit_app,
    plugin_config_app,
    plugin_service_app,
    plugin_user_app,
    service_app,
    web_app,
    web_password_app,
    web_service_app,
)
from .commands import config as _commands_config  # noqa: F401 - 触发命令注册
from .commands import install as _commands_install  # noqa: F401
from .commands import lifecycle as _commands_lifecycle  # noqa: F401
from .commands import observe as _commands_observe  # noqa: F401
from .commands import plugin as _commands_plugin  # noqa: F401
from .commands import service as _commands_service  # noqa: F401
from .commands import web as _commands_web  # noqa: F401
from .commands.config import (
    _json_change_value,
    _plain,
    _resolve_value_input,
    _render_change_outcome,
    _render_leaf,
    _render_tree,
    _resolve_editor,
    config_diff,
    config_edit,
    config_get,
    config_list,
    config_rollback,
    config_set,
    config_unset,
)
from .commands.install import (
    _parse_port_ranges,
    _render_init_config,
    _render_uninstall_plan,
    init,
    install,
    uninstall,
    verify,
)
from .commands.lifecycle import (
    _emit_health_warnings,
    _follow_file,
    _health_tick,
    _print_status,
    _require_healthy,
    _start_payload,
    _status_payload,
    _tail_file,
    log,
    restart,
    start,
    status,
    stop,
)
from .commands.observe import (
    _render_traffic_rows,
    capabilities,
    _render_traffic_total,
    _text,
    clients,
    doctor,
    instances,
    prune,
    proxies,
    traffic,
)
from .commands.plugin import (
    _POLICY_KEYS,
    _audit_line,
    _audit_target,
    _parse_policy_value,
    _policy_value,
    _probe_proxy_name,
    _render_policy_template,
    _sample_decisions,
    plugin_audit_stats,
    plugin_audit_tail,
    plugin_check,
    plugin_config_list,
    plugin_config_set,
    plugin_init,
    plugin_serve,
    plugin_service_install,
    plugin_service_restart,
    plugin_service_start,
    plugin_service_status,
    plugin_service_stop,
    plugin_service_uninstall,
    plugin_user_list,
    plugin_user_remove,
    plugin_user_set,
)
from .commands.service import (
    service_install,
    service_logs,
    service_status,
    service_uninstall,
)
from .commands.web import (
    _web_audit_line,
    _web_audit_target,
    web_audit_stats,
    web_audit_tail,
    web_password_set,
    web_password_show,
    web_serve,
    web_service_install,
    web_service_restart,
    web_service_start,
    web_service_status,
    web_service_stop,
    web_service_uninstall,
)
from .context import run_cli
from .runtime import (
    FOLLOW_INTERVAL,
    _complete_config_key,
    _complete_policy_user,
    _admin,
    _emit_stream_line,
    _reopen_if_rotated,
    _follow_audit,
    _ctx,
    _frpsctl_executable,
    _lifecycle,
    _load_policy,
    _load_policy_raw,
    _policy_path,
    _require_admin,
    _save_policy_raw,
    _split_spec,
    _web_password_from_file,
)


def main() -> None:
    """控制台入口（pyproject 的 `frpsctl` 指向这里）。"""
    run_cli(app)


if __name__ == "__main__":
    main()
