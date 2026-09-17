"""CLI 共享上下文与参数解析（设计文档 §7.1、§7.3）。

`--instance` / `--root` / `--config` / `--binary` 之类的全局选项在这里收敛成
一个 `AppContext`，各命令只跟它打交道——`cli/` 因此保持"薄"（§5）。
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import click
import typer

from ..errors import ExitCode, FrpsctlError
from ..core.instance import Instance, resolve_data_home, resolve_instances_root
from . import ui

__all__ = ["AppContext", "build_context", "run_cli"]

#: 默认值哨兵：区分"用户没给 --instance"与"给了空串"。
_DEFAULT_INSTANCE = "default"


@dataclass(frozen=True)
class AppContext:
    """全局选项的解析结果。"""

    instance: Instance
    binary: Path | None
    json: bool
    yes: bool
    verbose: bool
    admin_password: str | None

    @property
    def config_path(self) -> Path:
        return self.instance.config

    def with_json(self, flag: bool) -> AppContext:
        """让子命令级 `--json` 与全局 `--json` 等价。

        只提供全局选项时，`frpsctl status --json` 会报 "No such option"——
        而设计文档的命令表写的是 `status … [--json]`，用户的直觉也是写在后面。
        两者都支持：子命令选项优先，未给则沿用全局值。
        """
        if not flag or self.json:
            return self
        return replace(self, json=True)


def build_context(
    *,
    instance: str | None,
    root: Path | None,
    config: Path | None,
    binary: Path | None,
    json_output: bool,
    yes: bool,
    verbose: bool,
    admin_password: str | None = None,
) -> AppContext:
    """把 CLI 参数解析成 `AppContext`。

    实例名解析优先级（§7.1）：`--instance` > `FRPSCTL_INSTANCE` > `default`。
    `--config` 是**覆盖**实例默认配置路径，不改变实例本身
    （state / 锁 / 快照仍按实例走，否则会与所有权判定脱节）。
    """
    name = instance or os.environ.get("FRPSCTL_INSTANCE") or _DEFAULT_INSTANCE
    # 凭据优先级（§8.5）：--admin-password > FRPSCTL_ADMIN_PASSWORD > 配置文件。
    # 让命令行/环境变量优先，是为了不改动配置文件就能临时排查——而配置文件里
    # 那份仍然生效，不需要重复维护。
    resolved_password = admin_password or os.environ.get("FRPSCTL_ADMIN_PASSWORD") or None
    inst = Instance(
        name=name,
        instances_root=(root or resolve_instances_root()).expanduser(),
        data_home=resolve_data_home(),
        config_override=config.expanduser() if config else None,
    )
    # `--verbose` 是进程级诊断开关：它要影响的调用点在 core/ 里（下载与 verify 的
    # 子进程），那些函数没有 CLI 上下文。见 `ui.set_verbose` 的说明。
    ui.set_verbose(verbose)
    return AppContext(
        instance=inst,
        binary=binary.expanduser() if binary else None,
        json=json_output,
        yes=yes,
        verbose=verbose,
        admin_password=resolved_password,
    )


def run_cli(app: typer.Typer) -> None:
    """统一异常 → 退出码映射的唯一出口（§7.3）。

    用 `standalone_mode=False` 调用：这样 Click 不会自己吞掉异常或抢先
    `sys.exit(1)`，异常会一路冒到下面这张表，由**我们**决定退出码。走默认的
    standalone 模式时，任何 `FrpsctlError` 都会变成退出码 1——那张精心设计的
    退出码表（脚本化契约）就形同虚设。

    `standalone_mode=True`：让 Click 自己把**用法错误**渲染成它惯用的
    "Usage: … / Error: …" 形式并退出 2——那正是 §7.3 对退出码 2 的定义。
    而业务异常（`FrpsctlError`）不在 Click 的处理范围内，会正常冒上来由下面
    的分支映射。

    `--help` 与显式 `typer.Exit(n)` 走 Click 的 `Exit` 分支，原样透传退出码。
    """
    with map_exceptions():
        app(standalone_mode=True)


@contextlib.contextmanager
def map_exceptions() -> Iterator[None]:
    """把异常翻译成退出码（§7.3 的唯一定义处）。

    单独成上下文管理器，是为了让**测试**能用同一条路径：测试里直接
    `app(standalone_mode=False)` 时，Click 会把异常原样抛给调用者而**不做**
    退出码映射，于是所有"退出码"断言都会看到 1——测的就不是真实契约了。

    ⚠️ 这里用**鸭子类型**而不是 `except click.UsageError`：Typer 0.27 内嵌了
    自己的 click（`typer._click`），其异常类与顶层 `click` 包的异常类**互不
    相识**——`isinstance(e, click.UsageError)` 返回 False，捕获会静默失效，
    用法错误就退化成"未分类错误（1）"。Click 的异常都带 `exit_code` 属性，
    据此判定既稳又不必绑定某个具体模块。
    """
    try:
        yield
    except SystemExit:
        raise
    except click.Abort:
        ui.warn("已中断")
        raise SystemExit(130) from None
    except FrpsctlError as exc:
        ui.warn(f"错误：{exc.render()}")
        raise SystemExit(int(exc.exit_code)) from None
    except KeyboardInterrupt:
        ui.warn("已中断")
        raise SystemExit(130) from None
    except BrokenPipeError:
        # 例如 `frpsctl log | head`——正常终止，不要打印回溯吓人
        with contextlib.suppress(OSError):
            sys.stderr.close()
        raise SystemExit(0) from None
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，必须给退出码
        # Click 家族的两类异常都带 `exit_code`（用法错误族默认 2）：
        # - 用法错误族（UsageError / NoSuchOption / BadParameter…）带
        #   `format_message()`，需要打印消息；
        # - `Exit`（`raise typer.Exit(1)` / `typer.Exit(subprocess.call(...))`）
        #   是**静默退出**，不打印消息。
        # `standalone_mode=False` 时 Click 会把 `Exit` 变成返回值（由调用方
        # 接收），但直接调用（如测试）若忽略返回值，退出码就会表现为 0——
        # 这里统一转成 SystemExit 兜底，保证任何调用方式下退出码都真实。
        code = getattr(exc, "exit_code", None)
        if isinstance(code, int):
            if hasattr(exc, "format_message"):
                try:
                    ui.warn(str(exc.format_message()))
                except Exception:  # noqa: BLE001 - 渲染失败不影响退出码
                    ui.warn(str(exc))
            raise SystemExit(code) from None

        ui.warn(f"未分类错误：{type(exc).__name__}: {exc}")
        if os.environ.get("FRPSCTL_TRACEBACK"):
            raise
        ui.warn("设置 FRPSCTL_TRACEBACK=1 可查看完整回溯")
        raise SystemExit(int(ExitCode.UNCLASSIFIED)) from None
