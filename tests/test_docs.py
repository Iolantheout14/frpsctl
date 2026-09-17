"""文档一致性守卫（P3）：README / 设计文档与代码的交叉核对。

**为什么要有这一层**：README 承诺的"命令 / 环境变量 / 退出码"是用户与脚本的
直接接口；设计文档则记录着"哪些设计已落地"。v0.2.2 发布前的人工核对发现过
真实 drift（设计文档 §1.3 还写着已删除的 `kick`、§7.2 缺 6 个新命令）。
把那次核对固化成测试后，drift 会在 CI 里立刻暴露，而不是等下一轮 review。

守三条：README 命令引用、README 环境变量表、退出码表；外加设计文档的命令面
覆盖（§7.2 必须提到每个一级命令）与已删除命令的幽灵检查。
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.main import get_command

from frpsctl.cli import app
from frpsctl.errors import ExitCode

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"
DESIGN = ROOT / "frpsctl-设计方案.md"


def _command_paths() -> set[str]:
    """CLI 的全部命令路径（含子命令，如 `config set`）。"""
    command = get_command(app)
    paths: set[str] = set()

    def walk(group, prefix: str = "") -> None:
        for name, sub in group.commands.items():
            path = f"{prefix} {name}".strip()
            paths.add(path)
            if hasattr(sub, "commands"):
                walk(sub, path)

    walk(command)
    return paths


def _top_level_commands() -> set[str]:
    return {path.split()[0] for path in _command_paths()}


class TestReadmeConsistency:
    def test_every_readme_command_exists(self) -> None:
        """README 提到的每一条命令都必须在 CLI 里存在（防幽灵命令）。

        负向后顾排除路径形态（`/opt/frpsctl frpsctl install` 里的第一段是目录）。
        """
        text = README.read_text("utf-8")
        referenced = {
            m.group(1) for m in re.finditer(r"(?<![\w/])frpsctl[ \t]+([a-z][a-z-]*)", text)
        }
        unknown = referenced - _top_level_commands()
        assert not unknown, f"README 引用了不存在的命令：{sorted(unknown)}"

    def test_env_vars_match_both_ways(self) -> None:
        """README 环境变量表 vs 实际读取点——双向一致。

        读取点不止 Python：`install.sh` 也读 `FRPSCTL_INSTALL_REF / _URL`
        （在线安装的版本固定与镜像），必须一并纳入，否则文档如实写它们会被
        守卫误判成"幽灵环境变量"（v0.2.5 实测）。
        """
        code: set[str] = set()
        for path in (ROOT / "src").rglob("*.py"):
            code |= set(
                re.findall(
                    r'os\.environ(?:\.get)?\(?"?(FRPSCTL_[A-Z_]+)', path.read_text("utf-8")
                )
            )
        code |= set(re.findall(r"(FRPSCTL_[A-Z_]+)", (ROOT / "install.sh").read_text("utf-8")))
        documented = set(re.findall(r"\| `(FRPSCTL_[A-Z_]+)`", README.read_text("utf-8")))
        assert not (code - documented), f"README 环境变量表缺：{sorted(code - documented)}"
        assert not (documented - code), f"README 有幽灵环境变量：{sorted(documented - code)}"

    def test_exit_codes_match(self) -> None:
        """README 退出码表 vs `errors.ExitCode`——双向一致。"""
        documented = {
            int(m.group(1))
            for m in re.finditer(r"^\| (\d+) \|", README.read_text("utf-8"), re.MULTILINE)
        }
        actual = {int(code) for code in ExitCode}
        assert documented == actual, f"退出码表与 ExitCode 不一致：{sorted(documented ^ actual)}"


class TestDesignDocConsistency:
    def test_command_table_covers_every_top_level_command(self) -> None:
        """设计文档 §7.2 命令表必须覆盖全部一级命令。

        文档 drift 的典型形态是"新命令只在 README 里"——设计文档是事实基线的
        驻留地，命令面变了它必须跟着变。
        """
        design = DESIGN.read_text("utf-8")
        match = re.search(r"### 7\.2 命令表(.*?)### 7\.3", design, re.S)
        assert match is not None, "找不到设计文档 §7.2 命令表"
        section = match.group(1)
        missing = sorted(name for name in _top_level_commands() if name not in section)
        assert not missing, f"§7.2 命令表未覆盖：{missing}"

    def test_no_ghost_commands_in_design_doc(self) -> None:
        """已删除的命令不得再以"可执行形式"出现在设计文档里。

        `kick` 在 §18.6 作为"从未工作过的功能"的历史教训出现是**刻意的**，
        但 `frpsctl kick` 这种可被复制粘贴的形式必须绝迹。
        """
        design = DESIGN.read_text("utf-8")
        assert "frpsctl kick" not in design

    def test_subcommands_referenced_exist(self) -> None:
        """设计文档里 `frpsctl <组> <子命令>` 形式的引用必须真实存在。"""
        design = DESIGN.read_text("utf-8")
        paths = _command_paths()
        referenced = set(
            re.findall(r"(?<![\w/])frpsctl[ \t]+([a-z][a-z-]*)[ \t]+([a-z][a-z-]*)", design)
        )
        ghosts = sorted(f"{group} {sub}" for group, sub in referenced if f"{group} {sub}" not in paths)
        # 允许"位置参数"形态（如 `frpsctl config get <key>` 里的 get 已是子命令；
        # 而 `frpsctl plugin check` 等都在 paths 里）。剩下的都是真幽灵。
        assert not ghosts, f"设计文档引用了不存在的子命令：{ghosts}"


# ---------------------------------------------------------------------------
# Web API 表（§18.2）——v0.2.4 发现 `GET /api/config/history` 与 `/api/session`
# 长期只在代码里、API 表里没有。与命令表同一条纪律：接口面变了，文档必须跟。
# ---------------------------------------------------------------------------


def _api_code_routes() -> tuple[set[str], set[str]]:
    """从 web 层源码提取路由：`(精确路由, 前缀路由)`。

    路由写在 `path == "..."` / `path.startswith("...")` 里（server 层用
    `parsed.path`）。前缀路由以 `/api/actions/` 这类形态存在，文档里写作
    `` `/api/actions/{start,stop,...}` ``。
    """
    api_src = (ROOT / "src/frpsctl/web/api.py").read_text("utf-8")
    server_src = (ROOT / "src/frpsctl/web/server.py").read_text("utf-8")
    exact = set(re.findall(r'path == "(/api/[^"]+)"', api_src))
    exact |= set(re.findall(r'parsed\.path == "(/api/[^"]+)"', server_src))
    prefix = set(re.findall(r'path\.startswith\("(/api/[^"]+)"\)', api_src))
    return exact, prefix


def _doc_api_tokens() -> set[str]:
    """§18.2 表格里反引号包裹的 API 路径（query 部分去掉）。"""
    design = DESIGN.read_text("utf-8")
    match = re.search(r"### 18\.2 API 契约(.*?)### 18\.3", design, re.S)
    assert match is not None, "找不到设计文档 §18.2"
    tokens = set(re.findall(r"`(/api/[^`]+)`", match.group(1)))
    return {token.split("?")[0] for token in tokens}


def _prefix_matches(prefix: str, token: str) -> bool:
    """前缀路由（如 `/api/actions/`）能否解释文档里的花括号形态。"""
    return re.match(re.escape(prefix) + r"\{[^}]*\}", token) is not None


class TestApiDocConsistency:
    def test_code_routes_are_documented(self) -> None:
        """代码里的每条 API 路由都必须在 §18.2 表里出现（防"只加接口不改文档"）。"""
        exact, prefix = _api_code_routes()
        doc = _doc_api_tokens()
        missing = sorted(route for route in exact if route not in doc)
        assert not missing, f"§18.2 未记录的 API 路由：{missing}"
        for candidate in prefix:
            assert any(_prefix_matches(candidate, token) for token in doc), (
                f"§18.2 未记录前缀路由：{candidate}（应写作 `{candidate}{{...}}`）"
            )

    def test_documented_routes_exist_in_code(self) -> None:
        """§18.2 里的每个 API 路径都必须能在代码里找到（防幽灵接口）。"""
        exact, prefix = _api_code_routes()
        ghosts = sorted(
            token
            for token in _doc_api_tokens()
            if token not in exact and not any(_prefix_matches(prefix_route, token) for prefix_route in prefix)
        )
        assert not ghosts, f"§18.2 引用了不存在的 API 路由：{ghosts}"
