"""统一异常 → 退出码映射（设计文档 §7.3）。

这是"脚本化契约"的唯一出口：所有面向用户的失败都必须落在这张表里。
`core/` 只抛异常，`cli/` 只负责把异常翻译成退出码与人类可读消息。

约定：**异常消息里绝不出现口令与 token**（§10 硬约束 2）。
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "ExitCode",
    "FrpsctlError",
    "UsageError",
    "ConfigError",
    "ConfigKeyMissing",
    "ConfigRejected",
    "TemplateSyntaxRejected",
    "BinaryError",
    "BinaryNotFound",
    "UnsupportedVersion",
    "VersionParseError",
    "ChecksumUnavailable",
    "ChecksumMismatch",
    "NotRunning",
    "AlreadyRunning",
    "AdminUnreachable",
    "ApiVersionMismatch",
    "PermissionRequired",
    "ChangeRolledBack",
    "StartupFailed",
    "StopFailed",
    "OwnershipConflict",
    "LockBusy",
    "UnsupportedPlatform",
]


class ExitCode(IntEnum):
    """退出码表（§7.3）。改动即破坏兼容，新增需同步文档。"""

    OK = 0
    UNCLASSIFIED = 1
    USAGE = 2
    CONFIG_INVALID = 3
    BINARY = 4
    NOT_RUNNING = 5
    ALREADY_RUNNING = 6
    ADMIN_UNREACHABLE = 7
    PERMISSION = 8
    ROLLED_BACK = 9
    STARTUP_FAILED = 10
    OWNERSHIP_CONFLICT = 11


class FrpsctlError(Exception):
    """所有 frpsctl 异常的基类：自带退出码与可选处置提示。"""

    exit_code: ExitCode = ExitCode.UNCLASSIFIED

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        """CLI 展示用：主消息 + 可选的一行指引。"""
        return f"{self.message}\n提示：{self.hint}" if self.hint else self.message


# --- 用法 / 参数（2） ---------------------------------------------------


class UsageError(FrpsctlError):
    exit_code = ExitCode.USAGE


# --- 配置（3） ----------------------------------------------------------


class ConfigError(FrpsctlError):
    exit_code = ExitCode.CONFIG_INVALID


class ConfigKeyMissing(ConfigError):
    def __init__(self, dotted: str) -> None:
        super().__init__(f"配置中没有键：{dotted}")


class ConfigRejected(ConfigError):
    """官方 `frps verify` 拒绝（唯一权威判定，§9 第 5 步）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(f"配置被 frps verify 拒绝：{detail}")


class TemplateSyntaxRejected(ConfigError):
    """值含 `{{`，会被 frp 的 text/template 渲染（§3.1 推论 2）。"""

    def __init__(self, dotted: str) -> None:
        super().__init__(
            f"拒绝写入 {dotted}：值含模板语法 '{{{{'，frp 会对其进行 text/template 渲染",
            hint="如确需写入花括号，请手工编辑配置文件并自行承担渲染后果",
        )


# --- 二进制 / 版本（4） -------------------------------------------------


class BinaryError(FrpsctlError):
    exit_code = ExitCode.BINARY


class BinaryNotFound(BinaryError):
    def __init__(self, path: object) -> None:
        super().__init__(
            f"找不到 frps 二进制：{path}",
            hint="先运行 `frpsctl install` 获取并校验官方二进制",
        )


class UnsupportedVersion(BinaryError):
    """版本门槛（§3.6）：只接受 >= 0.70.0。"""

    def __init__(self, version: tuple[int, int, int] | str, minimum: str = "0.70.0") -> None:
        shown = ".".join(str(p) for p in version) if isinstance(version, tuple) else version
        super().__init__(
            f"不支持的 frps 版本：{shown}（最低支持 {minimum}）",
            hint=(
                "低于 0.70.0 的版本没有 v2 Admin API，且存在已知安全问题；"
                "请运行 `frpsctl install` 安装达标版本"
            ),
        )


class VersionParseError(BinaryError):
    def __init__(self, raw: str) -> None:
        super().__init__(
            f"无法从输出中解析 frps 版本号：{raw!r}",
            hint="该二进制可能不是官方 frps，请用 --binary 指向正确的可执行文件",
        )


class ChecksumUnavailable(BinaryError):
    """ADR-7：拿不到校验和就拒绝安装，而不是"先装上再说"。"""

    def __init__(self, version: str) -> None:
        super().__init__(
            f"拿不到 frps {version} 的官方校验和，拒绝安装",
            hint="可用 --insecure 显式跳过校验（风险自负），或检查网络与镜像配置",
        )


class ChecksumMismatch(BinaryError):
    def __init__(self, asset: str, expected: str, actual: str) -> None:
        super().__init__(
            f"{asset} 校验和不匹配：期望 {expected[:16]}…，实际 {actual[:16]}…",
            hint="下载内容与官方校验和不符，已丢弃，绝不落盘（可能存在投毒或传输损坏）",
        )


# --- 生命周期（5 / 6 / 10 / 11） ---------------------------------------


class NotRunning(FrpsctlError):
    exit_code = ExitCode.NOT_RUNNING

    def __init__(self, instance: str) -> None:
        super().__init__(f"实例 {instance} 当前未运行")


class AlreadyRunning(FrpsctlError):
    exit_code = ExitCode.ALREADY_RUNNING

    def __init__(self, pid: int) -> None:
        super().__init__(f"实例已在运行（pid {pid}）")


class StartupFailed(FrpsctlError):
    """启动即退出：端口被占、证书缺失、配置语义错误（§8.3）。

    `detail` 是 frp 的原始报错（回显给用户，G6）；`hint` 用于覆盖默认的
    "见 startup 日志"——例如停止流程里"SIGKILL 后仍未退出"需要不同的指引。
    """

    exit_code = ExitCode.STARTUP_FAILED

    def __init__(self, detail: str = "", *, hint: str | None = None) -> None:
        super().__init__(
            "frps 启动后立即退出",
            hint=hint if hint is not None else (detail.strip() or "见 startup 日志"),
        )


class StopFailed(FrpsctlError):
    """进程停不下来（连 SIGKILL 都无效，例如 D 状态）。

    独立于 `StartupFailed` 存在：后者的消息是"frps 启动后立即退出"，用它描述
    "停止失败"会给出与事实**相反**的结论——用户会去查启动日志，而真问题是
    进程卡在内核里出不来。诊断信息错了比没有诊断更费时间。
    """

    exit_code = ExitCode.STARTUP_FAILED

    def __init__(self, pid: int, *, hint: str | None = None) -> None:
        super().__init__(
            f"无法停止 pid {pid}：发出 SIGKILL 后进程仍存在",
            hint=hint or "进程可能处于不可中断睡眠（D 状态），检查内核日志（dmesg）",
        )


class OwnershipConflict(FrpsctlError):
    """身份校验不通过 / systemd 与 direct 混用（ADR-1、ADR-7）。"""

    exit_code = ExitCode.OWNERSHIP_CONFLICT


# --- 控制面（7） --------------------------------------------------------


class AdminUnreachable(FrpsctlError):
    exit_code = ExitCode.ADMIN_UNREACHABLE


class ApiVersionMismatch(FrpsctlError):
    """ADR-3：拿到 404 不降级，而是报错并附实测版本。"""

    exit_code = ExitCode.ADMIN_UNREACHABLE


# --- 环境（8 / 1） ------------------------------------------------------


class PermissionRequired(FrpsctlError):
    exit_code = ExitCode.PERMISSION


class LockBusy(FrpsctlError):
    exit_code = ExitCode.OWNERSHIP_CONFLICT


class UnsupportedPlatform(FrpsctlError):
    """非 Linux 内核：身份校验无从谈起，拒绝而非降级（§3.4）。"""

    exit_code = ExitCode.USAGE


# --- 变更闭环（9） ------------------------------------------------------


class ChangeRolledBack(FrpsctlError):
    """变更后启动/健康检查失败，已尝试回滚（ADR-6、§9 第 8 步）。

    `hint` 是**回滚结果本身**，不是泛泛的安慰话：回滚可能只恢复了配置文件而
    没能把服务拉回来。用户据这一行决定"要不要人工介入"，所以必须如实——
    说"已回滚"却留个 DOWN 的实例，比报错更糟。
    """

    exit_code = ExitCode.ROLLED_BACK

    def __init__(self, detail: str = "", *, hint: str | None = None) -> None:
        super().__init__(
            "变更后启动失败，已自动回滚到上一份配置",
            hint=hint.strip() if hint and hint.strip() else (detail.strip() or "原配置已恢复"),
        )
