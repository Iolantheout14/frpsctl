"""install.sh 的测试（设计文档 §12、§22）。

shell 脚本没有单元测试框架，但它有三类能被守住的东西：

1. **语法**：`bash -n`——脚本在用户机器上"第一次运行就崩"是不可接受的；
2. **行为**：`--uninstall` 不依赖 Python；完全无 Python 时打印 uv 一键指引
   （这两条都真跑脚本，不联网、秒级）；
3. **承诺一致性**：帮助文本与 README 里宣传的在线一键命令必须真实存在于脚本
   与文档——"文档说了但脚本没有"是最典型的 drift。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
INSTALL = ROOT / "install.sh"
README = ROOT / "README.md"

#: 无 Python 场景的测试里，脚本运行需要的基本工具（其余都走 bash 内建）。
_FAKE_PATH_TOOLS = ("uname", "id", "dirname", "mktemp", "cat", "tar", "rm", "mkdir", "ls")


def _bash() -> str:
    path = shutil.which("bash")
    if path is None:
        pytest.skip("没有 bash：install.sh 的测试需要它")
    return path


def _run(*args: str, env: dict | None = None, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_bash(), str(INSTALL), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


class TestInstallerSyntax:
    def test_bash_syntax_check(self) -> None:
        proc = subprocess.run(
            [_bash(), "-n", str(INSTALL)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr


class TestInstallerBehaviour:
    def test_uninstall_does_not_require_python(self, tmp_path: Path) -> None:
        """`--uninstall --prefix DIR` 必须走卸载路径。

        历史缺陷：MODE 与安装布局挤在同一个变量里，`--prefix` 会把
        `--uninstall` 覆盖成 custom → 卸载命令实际去跑安装流程（并因
        Python 检查失败）——"装到自定义前缀后想卸载"这个最常见的组合是坏的。
        这里真跑脚本（前缀为空 → 输出"没找到"并正常退出，不触碰 Python）。
        """
        proc = _run("--uninstall", "--prefix", str(tmp_path))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "卸载完成" in proc.stdout
        assert "Python" not in proc.stdout

    def test_missing_python_prints_uv_guide(self, tmp_path: Path) -> None:
        """完全没有 Python 时：打印 uv 一键命令，而不是只报一句错。"""
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir()
        for name in _FAKE_PATH_TOOLS:
            source = shutil.which(name)
            assert source is not None, f"测试环境缺少基本工具：{name}"
            (fakebin / name).symlink_to(source)

        proc = _run("--prefix", str(tmp_path / "prefix"), env={"PATH": str(fakebin)})
        assert proc.returncode != 0
        combined = proc.stdout + proc.stderr
        assert "astral.sh/uv" in combined, combined
        assert "没有 Python" in combined or "找不到 Python" in combined, combined


class TestInstallerPromises:
    def test_help_documents_pipe_install_and_env(self) -> None:
        proc = _run("--help")
        assert proc.returncode == 0
        assert "raw.githubusercontent.com" in proc.stdout, "帮助里没有管道安装用法"
        assert "FRPSCTL_INSTALL_REF" in proc.stdout
        assert "FRPSCTL_INSTALL_URL" in proc.stdout

    def test_readme_one_liners_are_real(self) -> None:
        """README 宣传的两条一键命令必须与脚本/现实一致。"""
        readme = README.read_text("utf-8")
        assert "astral.sh/uv/install.sh" in readme, "README 缺 uv 一键命令"
        url = "raw.githubusercontent.com/ThzxxArt/frpsctl/main/install.sh"
        assert url in readme, "README 缺管道安装命令"
        assert url in INSTALL.read_text("utf-8"), "帮助文本与 README 的 URL 不一致"

    def test_prefix_option_never_touches_mode(self) -> None:
        """静态守卫：解析 `--prefix` 的分支不得修改 MODE（历史缺陷的根因）。"""
        for line in INSTALL.read_text("utf-8").splitlines():
            if "--prefix)" in line or "--prefix=*)" in line:
                assert "MODE=" not in line, f"--prefix 分支动了 MODE：{line!r}"
