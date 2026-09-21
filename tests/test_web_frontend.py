"""前端多模块静态守卫（v0.3.4：单文件 → ESM 同源多模块）。

拆分解决了"单文件 1865 行不可维护"，但把原守卫的假设全部推翻——本文件
按新形态重建四层防护：

| 层 | 守卫 | 失败时意味着什么 |
|----|------|-----------------|
| 白屏级 | 每个模块 `node --check`（ESM 语法） | 整站白屏而 CI 全绿 |
| 白屏级 | import 路径存在 + 被导入符号确有导出 + 无孤儿模块 | 模块 404 / `undefined` 调用 |
| 安全线 | 禁 innerHTML 家族 / 无外部域资源 / 无内联 style / 无 JS style 赋值 | XSS 入口或 CSP 拦截静默失效 |
| 一致性 | CSS 变量双向对齐 / 亮色覆盖全颜色 / `$("id")` ↔ HTML 双向 / 组件类有样式 | 主题与组件静默失效 |
| 布局 | index 引用的资源都在 static 目录、且目录里没有意外文件 | 漏打包 / 死文件 |

行为层由两层互补覆盖：纯逻辑模块（`lib/*`）由 `node --test`
（`tests/frontend/*.test.mjs`，**直接 import 生产模块**）断言；DOM 分支仍靠
HTTP 全路由测试 + 手工点验（记账于设计方案 §27）。

没有 node 的环境跳过语法/图检查（不是 fail）——CI 的 runner 有 node。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from frpsctl.web.server import STATIC_DIR, STATIC_INDEX

#: 危险 DOM API：能把字符串当 HTML 解析。本前端全程 textContent/createElement，
#: 一旦有人引入任何一个都应立刻失败。
_FORBIDDEN_JS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
)

#: v0.3.4 新增组件类（拆分后逐项点名，防"样式漏写 = 静默失效"）。
#: v0.3.5 追加命令参考视图（cmd-*、tag-*）的类。
_COMPONENT_CLASSES = (
    "hero", "metric", "skeleton", "empty", "modal-card", "toolbar",
    "kbd", "grid-line", "crosshair", "tip-box", "tip-text", "bar-hit",
    "view-anim", "login-logo", "grad-in-stop", "grad-out-stop", "ticon",
    "drawer", "drawer-head", "drawer-body", "cmdk-panel", "cmdk-item",
    "service-card", "service-head", "progress-bar", "progress-fill",
    "port-editor", "port-row", "donut-slice", "donut-total", "banner-action",
    "cmd-group", "cmd-item", "cmd-item-head", "cmd-item-body", "cmd-code",
    "cmd-badges", "cmd-buttons", "tag-ro", "tag-mut", "tag-root", "tag-sys",
    "login-head", "login-title", "login-scan", "login-label", "login-field",
    "pw-toggle", "login-caps", "login-meta", "login-shake",
    "overlay-card", "overlay-title", "overlay-scan",
    "cmdk-label", "cmdk-hint", "cmdk-empty", "group-title", "login-card",
)

#: 静态资源目录里的合法文件扩展名。
_ALLOWED_STATIC_SUFFIXES = {".html", ".js", ".css", ".svg", ".json", ".txt"}


def _index_html() -> str:
    return STATIC_INDEX.read_text("utf-8")


def _js_files() -> list[Path]:
    return sorted((STATIC_DIR / "js").rglob("*.js"))


def _css_files() -> list[Path]:
    return sorted((STATIC_DIR / "css").glob("*.css"))


def _js_text() -> str:
    return "\n".join(path.read_text("utf-8") for path in _js_files())


def _css_text() -> str:
    return "\n".join(path.read_text("utf-8") for path in _css_files())


# ---------------------------------------------------------------------------
# 白屏级：语法与模块图
# ---------------------------------------------------------------------------


class TestModuleSyntax:
    @pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 做 JS 语法检查")
    def test_all_modules_pass_node_check(self) -> None:
        """每个 ES 模块都必须通过 `node --check`（ESM 模式）。

        这是"全站白屏而 CI 全绿"的第一道防线：HTTP 层测试与模块图检查都
        发现不了一个少写的括号。
        """
        files = _js_files()
        assert len(files) >= 20, f"模块数异常（{len(files)}）——拆分布局或提取逻辑变了"
        for path in files:
            proc = subprocess.run(
                ["node", "--input-type=module", "--check"],
                input=path.read_text("utf-8"),
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert proc.returncode == 0, f"{path.name} 语法错误：\n{proc.stderr}"


class TestModuleGraph:
    """import 图：路径存在、符号有导出、无孤儿模块（node --check 都不查这些）。"""

    _IMPORT_RE = re.compile(
        r'import\s+(?:\{([^}]*)\}|(\w+))\s+from\s+"([^"]+)"', re.M
    )

    def _imports(self, path: Path) -> list[tuple[Path, list[str]]]:
        out: list[tuple[Path, list[str]]] = []
        for match in self._IMPORT_RE.finditer(path.read_text("utf-8")):
            names_raw, _default, target = match.groups()
            target_path = (path.parent / target).resolve()
            names = []
            for item in (names_raw or "").split(","):
                item = item.strip()
                if not item:
                    continue
                names.append(item.split(" as ")[0].strip())
            out.append((target_path, names))
        return out

    def test_import_targets_exist(self) -> None:
        for path in _js_files():
            for target, _names in self._imports(path):
                assert target.is_file(), f"{path.name} 导入了不存在的模块：{target}"

    def test_imported_symbols_are_exported(self) -> None:
        for path in _js_files():
            for target, names in self._imports(path):
                source = target.read_text("utf-8")
                for name in names:
                    pattern = (
                        rf"export\s+(?:async\s+)?(?:function|const|let|class)\s+"
                        rf"{re.escape(name)}(?=\s|=|;|\(|$)"
                    )
                    assert re.search(pattern, source, re.M), (
                        f"{path.name} 从 {target.name} 导入了未导出的符号：{name}"
                    )

    def test_no_orphan_modules(self) -> None:
        """每个模块都必须被 import（main.js 是唯一入口）——死文件在 CI 里暴露。"""
        entry = STATIC_DIR / "js" / "main.js"
        imported: set[Path] = set()
        for path in _js_files():
            for target, _names in self._imports(path):
                imported.add(target)
        for path in _js_files():
            if path == entry:
                continue
            assert path.resolve() in imported, f"孤儿模块（无人 import）：{path.name}"


# ---------------------------------------------------------------------------
# 安全线
# ---------------------------------------------------------------------------


class TestFrontendDiscipline:
    def test_no_dangerous_dom_apis(self) -> None:
        """禁用 innerHTML 家族：CSP 允许脚本，字符串拼 HTML 就是 XSS 入口。"""
        js = _js_text()
        found = [name for name in _FORBIDDEN_JS if name in js]
        assert not found, f"前端引入了危险 DOM API：{found}（请用 textContent / createElement）"

    def test_no_external_resources(self) -> None:
        """`src` / `href` 只允许 data:、同源绝对路径或锚点——零外部域是硬约束。"""
        html = _index_html()
        for match in re.finditer(r'(?:src|href)\s*=\s*"([^"]*)"', html):
            url = match.group(1)
            assert url.startswith(("data:", "/", "#")), f"外部资源引用：{url!r}"

    def test_no_inline_style_attributes(self) -> None:
        hits = re.findall(r'\sstyle="[^"]*"', _index_html())
        assert not hits, f"还有内联 style 属性（CSP 无 unsafe-inline）：{hits[:3]}"

    def test_no_js_style_attribute_assignments(self) -> None:
        hits = re.findall(r"style\s*:", _js_text())
        assert not hits, f"JS 里还有 style 属性赋值：{hits[:3]}（请改用 class / CSS 变量）"

    def test_no_js_style_property_writes(self) -> None:
        """禁止 `node.style.xxx =` 与 `setAttribute("style", …)`（同上）。"""
        js = _js_text()
        assert ".style." not in js, "发现 JS 直接写 style 属性（请改用 class）"
        assert "setAttribute(\"style\"" not in js, "发现 setAttribute('style')"

    def test_no_native_blocking_dialogs(self) -> None:
        """不再使用原生 confirm/alert/prompt（改自绘 modal / 命令面板）。"""
        js = _js_text()
        assert "confirm(" not in js, "仍有原生 confirm 调用"
        assert "alert(" not in js, "仍有原生 alert 调用"
        assert "confirmAsync(" in js

    def test_no_boolean_attr_shorthand_in_el(self) -> None:
        """布尔属性不得走 `el()` 的 attrs 简写（v0.3.4 的真实缺陷）。

        `el("button", {disabled: false})` → `setAttribute("disabled", "false")`，
        而 HTML 的布尔属性**只要存在就生效**——按钮会永远禁用、面板永远隐藏。
        正确写法是属性赋值（`btn.disabled = true`）。插件启停按钮曾因此完全
        不可用（2026-09-21 发现并修复于 views/services.js）。
        """
        js = _js_text()
        # 用正则而不是子串：`disabled : false`（冒号前有空格）此前能绕过；
        # 也不能误伤 `"aria-hidden": "true"`（布尔属性守卫不该管 ARIA 属性）。
        pattern = re.compile(
            r"(?<![\w-])(disabled|checked|hidden|readonly|required|selected)\s*[:,]"
        )
        hits = pattern.findall(js)
        assert not hits, (
            f"发现布尔属性简写 {sorted(set(hits))}——它在 el() 里会变成字符串属性而静默生效，"
            "请改用属性赋值（node.disabled = true）"
        )

    def test_dollar_helper_only_used_with_literal_ids_or_tables(self) -> None:
        """`$(...)` 的非字面量实参只允许出现在登记的数据表文件里。

        `$("id")` 才是能被 id 双向守卫核对（并能被正则抓到 typo）的形态；
        `$(someVariable)` 让正向检查完全失效。两处例外是"字面量数据表驱动"
        的用法（表本身仍被反向精确核对覆盖）：
        `router.js` 的 `VIEW_IDS` 与 `busy.js` 的按钮 id 数组。
        """
        allowed = {"router.js", "busy.js"}
        bad: list[str] = []
        for path in _js_files():
            if path.name in allowed:
                continue
            for match in re.finditer(r"\$\(([^)]*)\)", path.read_text("utf-8")):
                arg = match.group(1).strip()
                if not arg:
                    continue
                # 模板串带插值（`` `id-${x}` ``）同样是动态实参，必须拒绝
                dynamic = arg[0] not in "\"'`" or (arg[0] == "`" and "${" in arg)
                if dynamic:
                    bad.append(f"{path.name}: $({arg})")
        assert not bad, f"$() 只允许字面量实参：{bad}"

    def test_html_skeleton(self) -> None:
        html = _index_html()
        assert '<html lang="zh-CN">' in html
        assert 'name="viewport"' in html
        assert re.search(r'<link rel="icon" href="data:image/svg\+xml', html), "缺少内嵌 favicon"


class TestCspReadiness:
    def test_nonce_placeholders_present(self) -> None:
        """1 个内联主题脚本 + 5 个 CSS link + 1 个 module script = 7 处占位。"""
        html = _index_html()
        assert html.count("__CSP_NONCE__") == 7, "nonce 占位符数量不对"

    def test_module_entry_is_present(self) -> None:
        html = _index_html()
        assert '<script type="module" src="/static/js/main.js" nonce="__CSP_NONCE__">' in html

    def test_reduced_motion_and_aurora_present(self) -> None:
        """无障碍与视觉基线：尊重 reduced-motion；极光/渐变令牌已启用。"""
        css = _css_text()
        assert "prefers-reduced-motion" in css
        assert "radial-gradient" in css
        assert "--accent-grad" in css and "var(--accent-grad)" in css

    def test_component_classes_have_styles(self) -> None:
        """组件类都必须有样式定义（否则动效/布局静默失效）。"""
        css = _css_text()
        for cls in _COMPONENT_CLASSES:
            assert f".{cls}" in css, f"缺少 .{cls} 样式"

    def test_chart_elements_have_styles(self) -> None:
        """图表用到的 class 必须有样式（颜色走 CSS 变量，主题才能即时生效）。"""
        css = _css_text()
        for cls in ("bar-in", "bar-out", "line-in", "line-out", "axis"):
            assert f".{cls}" in css, f"图表 class .{cls} 没有样式定义（颜色会回退成黑色）"


# ---------------------------------------------------------------------------
# 一致性
# ---------------------------------------------------------------------------


class TestCssConsistency:
    def test_css_variables_are_defined_and_used(self) -> None:
        """`var(--x)` 与 `--x:` 双向对齐（主题切换依赖变量，拼错是静默失效）。

        v0.3.4 review 补强：双向检查会漏掉"删除默认主题定义但保留 `.light`
        定义"的情形（defined 集合仍含该变量）——默认主题下引用会静默回退。
        因此追加"每个被使用的变量必须在 `:root`（赛博暗，默认）里定义"。
        """
        css = _css_text()
        used = set(re.findall(r"var\(--([\w-]+)\)", css))
        defined = set(re.findall(r"--([\w-]+)\s*:", css))
        assert not (used - defined), f"使用了未定义的 CSS 变量：{sorted(used - defined)}"
        assert not (defined - used), f"定义了却从未使用的 CSS 变量：{sorted(defined - used)}"
        root = re.search(r":root\s*\{(.*?)\}", css, re.S)
        assert root is not None, "缺少 :root 默认主题定义"
        root_vars = set(re.findall(r"--([\w-]+)\s*:", root.group(1)))
        assert not (used - root_vars), f"默认主题缺少变量定义：{sorted(used - root_vars)}"

    def test_light_theme_overrides_every_colour_variable(self) -> None:
        """亮色主题必须覆盖暗色定义的全部**颜色**变量，否则切换后局部仍是暗色。

        几何量（圆角 / 切角）不参与主题化，是明确的例外集——新增几何变量
        必须在此登记，避免"忘了映射"与"本来就是几何量"混为一谈。
        """
        css = _css_text()
        root = re.search(r":root\s*\{(.*?)\}", css, re.S)
        light = re.search(r":root\.light\s*\{(.*?)\}", css, re.S)
        assert root is not None and light is not None, "缺少 :root / :root.light 定义"
        dark_vars = set(re.findall(r"--([\w-]+)\s*:", root.group(1)))
        light_vars = set(re.findall(r"--([\w-]+)\s*:", light.group(1)))
        geometric = {"radius", "clip-cut", "clip-cut-sm"}
        missing = dark_vars - light_vars - geometric
        assert not missing, f"亮色主题缺少变量：{sorted(missing)}"


class TestIdCrossCheck:
    def test_js_id_references_exist_in_html(self) -> None:
        """`$("id")` 引用的每个 id 都必须在 HTML 里定义——双向核对。

        这是 `node --check` 抓不到的白屏级错误：语法完全正确，运行时
        `null.addEventListener` 抛异常，整个页面停在 boot 之前。

        反向核对放宽为"id 必须出现在 JS 文本中"：视图/导航 id 以数据表
        （`VIEW_IDS`）形式声明、审计时间窗经 `querySelectorAll("#id …")`
        引用，严格 `$("id")` 正则覆盖不到这两种合法形态。
        """
        html = _index_html()
        js = _js_text()
        referenced = set(re.findall(r'\$\("([^"]+)"\)', js))
        defined = set(re.findall(r'\bid="([^"]+)"', html))
        assert not (referenced - defined), f"JS 引用了未定义的 id：{sorted(referenced - defined)}"
        # 反向必须是**精确形态**（字符串/选择器里的完整 id），不能用"子串包含"：
        # 子串会让 `nav-dashh`（typo）里的 `nav-dash` 也算命中，从而漏掉一处白屏级
        # typo（v0.3.5 review）。规则：id 前面是引号或 `#`，后面不是 id 字符。
        unused = [
            name
            for name in sorted(defined)
            if not re.search(rf'["\'`#]{re.escape(name)}(?![A-Za-z0-9_-])', js)
        ]
        assert not unused, f"HTML 里有从未被 JS 精确引用的 id：{unused}"


# ---------------------------------------------------------------------------
# 布局：静态目录与引用一致
# ---------------------------------------------------------------------------


class TestStaticLayout:
    def test_index_resources_exist(self) -> None:
        html = _index_html()
        refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
        assert len(refs) >= 6, f"静态资源引用异常：{refs}"
        for ref in refs:
            assert (STATIC_DIR / ref[len("/static/") :]).is_file(), f"index 引用了不存在的资源：{ref}"

    def test_no_unexpected_files_in_static(self) -> None:
        for path in STATIC_DIR.rglob("*"):
            if path.is_dir():
                continue
            assert path.suffix in _ALLOWED_STATIC_SUFFIXES, f"静态目录出现意外文件：{path}"

    def test_js_dir_is_declared_as_esm(self) -> None:
        """`js/package.json` 的 `"type": "module"` 是 Node 侧（CI 的 --check/--test）
        把 `.js` 当 ESM 解析的必要条件——删掉它 CI 立刻红（发布后发现并修复
        的教训）。浏览器不请求它（静态路由白名单拒绝 .json），因此对部署无影响。
        """
        import json as _json

        manifest = STATIC_DIR / "js" / "package.json"
        assert manifest.is_file(), "缺少 js/package.json（Node 将按 CJS 解析 .js）"
        assert _json.loads(manifest.read_text("utf-8"))["type"] == "module"

    def test_every_css_and_module_is_referenced_or_imported(self) -> None:
        """CSS 必须被 index 引用；JS 模块必须被 import（防漏挂）。"""
        html = _index_html()
        for css in _css_files():
            assert f"/static/css/{css.name}" in html, f"CSS 未被 index 引用：{css.name}"


class TestContrast:
    """WCAG 2.1 对比度（方案 W2 承诺 ≥ AA）——对令牌做**实测**而非声称。

    分档标准：正文/次要文本按 4.5:1（AA 文本），标题类主文本按 7:1（AAA），
    状态色（tag/dot/折线，承担非文本信息）按 3:1（AA 非文本）。
    背景取 `--bg` 与 `--panel2`（rgba 面板无法直接计算，用不透明近色近似）。
    """

    #: 状态色：非文本信息（对应 WCAG 1.4.11 非文本对比度）
    _STATUS = ("--accent", "--ok", "--warn", "--err")

    def _tokens(self, theme: str) -> dict[str, str]:
        css = (STATIC_DIR / "css" / "tokens.css").read_text("utf-8")
        if theme == "light":
            match = re.search(r":root\.light\s*\{(.*?)\}", css, re.S)
        else:
            match = re.search(r":root\s*\{(.*?)\}", css, re.S)
        assert match is not None, f"找不到 {theme} 的令牌块"
        return {
            f"--{name}": value.strip()
            for name, value in re.findall(r"--([\w-]+)\s*:\s*([^;]+);", match.group(1))
        }

    @staticmethod
    def _hex(value: str) -> str:
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"令牌不是 6 位 hex：{value!r}"
        return value

    @classmethod
    def _luminance(cls, color: str) -> float:
        raw = cls._hex(color).lstrip("#")
        channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    @classmethod
    def _ratio(cls, fg: str, bg: str) -> float:
        a, b = cls._luminance(fg), cls._luminance(bg)
        lighter, darker = max(a, b), min(a, b)
        return (lighter + 0.05) / (darker + 0.05)

    @pytest.mark.parametrize("theme", ["cyber", "light"])
    def test_body_text_contrast(self, theme: str) -> None:
        tokens = self._tokens(theme)
        for bg in ("--bg", "--panel2"):
            ratio = self._ratio(tokens["--text"], tokens[bg])
            assert ratio >= 7.0, f"{theme}: 主文本 on {bg} 仅 {ratio:.2f}:1"
            ratio = self._ratio(tokens["--muted"], tokens[bg])
            assert ratio >= 4.5, f"{theme}: 次要文本 on {bg} 仅 {ratio:.2f}:1"

    @pytest.mark.parametrize("theme", ["cyber", "light"])
    def test_status_colours_are_visible(self, theme: str) -> None:
        tokens = self._tokens(theme)
        for name in self._STATUS:
            ratio = self._ratio(tokens[name], tokens["--bg"])
            assert ratio >= 3.0, f"{theme}: {name} on bg 仅 {ratio:.2f}:1"
