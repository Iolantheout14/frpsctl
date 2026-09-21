"""Typer 应用与全局选项（0.3.0 从 cli/__init__.py 拆出）。

这里是命令树的组装点与全局回调（`_AnywhereGroup` / `--version` / `--instance`
补全）。命令实现见 `cli/commands/`，共享依赖装配见 `cli/runtime.py`。"""

from __future__ import annotations

from pathlib import Path
import typer
from .. import __version__
from ..core import platform as plat
from . import ui
from .context import build_context


_GLOBAL_VALUE_FLAGS = frozenset({"--instance", "-i", "--root", "--config", "--binary", "--admin-password"})


_GLOBAL_BOOL_FLAGS = frozenset({"--json", "--yes", "-y", "--verbose"})


class _AnywhereGroup(typer.core.TyperGroup):
    """让**全局选项出现在子命令之后**也能被识别。

    为什么必须做这件事：Typer/Click 原生只解析"子命令之前"的选项，而
    `frpsctl status --json` 这种写法既符合 README 的命令表，也符合所有人的直觉。
    早先的绕法是给**每一条**子命令手工加一个同名的 `--json` 选项再在 `with_json()`
    里做 OR —— 补了 19 处，于是 `--yes` / `--verbose` 写在后面仍然报
    `No such option: --yes`，而 README 却写着"全局选项写在子命令前后都可以"。
    逐命令打补丁是治标的：每加一条子命令、每加一个全局选项，都可能再漏一次。

    治本的做法是在**解析之前**把散落在子命令后面的全局选项整体搬到前面。这样只有
    这一处需要维护，且与子命令自身选项的优先级关系保持直观：

    - `frpsctl status --json` → `frpsctl --json status`
    - `frpsctl --instance web start` 原样不动（已经在前面）
    - 局部选项（`start --health-timeout 3`）不匹配全局名单，位置不变
    - `frpsctl install --version 0.71.0`：`--version` 不在名单里，因此仍归 `install`，
      不会被误当成根命令的 `--version`
    """

    def parse_args(self, ctx, args):  # noqa: ANN001, ANN201 - Click 的接口签名
        hoisted: list[str] = []
        rest: list[str] = []
        index = 0
        # `--` 之后的一切都是**位置参数**，绝不能再前移：用户显式声明了
        # "后面的 --json 是值而不是选项"（例如 `config set k -- --json`）。
        # 不遵守它会把值搬走，参数解析直接错位成 MissingParameter。
        terminated = False
        while index < len(args):
            item = args[index]
            if terminated:
                rest.append(item)
                index += 1
                continue
            if item == "--":
                rest.append(item)
                index += 1
                terminated = True
                continue
            if item in _GLOBAL_BOOL_FLAGS:
                hoisted.append(item)
                index += 1
                continue
            if item in _GLOBAL_VALUE_FLAGS:
                # 值紧随其后时一起搬；`--name=value` 形式已在下一个分支处理
                if index + 1 < len(args):
                    hoisted.extend([item, args[index + 1]])
                    index += 2
                else:
                    rest.append(item)
                    index += 1
                continue
            if any(item.startswith(f"{flag}=") for flag in _GLOBAL_VALUE_FLAGS):
                hoisted.append(item)
                index += 1
                continue
            rest.append(item)
            index += 1
        return super().parse_args(ctx, hoisted + rest)


app = typer.Typer(
    # 开启 shell 补全（`--install-completion` / `--show-completion`）。
    # 关闭它会让 `frpsctl config<TAB>` 这类日常操作永远不可用——没有理由关。
    add_completion=True,
    # 关掉 Click 的自动帮助：缺子命令属于**用法错误**，应当走退出码 2（§7.3），
    # 而不是打印帮助后退出 0 —— 那会让脚本误判为成功。
    no_args_is_help=False,
    help="把 frp 服务端（frps）包装成命令行工具：配置翻译器 + 进程保镖 + 状态聚合器。",
    cls=_AnywhereGroup,
)


config_app = typer.Typer(no_args_is_help=True, help="配置读写与变更闭环。")


service_app = typer.Typer(no_args_is_help=True, help="systemd 集成。")


plugin_app = typer.Typer(no_args_is_help=True, help="服务端插件：多用户鉴权 + 端口白名单 + 审计。")


plugin_service_app = typer.Typer(no_args_is_help=True, help="插件服务的 systemd 集成。")


plugin_user_app = typer.Typer(
    no_args_is_help=True, help="策略里的用户管理（结构化编辑，避免手写 JSON）。"
)


plugin_audit_app = typer.Typer(no_args_is_help=True, help="审计日志的只读查看（tail / stats）。")


plugin_config_app = typer.Typer(
    no_args_is_help=True, help="策略级设置（用户表用 `plugin user`；此处是其余字段）。"
)


web_app = typer.Typer(no_args_is_help=True, help="Web 管理台（内置界面，含进程控制与配置编辑）。")


web_service_app = typer.Typer(no_args_is_help=True, help="Web 管理台的 systemd 集成。")


web_password_app = typer.Typer(no_args_is_help=True, help="Web 管理台的登录口令管理。")
web_audit_app = typer.Typer(no_args_is_help=True, help="Web 操作审计的只读查看（登录与变更动作）。")


def _show_version(value: bool) -> bool:
    """`--version` 的 eager 回调。

    `is_eager=True` 让它在 Click 校验"必须给出子命令"**之前**执行——否则
    `frpsctl --version` 会被当成缺子命令而报用法错误（退出码 2），
    一个纯查询动作却失败了。
    """
    if value:
        ui.emit(f"frpsctl {__version__}")
        raise typer.Exit(0)
    return value


def _complete_instance(ctx, args, incomplete):  # noqa: ANN001, ARG001 - Typer autocompletion 接口
    """`--instance` 的 shell 补全：列出实例根下的实例名。

    接口是 Typer 的 `autocompletion(ctx, args, incomplete)`（返回字符串列表）。
    补全回调必须**零副作用**：只读目录、不建目录、任何失败都返回空列表
    （补全环境千奇百怪，绝不能因为补全把命令行本身搞坏）。它尽力读取
    `FRPSCTL_ROOT` / `FRPSCTL_DATA_HOME`；同一个命令行里另写的 `--root`
    在补全阶段尚未解析，属于已知局限。
    """
    try:
        from ..core.instance import list_instances, resolve_data_home, resolve_instances_root

        data_home = resolve_data_home()
        root = resolve_instances_root()
        names = [inst.name for inst in list_instances(root, data_home=data_home)]
    except Exception:  # noqa: BLE001 - 补全失败绝不影响命令行
        return []
    return [name for name in names if name.startswith(incomplete or "")]


@app.callback()
def _root(
    ctx: typer.Context,
    instance: str = typer.Option(
        None,
        "--instance",
        "-i",
        help="实例名（默认 default，可用 FRPSCTL_INSTANCE 覆盖）",
        autocompletion=_complete_instance,
    ),
    root: Path = typer.Option(None, "--root", help="实例根目录（默认 ~/.local/share/frpsctl/instances）"),
    config: Path = typer.Option(None, "--config", help="直接指定配置文件（覆盖实例默认）"),
    binary: Path = typer.Option(None, "--binary", help="直接指定 frps 二进制"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    admin_password: str = typer.Option(
        None, "--admin-password", help="dashboard 口令（优先于配置文件；也可用 FRPSCTL_ADMIN_PASSWORD）"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过交互确认"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="详细输出"),
    version: bool = typer.Option(  # noqa: ARG001 - 值由 eager callback 消费，函数体不需要它
        False,
        "--version",
        help="显示 frpsctl 版本",
        is_eager=True,
        callback=_show_version,
        # 值由 eager callback 处理；这里留着是为了让 Click 生成帮助条目
        expose_value=False,
    ),
) -> None:
    """解析全局选项。平台检查在这里做一次硬拒绝（§3.4）。"""
    plat.assert_supported()
    ctx.obj = build_context(
        instance=instance,
        root=root,
        config=config,
        binary=binary,
        json_output=json_output,
        admin_password=admin_password,
        yes=yes,
        verbose=verbose,
    )


# --- 命令组注册（0.3.0 拆分时从原文件搬来：AST 只见定义，注册调用要显式保留） ---
app.add_typer(config_app, name="config")
app.add_typer(service_app, name="service")
app.add_typer(plugin_app, name="plugin")
app.add_typer(web_app, name="web")
plugin_app.add_typer(plugin_service_app, name="service")
plugin_app.add_typer(plugin_user_app, name="user")
plugin_app.add_typer(plugin_audit_app, name="audit")
plugin_app.add_typer(plugin_config_app, name="config")
web_app.add_typer(web_service_app, name="service")
web_app.add_typer(web_password_app, name="password")
web_app.add_typer(web_audit_app, name="audit")
