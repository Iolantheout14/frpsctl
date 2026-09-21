"""命令面反射的单点实现（v0.3.5 R1/R2/R3）。

命令面（路径 + 参数 + 分类）此前只存在于**测试**里（`tests/test_contract_snapshot.py`
用 Typer 反射生成快照）。Web 管理台要在"命令"视图里展示同一份命令面——若各自
实现一套反射，两者迟早漂移：CLI 加了参数，快照会红，但界面仍显示旧参数。

因此把反射提炼到生产代码，三处消费**同一份数据**：

| 消费方 | 用途 |
|--------|------|
| `tests/test_contract_snapshot.py` | 命令面契约快照（脚本化契约，逐字节不变） |
| `tests/test_commands_export.py` | 生成物无 diff + 元数据覆盖全部命令 |
| `web/static/js/data/commands.js` | Web"命令"视图与 Ctrl+K（静态构建产物） |

**为什么元数据必须覆盖全部命令**：分类（只读 / 变更 / 需 root / 需 systemd）与
"Web 里对应哪个视图"是给用户看的**承诺**。漏一条会让界面标错危险等级，或让某条
命令在参考视图里凭空消失——而这类错误不会让任何既有测试变红。所以
`command_surface()` 默认严格：元数据缺一条即抛 `CommandMetaMissing`。

**分层说明**：本模块属于 `cli/`，通过 Typer 反射读命令树；Web 侧**不 import 它**
（那会让 web 依赖 cli），而是消费它生成的静态 JS 模块（`--write`）。生成物随
wheel 分发，Web 零请求直读，`node --check` 与 import 图守卫自动覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "COMMAND_META",
    "CommandMeta",
    "CommandMetaMissing",
    "build_snapshot",
    "command_surface",
    "generated_module_path",
    "main",
    "render_js",
]


class CommandMetaMissing(RuntimeError):
    """命令面元数据未覆盖全部命令（漏一条即失败——界面承诺不能静默缺失）。"""


@dataclass(frozen=True)
class CommandMeta:
    """一条命令的展示与安全分类（Web 命令参考视图的数据来源）。

    - `readonly`：只读命令（查询 / 探测 / 展示），不改变任何持久化状态；
    - `needs_root`：**总是**需要 root（systemd unit 的写操作、卸载 unit）；
    - `needs_systemd`：与 systemd 托管有关（root 的 service 命令；根命令
      `start/stop/restart` 是**条件**需要——实例由 systemd 托管时才委托
      `systemctl`，direct 模式下普通用户即可）；
    - `web_view`：Web 管理台里与之对应的视图（`dash`/`config`/`audit`/
      `services`/`versions`/`commands`）；`None` 表示界面暂无对应能力
      （例如插件策略编辑、`plugin serve` 前台形态）。
    """

    readonly: bool
    needs_root: bool = False
    needs_systemd: bool = False
    web_view: str | None = None


#: 全部 63 条叶子命令的分类。**必须完整**（由 `command_surface()` 强制）。
#: 分类依据是源码行为（docstring、core 调用、`_require_root`、`serve_guard`），
#: 不是命令名——改动任一命令的语义时同步更新这里，否则界面在说谎。
COMMAND_META: dict[str, CommandMeta] = {
    # --- 顶层单命令（16） ---
    "capabilities": CommandMeta(readonly=True, web_view="commands"),
    "clients": CommandMeta(readonly=True, web_view="dash"),
    "doctor": CommandMeta(readonly=True, web_view="dash"),
    "init": CommandMeta(readonly=False, web_view="config"),
    "install": CommandMeta(readonly=False, web_view="versions"),
    "instances": CommandMeta(readonly=True),
    "log": CommandMeta(readonly=True, web_view="dash"),
    "proxies": CommandMeta(readonly=True, web_view="dash"),
    "prune": CommandMeta(readonly=False, web_view="dash"),
    "restart": CommandMeta(readonly=False, needs_systemd=True, web_view="dash"),
    "start": CommandMeta(readonly=False, needs_systemd=True, web_view="dash"),
    "status": CommandMeta(readonly=True, web_view="dash"),
    "stop": CommandMeta(readonly=False, needs_systemd=True, web_view="dash"),
    "traffic": CommandMeta(readonly=True, web_view="dash"),
    "uninstall": CommandMeta(readonly=False, needs_root=True),
    "verify": CommandMeta(readonly=True, web_view="config"),
    # --- config（8） ---
    "config get": CommandMeta(readonly=True, web_view="config"),
    "config list": CommandMeta(readonly=True, web_view="config"),
    "config diff": CommandMeta(readonly=True, web_view="config"),
    "config set": CommandMeta(readonly=False, web_view="config"),
    "config unset": CommandMeta(readonly=False, web_view="config"),
    "config edit": CommandMeta(readonly=False, web_view="config"),
    "config rollback": CommandMeta(readonly=False, web_view="config"),
    "config apply": CommandMeta(readonly=False, web_view="config"),
    # --- service（4） ---
    "service install": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "service uninstall": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "service status": CommandMeta(readonly=True, needs_systemd=True, web_view="services"),
    "service logs": CommandMeta(readonly=True, needs_systemd=True, web_view="services"),
    # --- plugin（20） ---
    "plugin init": CommandMeta(readonly=False),
    "plugin check": CommandMeta(readonly=True, web_view="services"),
    "plugin serve": CommandMeta(readonly=False),
    "plugin start": CommandMeta(readonly=False, web_view="services"),
    "plugin stop": CommandMeta(readonly=False, web_view="services"),
    "plugin restart": CommandMeta(readonly=False, web_view="services"),
    "plugin status": CommandMeta(readonly=True, web_view="services"),
    "plugin user list": CommandMeta(readonly=True),
    "plugin user set": CommandMeta(readonly=False),
    "plugin user remove": CommandMeta(readonly=False),
    "plugin audit tail": CommandMeta(readonly=True, web_view="audit"),
    "plugin audit stats": CommandMeta(readonly=True, web_view="audit"),
    "plugin config list": CommandMeta(readonly=True),
    "plugin config set": CommandMeta(readonly=False),
    "plugin service install": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "plugin service start": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "plugin service stop": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "plugin service restart": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "plugin service uninstall": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "plugin service status": CommandMeta(readonly=True, needs_systemd=True, web_view="services"),
    # --- web（15） ---
    "web serve": CommandMeta(readonly=False),
    "web start": CommandMeta(readonly=False, web_view="services"),
    "web stop": CommandMeta(readonly=False, web_view="services"),
    "web restart": CommandMeta(readonly=False, web_view="services"),
    "web status": CommandMeta(readonly=True, web_view="services"),
    "web password set": CommandMeta(readonly=False),
    "web password show": CommandMeta(readonly=True),
    "web audit tail": CommandMeta(readonly=True, web_view="audit"),
    "web audit stats": CommandMeta(readonly=True, web_view="audit"),
    "web service install": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "web service start": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "web service stop": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "web service restart": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "web service uninstall": CommandMeta(
        readonly=False, needs_root=True, needs_systemd=True, web_view="services"
    ),
    "web service status": CommandMeta(readonly=True, needs_systemd=True, web_view="services"),
}

#: 生成物路径（Web 静态资源：作为 ESM 模块被"命令"视图 import）。
GENERATED_MODULE = "src/frpsctl/web/static/js/data/commands.js"


def generated_module_path(root: Path | None = None) -> Path:
    """生成物的绝对路径（默认从本文件回溯到仓库根）。"""
    base = root if root is not None else Path(__file__).resolve().parents[3]
    return base / GENERATED_MODULE


def _default_desc(value: object) -> object:
    """默认值的稳定描述（不存 repr，避免内存地址之类的不稳定输出）。

    与 `tests/test_contract_snapshot.py` 的旧实现逐字节一致——快照基线必须
    保持不变（提炼实现是重构，不是命令面变更）。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return f"<{type(value).__name__}>:{value}"


def _default_text(value: object) -> str | None:
    """给界面看的默认值文本（`<PosixPath>:/x` → `/x`；`None`/空 → 不展示）。"""
    if value is None or value == "" or value == ():
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text.startswith("<") and ">:" in text:
        text = text.split(">:", 1)[1]
    return text


def _is_positional(param: Any) -> bool:
    """是否位置参数（Click 的 `Argument`）。

    ⚠️ Click 的 `Argument` **也有 `opts` 属性**（值是不带前缀的自身名，如 `key`），
    因此"有没有 opts"区分不开选项与位置参数——用类判定，并以"没有 `-` 前缀的
    选项名"兜底。快照契约不受影响（`with_help=False` 时不写这个字段）。
    """
    from click import Argument

    if isinstance(param, Argument):
        return True
    return not any(str(opt).startswith("-") for opt in getattr(param, "opts", []))


def _param_entry(param: Any, *, with_help: bool) -> dict:
    """单个参数的契约形状（`with_help=True` 时额外带界面用的 help/default_text）。"""
    if not hasattr(param, "opts"):
        entry: dict = {
            "kind": "arg",
            "name": param.name,
            "required": bool(getattr(param, "required", False)),
            "nargs": int(getattr(param, "nargs", 1)),
        }
        if with_help:
            entry["help"] = str(getattr(param, "help", "") or "")
            entry["positional"] = True
        return entry
    entry = {
        "kind": "opt",
        "name": param.name,
        "opts": sorted(param.opts),
        "secondary": sorted(getattr(param, "secondary_opts", [])),
        "is_flag": bool(getattr(param, "is_flag", False)),
        "multiple": bool(getattr(param, "multiple", False)),
        "required": bool(getattr(param, "required", False)),
        "default": _default_desc(param.default),
    }
    ptype = getattr(param, "type", None)
    if hasattr(ptype, "min") or hasattr(ptype, "max"):
        entry["min"] = getattr(ptype, "min", None)
        entry["max"] = getattr(ptype, "max", None)
    if with_help:
        entry["help"] = str(getattr(param, "help", "") or "")
        entry["default_text"] = _default_text(param.default)
        entry["positional"] = _is_positional(param)
    return entry


def _describe(cmd: Any, *, with_help: bool = False) -> list[dict]:
    return sorted(
        (_param_entry(param, with_help=with_help) for param in cmd.params),
        key=lambda item: (item["kind"], item["name"]),
    )


def _walk(group: Any, prefix: str = ""):
    for name in sorted(group.commands):
        sub = group.commands[name]
        path = f"{prefix} {name}".strip()
        if hasattr(sub, "commands"):
            yield from _walk(sub, path)
        else:
            yield path, sub


def _command_tree() -> Any:
    """根 Click 命令（导入 `cli` 包即完成命令注册）。"""
    from typer.main import get_command

    import frpsctl.cli  # noqa: F401 - 触发命令注册（导入即注册）
    from .app import app

    return get_command(app)


def build_snapshot() -> dict[str, dict]:
    """命令面契约快照（键：命令路径 + `(root)`；值：参数面）。

    **形状与 v0.3.0 的测试内实现逐字节一致**——它是既有快照基线的生成器，
    提炼到生产代码时不得改变输出（否则快照会无意义地全量 diff）。
    """
    command = _command_tree()
    snapshot: dict[str, dict] = {"(root)": {"params": _describe(command)}}
    for path, cmd in sorted(_walk(command)):
        snapshot[path] = {"params": _describe(cmd)}
    return snapshot


def _summary(cmd: Any) -> str:
    """命令摘要（Click help 的第一段首行；不手写，避免文案漂移）。"""
    text = str(getattr(cmd, "help", "") or getattr(cmd, "short_help", "") or "")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def command_surface() -> dict[str, Any]:
    """给 Web 命令参考视图用的完整命令面（严格校验元数据覆盖）。"""
    from .. import __version__
    from ..core.version import MINIMUM_VERSION, RECKONED_VERSION
    from ..env import ENV_VARS
    from ..errors import ExitCode

    command = _command_tree()
    entries: list[dict[str, Any]] = []
    for path, cmd in sorted(_walk(command)):
        meta = COMMAND_META.get(path)
        if meta is None:
            raise CommandMetaMissing(
                f"命令 {path!r} 缺少分类元数据——请在 cli/introspect.py 的 COMMAND_META 中补上"
                "（界面承诺只读/变更/需 root 与 Web 对应视图）"
            )
        entries.append(
            {
                "path": path,
                "group": path.split(" ", 1)[0] if " " in path else "",
                "name": path.split(" ")[-1],
                "summary": _summary(cmd),
                "readonly": meta.readonly,
                "needs_root": meta.needs_root,
                "needs_systemd": meta.needs_systemd,
                "web_view": meta.web_view,
                "params": _describe(cmd, with_help=True),
            }
        )
    missing = sorted(set(COMMAND_META) - {entry["path"] for entry in entries})
    if missing:
        raise CommandMetaMissing(
            f"元数据含不存在的命令：{missing}（命令被删除/改名后必须同步清理 COMMAND_META）"
        )

    def _render(version: tuple[int, int, int]) -> str:
        return ".".join(str(part) for part in version)

    return {
        "version": __version__,
        "frps_minimum": _render(MINIMUM_VERSION),
        "frps_reckoned": _render(RECKONED_VERSION),
        "exit_codes": {member.name: int(member) for member in ExitCode},
        "env_vars": dict(ENV_VARS),
        "commands": entries,
    }


_HEADER = """\
/** 命令面数据（Web"命令"视图与 Ctrl+K 的数据源）。
 *
 *  ⚠️ 本文件由生成器写入：`python -m frpsctl.cli.introspect --write`
 *  数据源 = `cli/introspect.py::command_surface()`，与命令面契约快照
 *  （tests/snapshots/cli_commands.json）同源。**请勿手工编辑**——CI 会跑
 *  `--check` 断言重新生成后无 diff（界面与 CLI 命令面不允许漂移）。
 */

"""


def render_js(surface: dict[str, Any] | None = None) -> str:
    """把命令面渲染为 ESM 模块（静态构建产物，浏览器零请求直读）。"""
    payload = surface if surface is not None else command_surface()
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{_HEADER}export const COMMAND_SURFACE = {body};\n"


def _write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_js(), "utf-8")


def main(argv: list[str] | None = None) -> int:
    """生成器入口：`--write` 写生成物 / `--check` 断言无漂移（CI 用）。"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    path = generated_module_path()
    if args == ["--write"]:
        _write(path)
        print(f"已生成：{path}")
        return 0
    if args == ["--check"]:
        expected = render_js()
        try:
            actual = path.read_text("utf-8")
        except OSError:
            print(f"✗ 生成物缺失：{path}（运行 `python -m frpsctl.cli.introspect --write`）")
            return 1
        if actual != expected:
            print(
                f"✗ 生成物与命令面不一致：{path}\n"
                "  运行 `python -m frpsctl.cli.introspect --write` 并提交生成物"
            )
            return 1
        print(f"✓ 命令面生成物一致：{path}")
        return 0
    print("用法：python -m frpsctl.cli.introspect --write | --check")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
