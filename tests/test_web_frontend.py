"""前端单文件的静态守卫（设计文档 §18）。

单文件零依赖是安全资产（CSP `default-src 'none'`、供应链面为零），但它有个
危险的副作用：**没有任何自动化在保护它**。后端测试与 Web 冒烟都只走 HTTP，
不执行 JS——所以一处语法错误会让整站白屏，而 CI 一路绿灯。

本文件把这条盲区变成 CI 断言，守住四件事：

| 守卫 | 失败时意味着什么 |
|------|-----------------|
| `node --check`（逐 script 块） | 白屏级语法错误 |
| 禁用 innerHTML 家族 | XSS 的唯一入口（CSP 允许内联脚本，纪律必须由代码保证） |
| 无外部资源引用 | 破坏"零外部依赖/可离线"，且会被 CSP 拦掉（静默失效） |
| `var(--x)` 与 `--x:` 双向对齐 | 主题/样式静默失效（拼错变量名不报错） |

没有 node 的环境跳过语法检查（不是 fail）——CI 的 runner 自带 node，守卫有效。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from frpsctl.web.server import STATIC_INDEX

#: 前端可能出现的"危险 DOM API"。它们能把字符串当 HTML 解析——本前端全程用
#: `textContent` 与 `createElement`，一旦有人引入这里任何一个都应立刻失败。
_FORBIDDEN_JS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
)


def _index_html() -> str:
    return STATIC_INDEX.read_text("utf-8")


def _script_blocks(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def _style_block(html: str) -> str:
    match = re.search(r"<style>(.*?)</style>", html, re.S)
    assert match is not None, "找不到 <style> 块"
    return match.group(1)


class TestFrontendSyntax:
    @pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 做 JS 语法检查")
    def test_script_blocks_pass_node_check(self, tmp_path: Path) -> None:
        """每个内联 script 块都必须通过 `node --check`。

        这是"全站白屏而 CI 全绿"的唯一防线：HTML 与 JSON API 的测试都不会发现
        一个少写的括号。
        """
        blocks = _script_blocks(_index_html())
        assert blocks, "index.html 里没有找到任何 <script> 块（提取逻辑或文件结构变了）"
        for index, script in enumerate(blocks):
            target = tmp_path / f"block{index}.js"
            target.write_text(script, "utf-8")
            proc = subprocess.run(
                ["node", "--check", str(target)], capture_output=True, text=True, timeout=30
            )
            assert proc.returncode == 0, f"script 块 {index} 语法错误：\n{proc.stderr}"


class TestFrontendDiscipline:
    def test_no_dangerous_dom_apis(self) -> None:
        """禁用 innerHTML 家族：CSP 允许内联脚本，字符串拼 HTML 就是 XSS 入口。"""
        html = _index_html()
        found = [name for name in _FORBIDDEN_JS if name in html]
        assert not found, f"前端引入了危险 DOM API：{found}（请用 textContent / createElement）"

    def test_no_external_resources(self) -> None:
        """`src` / `href` 只允许 data:、绝对路径或锚点——零外部资源是硬约束。"""
        html = _index_html()
        for match in re.finditer(r'(?:src|href)\s*=\s*"([^"]*)"', html):
            url = match.group(1)
            assert url.startswith(("data:", "/", "#")), f"外部资源引用：{url!r}"

    def test_css_variables_are_defined_and_used(self) -> None:
        """`var(--x)` 与 `--x:` 双向对齐（主题切换依赖变量，拼错是静默失效）。"""
        style = _style_block(_index_html())
        used = set(re.findall(r"var\(--([\w-]+)\)", style))
        defined = set(re.findall(r"--([\w-]+)\s*:", style))
        assert not (used - defined), f"使用了未定义的 CSS 变量：{sorted(used - defined)}"
        assert not (defined - used), f"定义了却从未使用的 CSS 变量：{sorted(defined - used)}"

    def test_light_theme_overrides_every_colour_variable(self) -> None:
        """亮色主题必须覆盖暗色定义的全部**颜色**变量，否则切换后局部仍是暗色。

        `--radius` 是几何量（圆角半径），不参与主题化，是唯一的例外。
        """
        style = _style_block(_index_html())
        root = re.search(r":root\s*\{(.*?)\}", style, re.S)
        light = re.search(r":root\.light\s*\{(.*?)\}", style, re.S)
        assert root is not None and light is not None, "缺少 :root / :root.light 定义"
        dark_vars = set(re.findall(r"--([\w-]+)\s*:", root.group(1)))
        light_vars = set(re.findall(r"--([\w-]+)\s*:", light.group(1)))
        missing = dark_vars - light_vars - {"radius"}
        assert not missing, f"亮色主题缺少变量：{sorted(missing)}"

    def test_chart_elements_have_styles(self) -> None:
        """图表用到的 class 必须有样式（颜色走 CSS 变量，主题才能即时生效）。"""
        style = _style_block(_index_html())
        for cls in ("bar-in", "bar-out", "line-in", "line-out", "axis"):
            assert f".{cls}" in style, f"图表 class .{cls} 没有样式定义（颜色会回退成黑色）"

    def test_html_skeleton(self) -> None:
        """基础结构：语言、viewport、图标（data URI）。"""
        html = _index_html()
        assert '<html lang="zh-CN">' in html
        assert 'name="viewport"' in html
        assert re.search(r'<link rel="icon" href="data:image/svg\+xml', html), "缺少内嵌 favicon"

    def test_js_id_references_exist_in_html(self) -> None:
        """`$("id")` 引用的每个 id 都必须在 HTML 里定义——**双向核对**。

        这是 `node --check` 抓不到的白屏级错误：语法完全正确，运行时
        `null.addEventListener` 抛异常，整个页面停在 boot 之前。

        反向也成立：本前端的 id **只作为 JS 钩子**存在（样式一律走 class），
        因此"定义了却没有引用"的 id 是重构残留，同样应该清理。
        """
        html = _index_html()
        js = "\n".join(_script_blocks(html))
        referenced = set(re.findall(r'\$\("([^"]+)"\)', js))
        defined = set(re.findall(r'\bid="([^"]+)"', html))
        assert not (referenced - defined), f"JS 引用了未定义的 id：{sorted(referenced - defined)}"
        assert not (defined - referenced), f"HTML 里有从未被引用的 id：{sorted(defined - referenced)}"


class TestFrontendPureFunctionsRuntime:
    """前端纯逻辑的**动态执行**守卫（node 执行从单文件里抽取的函数源码）。

    静态守卫（`node --check` / id 核对）抓不到"逻辑错了但语法正确"。此前对
    前端行为的验证全靠人工点验（v0.2.4 的 DOM stub 试验没有固化成测试），
    这里把纯函数的边界变成 CI 断言。配置页的数组值校验（`looksBalanced`）
    是 v0.2.6 新增启发式，属于此类。
    """

    @staticmethod
    def _extract(script: str, name: str) -> str:
        match = re.search(rf"function {name}\(.*?\n\}}", script, re.S)
        assert match is not None, f"找不到函数 {name}（重构后请同步本测试的抽取逻辑）"
        return match.group(0)

    def _run(self, tmp_path: Path, source: str, checks: list[str]) -> None:
        assert shutil.which("node") is not None, "需要 node"
        js = source + "\n" + "\n".join(checks) + "\nconsole.log('ok');\n"
        target = tmp_path / "check.js"
        target.write_text(js, "utf-8")
        proc = subprocess.run(
            ["node", str(target)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr

    def test_looks_balanced_boundaries(self, tmp_path: Path) -> None:
        script = "\n".join(_script_blocks(_index_html()))
        source = self._extract(script, "looksBalanced")
        import json as _json

        cases = [
            ("7000", True),
            ("true", True),
            ('"text"', True),
            ("[1, 2, 3]", True),
            ("[{ single = 6000 }, { start = 7000, end = 7100 }]", True),
            ('{ token = "with ] bracket" }', True),
            ("[1, 2", False),
            ("{ a = 1 ]", False),
            ('"unclosed', False),
            ("]", False),
            ("", True),
            ("   ", True),
        ]
        checks = [
            f"if (looksBalanced({_json.dumps(value)}) !== {str(expected).lower()}) "
            f"{{ console.error({_json.dumps(f'looksBalanced({value}) != {expected}')}); process.exit(1); }}"
            for value, expected in cases
        ]
        self._run(tmp_path, source, checks)

    def test_human_bytes_and_duration_boundaries(self, tmp_path: Path) -> None:
        script = "\n".join(_script_blocks(_index_html()))
        source = "\n".join(
            [self._extract(script, "humanBytes"), self._extract(script, "humanDuration")]
        )
        checks = [
            "if (humanBytes(0) !== '0 B') { console.error(humanBytes(0)); process.exit(1); }",
            "if (humanBytes(1024) !== '1.0 KiB') { console.error(humanBytes(1024)); process.exit(1); }",
            "if (humanBytes(null) !== '-') process.exit(1);",
            "if (humanDuration(90) !== '1m30s') { console.error(humanDuration(90)); process.exit(1); }",
            "if (humanDuration(3600) !== '1h0m') { console.error(humanDuration(3600)); process.exit(1); }",
            "if (humanDuration(null) !== '-') process.exit(1);",
        ]
        self._run(tmp_path, source, checks)
