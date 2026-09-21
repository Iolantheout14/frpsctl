"""命令面契约快照（0.3.0 重构安全网）。

CLI 内核重构（拆分 `cli/__init__.py` 为命令包、引入 Runtime 注入）期间，
**用户可见的命令契约必须逐字节不变**：命令路径、参数名、选项拼写（长短名）、
flag/取值形态、必填性与默认值。这份快照就是那条"零变化"的守门人。

基线文件：`tests/snapshots/cli_commands.json`（首次生成后人工审查并提交）。
有意变更命令面时：

    .venv/bin/python -m tests.test_contract_snapshot --update

然后 `git diff tests/snapshots/cli_commands.json` **逐条审查**变更是否就是预期
（v0.3.1 起带生成入口；此前文档只写了"删除基线重跑"，但测试并不会自动重建——
照做只会得到 FileNotFoundError，已修正为显式命令）。

快照包含**数值范围**（`min` / `max`，来自 Click 的 IntRange/FloatRange）：
CLI 的参数越界行为是脚本化契约的一部分（v0.3.1 给全部时长/行数参数补了上限），
没有这条守卫，范围被谁悄悄改回去都不会有人知道。

**为什么需要它**：命令面是脚本化契约（README 命令表、shell 补全、用户脚本都
依赖它），而文件拆分/名字迁移这类重构**不会**让既有行为测试变红——它们只是
patch 目标失效或覆盖不到新位置。快照测试让"改坏了命令面"立刻可见。
"""

from __future__ import annotations

import json
from pathlib import Path

from frpsctl.cli import app
from frpsctl.cli.introspect import build_snapshot

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "cli_commands.json"

#: 重构前必须全部存在的命令路径（61 条概念路径中的叶子命令 + 关键分组）。
#: 快照相等已经覆盖它们，这里独立再钉一遍是防止"基线被整体误重生成"。
REQUIRED_PATHS = {
    "install",
    "init",
    "verify",
    "uninstall",
    "start",
    "stop",
    "restart",
    "status",
    "log",
    "doctor",
    "prune",
    "clients",
    "proxies",
    "traffic",
    "instances",
    "capabilities",
    "config get",
    "config set",
    "config unset",
    "config edit",
    "config list",
    "config diff",
    "config rollback",
    "service install",
    "service uninstall",
    "service status",
    "service logs",
    "plugin init",
    "plugin check",
    "plugin serve",
    "plugin user list",
    "plugin user set",
    "plugin user remove",
    "plugin audit tail",
    "plugin audit stats",
    "plugin config list",
    "plugin config set",
    "plugin service install",
    "plugin service start",
    "plugin service stop",
    "plugin service restart",
    "plugin service uninstall",
    "plugin service status",
    "web serve",
    "web service install",
    "web service start",
    "web service stop",
    "web service restart",
    "web service uninstall",
    "web service status",
    "web password set",
    "web password show",
    "web audit tail",
    "web audit stats",
}

#: 0.3.0 新增命令（实现后加入 REQUIRED_PATHS 并更新快照基线）；
#: 单独列出是让"还未做"与"做完了"在测试里有清晰边界。
PLANNED_IN_030: set[str] = set()  # 0.3.0 计划项已全部落地


#: 快照生成器已提炼到生产代码（v0.3.5 R1）：`frpsctl/cli/introspect.py` 同时服务
#: 本快照、Web"命令"视图的静态数据（生成物无 diff 守卫）与元数据覆盖守卫。
#: `build_snapshot()` 的输出形状与提炼前逐字节一致——基线文件未变动。


def test_command_surface_matches_snapshot() -> None:
    expected = json.loads(SNAPSHOT_PATH.read_text("utf-8"))
    actual = build_snapshot()
    assert set(actual) == set(expected), (
        "命令路径集合变化：新增 "
        f"{sorted(set(actual) - set(expected))}；消失 {sorted(set(expected) - set(actual))}"
    )
    for path in sorted(expected):
        assert actual[path] == expected[path], f"命令 {path!r} 的参数面发生变化"


def test_required_paths_present() -> None:
    actual = set(build_snapshot())
    missing = REQUIRED_PATHS - actual
    assert not missing, f"缺少必需命令：{sorted(missing)}"


def test_minimum_command_count() -> None:
    """命令面不允许悄悄缩水（重构期守卫）。"""
    actual = build_snapshot()
    assert len(actual) >= 52, f"命令路径过少：{len(actual)}"


def test_global_option_after_subcommand_is_hoisted() -> None:
    """`frpsctl status --json`（选项写在子命令之后）必须能解析。

    这锁住 `_AnywhereGroup.parse_args` 的行为在重构后仍然生效——它是
    "`frpsctl status --json` 与 `frpsctl --json status` 等价"的实现基础。
    用 `status`（无实例也能成功）真跑一次 JSON，而不是只看 `--help`
    （v0.3.0 review：`--help` 走的是 Click 的 eager 路径，测不到 hoist）。
    """
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["state"] == "STOPPED"
    assert "No such option" not in result.output


def main(argv: list[str] | None = None) -> int:
    """显式生成/更新基线：`python -m tests.test_contract_snapshot --update`。

    更新后必须 `git diff` 逐条审查——这份文件的意义就是"变更必须被看见"。
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--update"]:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(build_snapshot(), indent=2, sort_keys=True, ensure_ascii=False)
        SNAPSHOT_PATH.write_text(payload + "\n", "utf-8")
        print(f"已生成基线：{SNAPSHOT_PATH}")
        return 0
    print("用法：python -m tests.test_contract_snapshot --update")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
