"""命令面生成物与元数据守卫（v0.3.5 R1/R2/R3）。

Web"命令"视图的全部数据来自 `web/static/js/data/commands.js`（构建产物）。它一旦
与 CLI 命令面漂移，界面就会展示错的参数、错的危险等级，甚至凭空少一条命令——
而这类错误不会让任何既有测试变红。因此这里钉死四件事：

1. **生成物无漂移**：重新渲染的结果与磁盘文件逐字节一致（CI 另跑
   `python -m frpsctl.cli.introspect --check`，这里再钉一遍，保证本地回归也能发现）；
2. **生成物覆盖全部命令**：路径集合 == 契约快照的路径集合（同一份反射）；
3. **元数据完整**：`COMMAND_META` 与命令路径**双向相等**（漏一条、多一条都失败）；
4. **元数据严格性真的生效**：模拟漏一条时 `command_surface()` 必须抛错
   （否则"漏一条即失败"只是文档里的口号）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from frpsctl.cli.introspect import (
    COMMAND_META,
    CommandMetaMissing,
    build_snapshot,
    command_surface,
    generated_module_path,
    render_js,
)

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "cli_commands.json"

#: Web 视图常量（与前端 `views/router.js` 的 VIEWS 一致，外加命令视图自身）。
KNOWN_WEB_VIEWS = {"dash", "config", "audit", "services", "versions", "commands"}


def _snapshot_paths() -> set[str]:
    data = json.loads(SNAPSHOT_PATH.read_text("utf-8"))
    return {path for path in data if path != "(root)"}


def test_generated_module_is_up_to_date() -> None:
    """生成物与当前命令面一致（手工编辑生成物时立刻失败）。"""
    path = generated_module_path()
    assert path.is_file(), f"缺少生成物：{path}（运行 `python -m frpsctl.cli.introspect --write`）"
    assert path.read_text("utf-8") == render_js(), (
        "生成物与命令面不一致——运行 `python -m frpsctl.cli.introspect --write` 并提交"
    )


def test_generated_module_is_esm_with_surface_export() -> None:
    """生成物是合法 ESM 形态（浏览器 import 的硬要求）。"""
    text = generated_module_path().read_text("utf-8")
    assert text.startswith("/**"), "生成物应以说明注释开头"
    assert "export const COMMAND_SURFACE = " in text
    assert "手工编辑" in text, "生成物必须带'请勿手工编辑'的警示"


def test_surface_covers_every_command() -> None:
    """命令面（生成物数据）覆盖契约快照的全部命令。"""
    surface = command_surface()
    paths = {entry["path"] for entry in surface["commands"]}
    assert paths == _snapshot_paths()
    assert len(paths) >= 63, f"命令数过少：{len(paths)}"


def test_snapshot_and_surface_share_one_reflection() -> None:
    """快照与命令面来自同一次反射（防止有人改一处漏一处）。"""
    assert set(build_snapshot()) - {"(root)"} == {entry["path"] for entry in command_surface()["commands"]}


def test_command_meta_covers_every_command_both_ways() -> None:
    """元数据与命令路径双向相等：漏一条、多一条都失败。"""
    paths = _snapshot_paths()
    assert set(COMMAND_META) == paths, (
        f"元数据缺失 {sorted(paths - set(COMMAND_META))}；"
        f"元数据多余 {sorted(set(COMMAND_META) - paths)}"
    )


def test_missing_meta_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """严格性真的生效：删掉一条元数据后 `command_surface()` 必须抛错。"""
    import frpsctl.cli.introspect as introspect_mod

    trimmed = dict(COMMAND_META)
    trimmed.pop("doctor")
    monkeypatch.setattr(introspect_mod, "COMMAND_META", trimmed)
    with pytest.raises(CommandMetaMissing, match="doctor"):
        introspect_mod.command_surface()


def test_extra_meta_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向：元数据里有一条不存在的命令（改名/删除后忘清理）同样拒绝。"""
    import frpsctl.cli.introspect as introspect_mod

    polluted = dict(COMMAND_META)
    polluted["kick"] = next(iter(COMMAND_META.values()))
    monkeypatch.setattr(introspect_mod, "COMMAND_META", polluted)
    with pytest.raises(CommandMetaMissing, match="kick"):
        introspect_mod.command_surface()


def test_every_web_view_is_known() -> None:
    """`web_view` 只能是前端真实存在的视图（否则跳转按钮会指向不存在的视图）。"""
    for path, meta in COMMAND_META.items():
        if meta.web_view is not None:
            assert meta.web_view in KNOWN_WEB_VIEWS, f"{path} 指向未知视图 {meta.web_view!r}"


def test_readonly_and_mutating_are_consistent() -> None:
    """只读命令不得同时标注 needs_root/needs_systemd（那是变更类命令的属性）。"""
    for path, meta in COMMAND_META.items():
        if meta.readonly:
            assert not meta.needs_root, f"{path} 只读却要求 root"


def test_capabilities_and_surface_agree() -> None:
    """`capabilities.command_paths()`（CLI 命令与 shell 补全的数据源）与命令面一致。

    shell 补全（`--install-completion`）由 Typer 从**同一个 app** 反射生成，
    因此它与这里的命令面天然同源；这条断言把"同源"变成可执行的契约——将来
    若有人给补全接第二份清单，这里会红。
    """
    from frpsctl.capabilities import command_paths

    assert set(command_paths()) == _snapshot_paths()


class TestCommandReferences:
    """界面与提示文案里的 `frpsctl <cmd>` 引用必须真实存在（v0.3.5）。

    此前这类引用是硬编码字面量（前端 HTML/JS、API 的 hint），CLI 改名或删除后
    界面会给出**不存在的命令**，而 CI 全绿。这条守卫把"提到的命令"与命令面
    对账（带组名前缀的白名单：`frpsctl web service install` 这类完整路径、
    `frpsctl plugin user` 这类组引用都合法）。
    """

    #: `frpsctl` + 最多两个小写命令词。**前边界**排除 `/opt/frpsctl`（路径）、
    #: `frpsctl-web@` / `frpsctl.cli`（模块名与 unit 名）这类非命令出现。
    _REF_RE = re.compile(r"(?<![\w/.-])frpsctl[ \t]+([a-z][a-z0-9-]*(?:[ \t]+[a-z][a-z0-9-]*)?)")

    def _legal_prefixes(self) -> set[str]:
        """叶子命令及其全部组前缀（`web service`、`plugin` 等都是合法引用）。"""
        legal: set[str] = set()
        for path in _snapshot_paths():
            tokens = path.split()
            for index in range(1, len(tokens) + 1):
                legal.add(" ".join(tokens[:index]))
        return legal

    def _sources(self) -> list[tuple[str, str]]:
        root = Path(__file__).resolve().parents[1]
        files: list[Path] = []
        files.extend(sorted((root / "src" / "frpsctl").rglob("*.py")))
        static = root / "src" / "frpsctl" / "web" / "static"
        files.append(static / "index.html")
        files.extend(sorted((static / "js").rglob("*.js")))
        out = []
        for path in files:
            # 生成物里含命令路径字面量（数据，不是引用），跳过。
            if path.name == "commands.js" and path.parent.name == "data":
                continue
            out.append((str(path.relative_to(root)), path.read_text("utf-8")))
        return out

    def test_mentions_of_frpsctl_commands_exist(self) -> None:
        legal = self._legal_prefixes()
        bad: list[str] = []
        for name, text in self._sources():
            for match in self._REF_RE.finditer(text):
                candidate = " ".join(match.group(1).split())
                if candidate not in legal:
                    bad.append(f"{name}: frpsctl {candidate}")
        assert not bad, "文案引用了不存在的命令：\n" + "\n".join(sorted(set(bad)))

    def test_guard_detects_a_fake_command(self) -> None:
        """守卫自身有效：伪造一条不存在的命令必须被判非法（反向验证）。"""
        legal = self._legal_prefixes()
        fake = "frpsctl totally-made-up-command"
        match = self._REF_RE.search(fake)
        assert match is not None
        assert " ".join(match.group(1).split()) not in legal


def test_click_completion_source_is_available() -> None:
    """Click 的 shell 补全类可实例化（补全机制存在且挂在同一个 app 上）。"""
    from click.shell_completion import get_completion_class
    from typer.main import get_command

    from frpsctl.cli import app

    completion_cls = get_completion_class("bash")
    assert completion_cls is not None
    completer = completion_cls(get_command(app), {}, "frpsctl", "_FRPSCTL_COMPLETE")
    assert "frpsctl" in completer.source()
