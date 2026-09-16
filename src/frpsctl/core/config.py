"""配置读写与无损补丁（设计文档 §8.4、ADR-2）。

**核心不变量**：`config set` 之后，除目标键之外的**每一个字节**都保持不变——
注释、键序、行内注释、空行、缩进全部原样。因此：

| 职责 | 技术选择 |
|------|---------|
| 读 | `tomllib`（标准库，只读） |
| 写 | `tomlkit`（保留注释与格式） |
| 校验候选值 | `pydantic`（只校验，不承担序列化） |
| 权威判定 | 官方 `frps verify -c` |

若把配置读进内存模型再整体序列化写回，会丢掉两类东西：注释排版，以及模型
未覆盖的键（`allowPorts`、`httpPlugins`…）——后者等于**一次 `config set`
悄悄关掉了端口白名单**。因此写路径必须走 `plan_set` 的定点赋值。
"""

from __future__ import annotations

import contextlib
import difflib
import re
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from ..errors import (
    ConfigError,
    ConfigKeyMissing,
    ConfigRejected,
    FrpsctlError,
    TemplateSyntaxRejected,
    UsageError,
)

__all__ = [
    "ChangePlan",
    "atomic_write",
    "config_flags",
    "validate_candidate",
    "diff_texts",
    "ensure_table",
    "load_config",
    "plan_set",
    "reject_template_syntax",
    "validate_text",
    "mask_tree",
    "mask_diff",
    "SECRET_KEYS",
    "is_secret_key",
]

#: 敏感键：`config get` 默认打码，日志与 --json 绝不输出（§10 硬约束 2）。
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "webServer.password",
        "auth.token",
        "auth.oidc.clientSecret",
    }
)

_MISSING = object()

#: `frps verify` 的超时（秒）。配置校验是纯本地解析，正常在毫秒级完成；
#: 给 30 秒是防"二进制卡死"，而不是给它慢慢跑。
VERIFY_TIMEOUT = 30


def is_secret_key(dotted: str) -> bool:
    """判断一个点分键是否敏感。前后缀匹配以覆盖 `httpPlugins[].*` 之类。"""
    return dotted in SECRET_KEYS or dotted.endswith(".password") or dotted.endswith(".token")


def mask_tree(value: Any, *, prefix: str = "", reveal: bool = False) -> Any:
    """递归打码一棵配置子树。

    **为什么必须递归**：`config get auth` 拿到的是整张表，而
    `is_secret_key("auth")` 是 False —— 于是 `auth.token` 会明文打印出来。
    实测过：`frpsctl config get webServer` 直接输出 `password` 原值，
    人读与 `--json` 两种模式都泄。

    子树里哪些键敏感由**完整点分路径**决定（`auth.token`、`webServer.password`），
    因此递归时必须一路把 prefix 传下去。
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            out[key] = mask_tree(item, prefix=child, reveal=reveal)
        return out
    if isinstance(value, list):
        return [mask_tree(item, prefix=prefix, reveal=reveal) for item in value]
    if not reveal and prefix and is_secret_key(prefix):
        from ..cli.ui import mask_secret

        return mask_secret(value)
    return value


#: diff 里的敏感键赋值。**必须容忍行首的 `-`/`+` 标记**——否则 `-token = "..."`
#: 匹配不上，打码静默失效（实测踩过）。首组同时捕获标记、缩进、键名、等号。
_ASSIGN_RE = re.compile(r"^([-+ ]?)(\s*)([A-Za-z_][\w.-]*)(\s*=\s*)(.*)$")


def mask_diff(diff: str) -> str:
    """把 unified diff 里敏感键的值打码。

    `config diff` / `config rollback` / `config set` 都会展示 diff，而 diff 的
    内容就是配置原文——里面必然包含 `token = "..."` 这样的行。不给它打码，
    前面所有"机密不进日志/不进 --json"的努力都会从这一个出口漏光。
    """
    from ..cli.ui import mask_secret

    out_lines: list[str] = []
    current_table = ""
    for line in diff.splitlines():
        stripped = line.lstrip("+-")
        table_match = re.match(r"^\s*\[([^\]]+)\]", stripped)
        if table_match:
            current_table = table_match.group(1).strip()
            out_lines.append(line)
            continue
        match = _ASSIGN_RE.match(line)
        if not match:
            out_lines.append(line)
            continue
        marker, indent, key, eq, raw = match.groups()
        prefix = f"{current_table}.{key}" if current_table else key
        if not is_secret_key(prefix):
            out_lines.append(line)
            continue
        # 保留引号形态，只替换值
        quote = '"' if raw.lstrip().startswith('"') else ""
        masked = mask_secret(raw.strip().strip('"'))
        out_lines.append(f"{marker}{indent}{key}{eq}{quote}{masked}{quote}")
    return "\n".join(out_lines) + ("\n" if diff.endswith("\n") else "")


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------


def atomic_write(path: Path, text: str, *, mode: int = 0o600) -> None:
    """临时文件 + fsync + `os.replace`：磁盘上的目标文件任何时刻都是完整的。

    `os.fchmod` **先于**内容写入：否则会出现一个短暂的、权限过宽的窗口，
    而这期间文件里可能已经有 token（§10）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        # 用 with 包住 fdopen：fchmod 抛异常时（不支持 chmod 的文件系统、EPERM）
        # 由它负责关闭 fd。此前 fchmod 在 fdopen 之外，异常路径只 unlink 不 close，
        # 会留下一个指向已删除文件的打开 fd（实测新增 fd 未释放）。
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            os.fchmod(handle.fileno(), mode)  # 先定权限再写内容，无权限窗口
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def load_config(path: Path) -> tomlkit.TOMLDocument:
    """解析 TOML。文件缺失或语法错误都转成 `ConfigError` 系异常。"""
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"配置文件不存在：{path}",
            hint="先运行 `frpsctl init` 生成，或用 --config 指定路径",
        ) from None
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from None
    try:
        return tomlkit.parse(text)
    except Exception as exc:  # tomlkit 抛的是自己的 ParseError
        raise ConfigError(f"配置文件 TOML 语法错误：{exc}") from None


def get_value(doc: tomlkit.TOMLDocument, dotted: str) -> Any:
    """按点分路径取值；缺失抛 `ConfigKeyMissing`。"""
    node: Any = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigKeyMissing(dotted)
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# 写（定点补丁）
# ---------------------------------------------------------------------------


def ensure_table(doc: Any, *path: str) -> Any:
    """逐级补建表。

    `tomlkit` 对不存在的键赋值会抛 `NonExistentKey`，所以必须显式建表；
    `is_super_table=True` 用于生成隐式父表，避免产出多余的空表头。
    """
    node = doc
    for i, key in enumerate(path):
        current = node.get(key)
        if current is None:
            current = tomlkit.table(is_super_table=(i < len(path) - 1))
            node[key] = current
        node = current
    return node


def reject_template_syntax(value: Any, *, dotted: str = "") -> None:
    """拒绝含 `{{` 的字符串（§3.1 推论 2）。

    frp 在解析配置前会做 Go `text/template` 渲染，写进配置的 `{{ .Envs.X }}`
    会被求值——对使用者而言是"我写的东西被改写了"，因此必须拒绝而不是放行。
    递归检查数组与内联表，因为它们同样会被渲染。
    """
    if isinstance(value, str):
        if "{{" in value:
            raise TemplateSyntaxRejected(dotted or "值")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            reject_template_syntax(item, dotted=dotted or str(key))
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            reject_template_syntax(item, dotted=dotted)


def parse_scalar(raw: str) -> Any:
    """把命令行字符串转成 TOML 标量。

    只做**保守**转换：bool / int / float / 内联数组 / 内联表 / 字符串。
    不做通配求值，也不接受裸的中文标点——写错就报错（ADR-7）。
    """
    text = raw.strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if text and (text[0] in "[{" or text[0] in "\"'"):
        try:
            parsed = tomlkit.parse(f"v = {text}")["v"]
        except Exception as exc:
            raise UsageError(f"无法解析值 {raw!r}：{exc}") from None
        # tomlkit 会返回自己的包装类型，转成纯 Python 值便于后续处理
        return parsed.unwrap() if hasattr(parsed, "unwrap") else parsed
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return raw


@dataclass(frozen=True)
class ChangePlan:
    """一次待落盘的变更：内存补丁的结果，**线上文件此刻未被触碰**。"""

    dotted: str
    before: Any
    after: Any
    text: str
    diff: str

    @property
    def is_noop(self) -> bool:
        return self.before is not _MISSING and self.before == self.after


def plan_set(path: Path, dotted: str, raw: str) -> ChangePlan:
    """只做内存补丁，不落盘（§8.4）。

    步骤：解析候选值 → **语义校验候选值** → 模板语法检查 → 定点赋值 →
    生成新文本与 diff。任何一步失败都在这里抛错，线上配置零影响（§9 第 3 步）。

    第 3 步（pydantic 语义校验）在这里做而不是留到整体校验：它给出的错误信息
    比 frps verify 更好读（"bindPort 必须是 1..65535 的整数"），而且能在**根本
    没碰文件之前**就失败。
    """
    if not dotted or dotted.startswith(".") or dotted.endswith("."):
        raise UsageError(f"非法键名：{dotted!r}", hint="使用点分路径，如 transport.tls.force")

    doc = load_config(path)
    parts = dotted.split(".")
    value = parse_scalar(raw)
    reject_template_syntax(value, dotted=dotted)
    validate_candidate(parts, value)

    table = ensure_table(doc, *parts[:-1])
    before = table.get(parts[-1], _MISSING)
    table[parts[-1]] = value

    text = tomlkit.dumps(doc)
    return ChangePlan(
        dotted=dotted,
        before=None if before is _MISSING else before,
        after=value,
        text=text,
        diff=diff_texts(path.read_text("utf-8"), text, path.name),
    )


def validate_candidate(parts: list[str], value: Any) -> None:
    """把"单个键的候选值"塞进空白文档做一次语义校验。

    这样做的好处是**不需要**为校验单独维护一套"键 → 类型"映射：pydantic 模型
    本身就是那份映射，把候选值放进正确的位置就能复用它的类型与范围检查。

    未知键（模型未覆盖的）会被 `extra="allow"` 放行——那是对的，键名合法性
    本来就归 `frps verify --strict_config=true` 管（§8.1 推论 1）。
    """
    from .schema import validate_mapping

    nested: Any = value
    for part in reversed(parts):
        nested = {part: nested}
    validate_mapping(nested)


# ---------------------------------------------------------------------------
# 权威校验
# ---------------------------------------------------------------------------


def config_flags(*, uses_exec_token_source: bool = False) -> list[str]:
    """构造 frps 标志，**必须放在子命令之前**（Cobra 持久标志）。

    **刻意不接收版本号**：版本门槛（§3.6）在 `install` 阶段就把 `< 0.70.0`
    拒之门外，因此运行时不存在"这个版本认不认这个标志"的分支。早先的设计里
    确有版本矩阵（0.52–0.65 无 `--allow-unsafe`、strict 默认 false），但门槛
    上收之后那段分支已成为死代码——留一个被忽略的 `version` 参数只会让人
    以为这里还在按版本判断。

    `--strict_config=true` 仍**显式传**：0.70+ 上它默认已是 true，但这个默认值
    在 v0.53 → v0.66 之间变过一次（false → true）。依赖"某个版本的默认值"是
    脆弱的，而显式传值只花一个字节，却把"键名写错必须硬报错"这条护栏钉死。

    `--allow-unsafe` 仅在配置使用 `auth.tokenSource` 的 exec 源时追加，
    且它是 `StringSlice` 而非布尔开关（附录 B-2 复核纪律 2）。
    """
    flags: list[str] = ["--strict_config=true"]
    if uses_exec_token_source:
        flags.extend(["--allow-unsafe", "TokenSourceExec"])
    return flags


def uses_exec_token_source(doc: Any) -> bool:
    """配置是否用了会触发 `--allow-unsafe` 的 exec token 源。"""
    try:
        source = get_value(doc, "auth.tokenSource")
    except FrpsctlError:
        return False
    if not isinstance(source, dict):
        return False
    return str(source.get("type", "")).lower() == "exec"


def validate_text(
    text: str,
    *,
    binary: Path,
    workdir: Path,
    uses_unsafe: bool = False,
) -> None:
    """双保险校验：pydantic 语义 + 官方 `frps verify` 真机校验（§9 第 3、5 步）。

    pydantic 提供**更快、更好读**的错误信息，但**不下最终结论**；
    `frps verify` 是唯一权威判定。用临时副本，绝不动线上文件。
    """
    # 第一层：本地语义校验（快速失败，错误信息友好）
    validate_semantics(text)

    # 第二层：官方权威判定
    workdir.mkdir(parents=True, exist_ok=True)
    # 先把路径拿到手再进 try：候选文件里可能有 webServer.password / auth.token，
    # 写入中途失败（ENOSPC/EIO）时若不清理，就会把一份含机密的文件留在实例目录里。
    # 刻意不用 with 包住创建：路径必须在 try 之前拿到，否则写失败时 finally 不生效。
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        "w", suffix=".toml", dir=workdir, delete=False, encoding="utf-8"
    )
    candidate = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        try:
            argv = [
                str(binary),
                *config_flags(uses_exec_token_source=uses_unsafe),
                "verify",
                "-c",
                str(candidate),
            ]
            from ..cli.ui import trace

            trace(f"执行权威校验：{' '.join(argv)}")
            proc = subprocess.run(
                argv,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=VERIFY_TIMEOUT,
            )
            trace(f"verify 退出码 {proc.returncode}")
        except subprocess.TimeoutExpired:
            # 必须接住：让它冒出去只会变成"未分类错误"，而真正该说的是
            # "校验没能在 N 秒内完成"——用户据此判断是二进制卡住了还是机器太慢。
            raise ConfigError(
                f"配置校验超时（{VERIFY_TIMEOUT} 秒）：{binary} verify 没有返回",
                hint="该二进制可能卡住或不可执行；可先手工运行它确认真实行为",
            ) from None
        except OSError as exc:
            raise ConfigError(f"无法执行配置校验：{exc}") from None
    finally:
        candidate.unlink(missing_ok=True)
    if proc.returncode != 0:
        detail = (proc.stdout or proc.stderr).strip() or f"退出码 {proc.returncode}"
        raise ConfigRejected(detail)


def validate_semantics(text: str) -> None:
    """本地语义校验（pydantic 模型）。

    只做**范围与类型**这类能给出友好信息的检查；键名合法性交给
    `--strict_config=true`，端口可用性交给 `doctor`——各管一段，不重复。
    """
    from .schema import validate_document

    validate_document(text)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def diff_texts(before: str, after: str, filename: str = "frps.toml") -> str:
    """unified diff。变更闭环第 9 步与 `config diff` 都用它。"""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    return "".join(lines)
