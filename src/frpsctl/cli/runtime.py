"""CLI 共享依赖装配点（0.3.0 从 cli/__init__.py 拆出）。

所有跨命令共享的"获取 / 构造"逻辑集中在这里：实例上下文、Lifecycle、
AdminClient、策略读写、可执行文件定位、值输入通道、跟随工具。命令模块
通过**模块对象**调用它（`from .. import runtime; runtime._ctx(ctx)`）——
这样测试替换这里的符号（`frpsctl.cli.runtime._frpsctl_executable`）对所有
命令一致生效，而不会因 `from` 导入复制引用而静默失效。"""

from __future__ import annotations

import time

import json

import contextlib

import os
import sys
from pathlib import Path
import typer
from ..core import config as cfg
from ..core import healthcheck
from ..core.admin import (
    AdminClient,
)
from ..core.auditlog import (
    resolve_policy_path,
)
from ..core.lifecycle import Lifecycle
from ..core.systemd import ServiceIdentity
from ..plugin.policy import PluginPolicy
from ..errors import (
    AdminUnreachable,
    ConfigError,
)
from .context import AppContext


def _ctx(ctx: typer.Context) -> AppContext:
    obj = ctx.find_root().obj
    assert isinstance(obj, AppContext)
    return obj


def _lifecycle(app_ctx: AppContext) -> Lifecycle:
    return Lifecycle(app_ctx.instance, binary=app_ctx.binary)


def _admin(app_ctx: AppContext) -> AdminClient | None:
    """构造 Admin 客户端；未启用 dashboard 时返回 None。"""
    dash = healthcheck.parse_dashboard(app_ctx.config_path)
    if not dash.enabled:
        return None
    password = app_ctx.admin_password or dash.password
    return AdminClient(dash.base_url, dash.user, password)


def _require_admin(app_ctx: AppContext, *, feature: str) -> AdminClient:
    """需要 dashboard 的命令的统一入口；未启用时退出码 7（§7.3）。

    不用配置错误(3)：脚本据此区分"配置写错了"与"这个功能当前不可用"。
    """
    client = _admin(app_ctx)
    if client is None:
        raise AdminUnreachable(
            f"dashboard 未启用（webServer.port = 0），无法{feature}",
            hint="该命令依赖 Admin API；请在配置里设置 webServer.port",
        )
    return client


def _policy_path(app_ctx: AppContext, override: Path | None) -> Path:
    """策略文件位置：`--policy` > `FRPSCTL_PLUGIN_POLICY` > `<实例>/plugin-policy.json`。

    实现在 `core/auditlog.resolve_policy_path`——CLI、插件服务与 Web 审计视图
    必须指向**同一个**文件，因此规则只有一份。
    """
    return resolve_policy_path(app_ctx.instance, override)


def _load_policy(app_ctx: AppContext, override: Path | None) -> tuple[Path, PluginPolicy]:
    path = _policy_path(app_ctx, override)
    return path, PluginPolicy.load(path)


def _load_policy_raw(path: Path) -> dict:
    """读策略文件的**原始字典**——结构化编辑必须保留未知键（`_comment` 等）。"""
    import json as _json

    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"策略文件不存在：{path}",
            hint="先运行 `frpsctl plugin init --policy <path>` 生成一份模板",
        ) from None
    except OSError as exc:
        raise ConfigError(f"无法读取策略文件 {path}：{exc}") from None
    try:
        raw = _json.loads(text)
    except _json.JSONDecodeError as exc:
        raise ConfigError(f"策略文件不是合法 JSON：{path}（{exc}）") from None
    if not isinstance(raw, dict):
        raise ConfigError("策略文件根节点必须是对象")
    return raw


def _save_policy_raw(path: Path, raw: dict) -> PluginPolicy:
    """复验（与 `plugin check` 同一判据）→ 0600 原子写；返回解析结果。"""
    import json as _json

    loaded = PluginPolicy.parse(raw)
    # bind 是运行期参数；离线校验用默认回环地址——与 `plugin check` 的默认一致，
    # 保证"写得进去的策略一定通过 check"。
    loaded.validate(bind="127.0.0.1")
    cfg.atomic_write(path, _json.dumps(raw, indent=2, ensure_ascii=False) + "\n", mode=0o600)
    return loaded


def _split_spec(spec: str) -> list[str]:
    """把 `6000-6010,7000` 拆成列表（空项忽略）。"""
    return [chunk.strip() for chunk in spec.split(",") if chunk.strip()]


def _frpsctl_executable() -> Path:
    """定位 frpsctl 可执行文件（v0.3.4：实现下沉 core.serve_runtime，CLI 与 Web 单点共用）。"""
    from ..core.serve_runtime import frpsctl_executable

    return frpsctl_executable()


def _identity_summary(identity: ServiceIdentity) -> str:
    """服务账户解析结果的一行摘要（三个 service install 共用同一口径）。"""
    if identity.source == "default-frps":
        return f"服务用户：{identity.user}（默认：系统已有 {identity.user} 用户）"
    if identity.source == "default-current":
        return f"服务用户：{identity.user}（默认：无 frps 用户，使用当前用户）"
    return f"服务用户：{identity.user}"


def _identity_warnings(identity: ServiceIdentity) -> list[str]:
    """账户相关告警（顺序：创建 → 组回退 → root 安全提示）。"""
    out: list[str] = []
    if identity.created:
        out.append(f"已创建系统用户 {identity.user}（--system，无家目录，nologin）")
    out.extend(f"注：{note}" for note in identity.notes)
    if identity.uid == 0:
        out.append(
            "⚠ 服务用户为 root：unit 的加固仍在，但以 root 运行会扩大风险面；"
            "建议改用专用账户（--user <名字> --create-user 可自动创建）"
        )
    return out


def _web_password_from_file(path: Path | None) -> str | None:
    """从 `--password-file` 读口令；文件缺失/为空给出配置错误(3)。"""
    if path is None:
        return None
    try:
        value = path.read_text("utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(
            f"口令文件不存在：{path}",
            hint="先运行 `frpsctl web service install` 生成，或去掉 --password-file 用自动生成的口令",
        ) from None
    except OSError as exc:
        raise ConfigError(f"无法读取口令文件 {path}：{exc}") from None
    if not value:
        raise ConfigError(
            f"口令文件为空：{path}",
            hint="删除该文件让服务重新生成，或手工写入一个口令",
        )
    return value


def _emit_stream_line(text: str) -> None:
    """流式输出单行并**立即 flush**。

    `ui.emit` 是一次性输出的语义（依赖进程退出时冲刷），而跟随类是**长驻**
    进程：stdout 重定向到文件/管道时是块缓冲，不 flush 的话新记录会延迟到
    缓冲满（或进程被杀）才出现——跟随就失去了意义。`frpsctl log -f` 的
    `_follow_file` 一直是这么做的，这里保持一致。

    **不吞 BrokenPipeError**：`plugin audit tail | head` 类管道提前关闭时，
    异常冒泡到 `map_exceptions` 的 BrokenPipeError 分支（静默退出 0）——
    若在这里 suppress，跟随循环会带着一个断掉的管道永远转下去。
    """
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _follow_audit(path: Path, render) -> None:
    """持续输出新追加的审计记录（渲染行；文件轮转时自动重开）。

    文件**尚不存在**时等待其出现（v0.3.0 review：审计启用但还没有任何记录
    时，旧实现直接 FileNotFoundError → 未分类错误 1；`web audit tail`
    已提前拦截，这里让两者行为一致且真正"能跟随"）。
    """
    handle = None
    try:
        while handle is None:
            try:
                handle = open(path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
            except FileNotFoundError:
                time.sleep(FOLLOW_INTERVAL)
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if line:
                text = line.strip()
                if text:
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        continue  # 半截行（正在写入）：跳过，下一轮可能补全
                    if isinstance(record, dict):
                        _emit_stream_line(render(record))
                continue
            time.sleep(FOLLOW_INTERVAL)
            handle = _reopen_if_rotated(path, handle)
    except KeyboardInterrupt:
        return
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()


#: `log` / `audit tail` 等跟随类命令的轮询间隔（秒）。
FOLLOW_INTERVAL = 0.3


def _reopen_if_rotated(path: Path, handle):
    """`path` 指向的 inode 与已打开的 handle 不一致时重开；否则原样返回。"""
    try:
        if path.stat().st_ino != os.fstat(handle.fileno()).st_ino:
            with contextlib.suppress(OSError):
                handle.close()
            return open(path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
    except FileNotFoundError:
        # 文件刚被移走、新的还没建：保持旧 handle，下一轮再试
        pass
    return handle


def _complete_config_key(ctx, args, incomplete):  # noqa: ANN001, ARG001 - Typer 补全接口
    """`config get/set/unset <TAB>`：列出当前配置里的点分键。

    与 `--instance` 补全同一条纪律：**零副作用**、任何失败返回空列表。
    实例名从环境解析（补全阶段全局选项尚未生效，属已知局限）。
    """
    try:
        from ..core.instance import Instance, resolve_data_home, resolve_instances_root

        name = os.environ.get("FRPSCTL_INSTANCE") or "default"
        inst = Instance(
            name=name,
            instances_root=resolve_instances_root(),
            data_home=resolve_data_home(),
        )
        doc = cfg.load_config(inst.config)
        keys = [key for key, _ in cfg.flatten_tree(doc)]
    except Exception:  # noqa: BLE001 - 补全失败绝不影响命令行
        return []
    return [key for key in keys if key.startswith(incomplete or "")]


def _complete_policy_user(ctx, args, incomplete):  # noqa: ANN001, ARG001
    """`plugin user remove <TAB>`：列出策略里的用户名（零副作用）。"""
    try:
        from ..core.auditlog import load_view
        from ..core.instance import Instance, resolve_data_home, resolve_instances_root

        name = os.environ.get("FRPSCTL_INSTANCE") or "default"
        inst = Instance(
            name=name,
            instances_root=resolve_instances_root(),
            data_home=resolve_data_home(),
        )
        view = load_view(inst)
        if not view.available:
            return []
        raw = json.loads(view.policy_path.read_text("utf-8"))
        users = raw.get("users") if isinstance(raw, dict) else None
        keys = [str(key) for key in users] if isinstance(users, dict) else []
    except Exception:  # noqa: BLE001
        return []
    return [key for key in keys if key.startswith(incomplete or "")]
