"""文档一致性检查（README ↔ 代码同源，0.3.0）。

三张表过去靠手工同步，而文档审计确认它们真的漂移过（新增命令/退出码/环境
变量时最容易漏）。这里把"代码是唯一真相"做成**可执行的对账**：

| 表 | 派生自 | 检查 |
|----|--------|------|
| 命令 | `capabilities.command_paths()` | 每条命令都能在 README 里找到（全文或表格行的组内简写） |
| 退出码 | `errors.ExitCode` | README 退出码表的码集合与枚举**完全一致** |
| 环境变量 | `env.ENV_VARS` | 双向：README 提到的有定义、定义的都被 README 提到 |

CI 由 `tests/test_docs.py` 调用；本地可 `python -m frpsctl.docs check`。
**不改写 README**：人工可读的富格式（分类、典型触发）保留，检查只保证不漂移。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .capabilities import command_paths, exit_codes
from .env import ENV_VARS

__all__ = [
    "command_problems",
    "env_var_problems",
    "exit_code_problems",
    "check_all",
]

_ENV_NAME_RE = re.compile(r"\b(FRPSCTL_[A-Z_]+|XDG_DATA_HOME|EDITOR)\b")


def _section(text: str, heading: str) -> str:
    """取 `### <heading>` 到下一个 `###` 之间的内容（不存在返回空串）。"""
    start = text.find(f"### {heading}")
    if start < 0:
        return ""
    rest = text[start + len(heading) + 4 :]
    end = rest.find("\n### ")
    return rest if end < 0 else rest[:end]


def _command_documented(readme: str, path: str) -> bool:
    """命令是否被 README 提到。

    完整路径直接出现即可；多段命令也接受"组内简写"（分类表把组前缀只写一次，
    例如 `config get` / `set` / `unset` 的写法）。判据是**同一行**内同时出现
    组名与动作名——表格行的分组前缀因此只需出现一次。
    """
    if path in readme:
        return True
    parts = path.split()
    if len(parts) < 2:
        return False
    group, action = parts[0], parts[-1]
    pattern = rf"\b{re.escape(group)}\b[^\n]*\b{re.escape(action)}\b"
    return re.search(pattern, readme) is not None


def command_problems(readme: str) -> list[str]:
    """README 未覆盖的命令（新增命令忘写文档会在这里暴露）。"""
    missing = [path for path in command_paths() if not _command_documented(readme, path)]
    return [f"命令未写入 README：{path}" for path in missing]


def exit_code_problems(readme: str) -> list[str]:
    """README 退出码表与 `ExitCode` 枚举的对账。"""
    block = _section(readme, "退出码（脚本化契约）")
    if not block:
        return ["README 缺少『退出码（脚本化契约）』章节"]
    rows = set(re.findall(r"^\|\s*(\d+)\s*\|", block, re.M))
    documented = {int(value) for value in rows}
    actual = set(exit_codes().values())
    problems: list[str] = []
    for code in sorted(actual - documented):
        problems.append(f"退出码 {code} 未写入 README 退出码表")
    for code in sorted(documented - actual):
        problems.append(f"README 退出码表有未定义的值：{code}")
    return problems


def env_var_problems(readme: str) -> list[str]:
    """环境变量的双向对账（README ↔ `env.ENV_VARS`）。"""
    problems: list[str] = []
    for name in ENV_VARS:
        if name not in readme:
            problems.append(f"环境变量 {name} 未写入 README")
    block = _section(readme, "环境变量与全局选项")
    if not block:
        return [*problems, "README 缺少『环境变量与全局选项』章节"]
    for name in sorted(set(_ENV_NAME_RE.findall(block))):
        if name not in ENV_VARS:
            problems.append(f"README 环境变量表提到了未定义的变量：{name}")
    return problems


def check_all(readme: str) -> list[str]:
    """全部对账问题（空列表 = 一致）。"""
    return [*command_problems(readme), *exit_code_problems(readme), *env_var_problems(readme)]


def main(argv: list[str] | None = None) -> int:
    """`python -m frpsctl.docs check [README 路径]`。"""
    from .core.diagnostics import configure_streams

    configure_streams()  # 中文输出与 locale 无关（与 CLI 入口同纪律）
    args = list(sys.argv[1:] if argv is None else argv)
    readme_path = Path(args[1]) if len(args) > 1 else Path("README.md")
    if args and args[0] != "check":
        sys.stderr.write("用法：python -m frpsctl.docs check [README.md]\n")
        return 2
    if not readme_path.exists():
        sys.stderr.write(f"找不到 {readme_path}\n")
        return 2
    problems = check_all(readme_path.read_text("utf-8"))
    if not problems:
        print("文档对账通过：命令 / 退出码 / 环境变量与代码一致")
        return 0
    for item in problems:
        print(f"✗ {item}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
