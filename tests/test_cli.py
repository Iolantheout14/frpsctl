"""CLI 层回归测试（设计文档 §7 命令行契约）。

这一层守的是**命令行的可脚本化契约**：退出码、`--json` 形态、以及"人读输出
不得泄露机密"。它跑在 `typer.testing.CliRunner` 上，不起真实服务。

其中 `test_init_*` 那组来自一次真机事故：`init` 生成的配置里 `allowPorts` 被写在了
`[log]` 之后，TOML 语义下它成了 `log.allowPorts`，而 frps 的报错是极具误导性的
`unknown field "allowPorts"`——看起来像键名错了，实际是位置错了。这类 bug 只有
"生成配置 → 真的交给 frps 校验"才能发现，所以这里既做结构断言，也做端到端校验。
"""

from __future__ import annotations

import tomllib

import pytest
from frpsctl.cli import app
from frpsctl.cli.context import map_exceptions


class _Result:
    """最小结果对象。

    `output` 是两流合并视图（既有断言都依赖它，保持不变）；`stdout` / `stderr`
    分开保留，供"诊断只能进 stderr"这类断言使用——**合并视图做不到这件事**，
    而 `--verbose` 的核心契约恰恰是"不污染 stdout"。
    """

    def __init__(
        self,
        exit_code: int,
        output: str,
        exception: BaseException | None = None,
        *,
        stdout: str = "",
        stderr: str = "",
    ):
        self.exit_code = exit_code
        self.output = output
        self.exception = exception
        self.stdout = stdout
        self.stderr = stderr


class _Cli:
    """把命令跑成 (退出码, 输出)。

    为什么不用 `CliRunner.invoke`：它内部走 `standalone_mode=False`，异常会原样
    抛给调用者而**不做**退出码映射，于是每个断言都只能看到 1——测到的不是真实
    契约。这里改为显式捕获 `SystemExit`，并把异常交给生产代码用的同一个映射器
    （`map_exceptions`），测试与 `frpsctl` 可执行文件的行为因此严格一致。
    """

    def invoke(self, app, args, **kwargs) -> _Result:
        import io
        import sys

        stdout, stderr = io.StringIO(), io.StringIO()
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        exception: BaseException | None = None
        try:
            with map_exceptions():
                app(args, standalone_mode=False)
            code = 0
        except SystemExit as exc:
            code = int(exc.code or 0)
        except BaseException as exc:  # noqa: BLE001 - 未分类异常按 1 报，便于断言暴露
            exception = exc
            code = 1
        finally:
            sys.stdout, sys.stderr = real_out, real_err
        out_text, err_text = stdout.getvalue(), stderr.getvalue()
        return _Result(
            code,
            out_text + err_text,
            exception,
            stdout=out_text,
            stderr=err_text,
        )


runner = _Cli()


def install_fake_binary(tmp_path, *, version: str = "0.71.0"):
    """给 CLI 测试装一个假 frps（版本号来自文件名，与生产布局一致）。

    凡是要走 `config set` / `verify` 的用例都需要它——那些路径会调用真实二进制
    做权威校验，没有二进制时一律以退出码 4 收场。
    """
    from .conftest import make_fake_frps

    return make_fake_frps(tmp_path / "data" / "bin", version=version)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """把 CLI 的实例根目录指到临时目录，避免碰用户的真实数据。"""
    monkeypatch.setenv("FRPSCTL_ROOT", str(tmp_path / "instances"))
    monkeypatch.setenv("FRPSCTL_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "data" / "bin").mkdir(parents=True, exist_ok=True)
    return tmp_path


class TestExitCodes:
    def test_usage_error_is_two(self, cli_env) -> None:
        result = runner.invoke(app, ["status", "--no-such-option"])
        assert result.exit_code == 2, "参数错误必须是退出码 2"

    def test_missing_binary_is_four(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["verify"])
        assert result.exit_code == 4, result.output

    def test_not_running_is_five(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 5, result.output

    def test_status_never_fails(self, cli_env) -> None:
        """`status` 必须永远能回答"现在什么情况"——即使什么都没配。"""
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "STOPPED" in result.output

    def test_version_flag(self, cli_env) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "frpsctl" in result.output


class TestJsonOutput:
    def test_json_option_works_after_subcommand(self, cli_env) -> None:
        """`status --json` 必须可用（文档命令表写的是子命令级选项）。

        只提供全局 `--json` 时它会报 "No such option"，与文档和用户直觉都不符。
        """
        result = runner.invoke(app, ["status", "--json"])
        assert result.exit_code == 0, result.output
        import json

        payload = json.loads(result.output)
        assert payload["state"] == "STOPPED"
        assert payload["owner"] == "none"

    def test_global_json_still_works(self, cli_env) -> None:
        import json

        result = runner.invoke(app, ["--json", "status"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["state"] == "STOPPED"

    def test_watch_json_emits_ndjson(self, cli_env, monkeypatch) -> None:
        """`status --watch --json` 必须逐行输出完整 JSON（NDJSON）。

        多行缩进格式在连续输出时会变成一串无法逐行解析的片段——脚本拿到
        只能干瞪眼。这里用 monkeypatch 让第二次 sleep 抛 KeyboardInterrupt
        （watch 的正常退出方式），从而在测试里截获两行输出。
        """
        import json
        import time

        runner.invoke(app, ["init", "--no-input"])
        real_sleep = time.sleep
        calls = {"n": 0}

        def fake_sleep(seconds):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise KeyboardInterrupt
            real_sleep(0)

        monkeypatch.setattr("frpsctl.cli.time.sleep", fake_sleep)
        result = runner.invoke(app, ["status", "--watch", "--json"])
        assert result.exit_code == 0, result.output

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) >= 2, f"NDJSON 行数不足：{lines}"
        for line in lines:
            payload = json.loads(line)  # 任何一行混入缩进就会在这里炸
            assert payload["instance"] == "default"

    def test_watch_does_not_emit_ansi_when_not_a_tty(self, cli_env, monkeypatch) -> None:
        """非终端（重定向/管道）不得写 ANSI 清屏码——会污染输出文件。

        测试里 stdout 是 StringIO（isatty() 为 False），正好覆盖这条路径。
        """
        runner.invoke(app, ["init", "--no-input"])

        def fake_sleep(seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr("frpsctl.cli.time.sleep", fake_sleep)
        result = runner.invoke(app, ["status", "--watch"])
        assert result.exit_code == 0, result.output
        assert "\033[2J" not in result.stdout, "非终端输出混入了清屏转义码"

    def test_config_get_masks_secret_by_default(self, cli_env) -> None:
        """§10 硬约束 2：机密绝不进 `--json`，除非显式 --reveal。

        打码形式是"保留首尾各两字符"（`SU***56`）而不是全 `***`：既防止泄露，
        又让人能核对"改的是不是同一个值"。
        """
        runner.invoke(app, ["init", "--no-input"])
        import json

        result = runner.invoke(app, ["config", "get", "auth.token", "--json"])
        assert result.exit_code == 0, result.output
        masked = json.loads(result.output)["value"]
        assert "***" in masked, f"token 未被识别为敏感：{masked}"

        # 真实值绝不能出现在输出里
        raw = (cli_env / "instances" / "default" / "frps.toml").read_text("utf-8")
        import re

        token = re.search(r'token = "([^"]+)"', raw).group(1)
        assert token not in result.output, "token 原文泄露进了 --json"

        revealed = runner.invoke(app, ["config", "get", "auth.token", "--json", "--reveal"])
        assert json.loads(revealed.output)["value"] == token

    def test_config_get_table_masks_nested_secrets(self, cli_env) -> None:
        """回归：`config get <表>` 曾整表明文输出，把表内的 token/口令漏出去。

        `is_secret_key("auth")` 是 False，所以只判被查询的那个键名是不够的——
        必须递归到 `auth.token` / `webServer.password` 这一层。
        """
        runner.invoke(app, ["init", "--no-input"])
        raw = (cli_env / "instances" / "default" / "frps.toml").read_text("utf-8")
        import re

        token = re.search(r'token = "([^"]+)"', raw).group(1)
        password = re.search(r'password = "([^"]+)"', raw).group(1)

        for key in ("auth", "webServer"):
            result = runner.invoke(app, ["config", "get", key, "--json"])
            assert result.exit_code == 0, result.output
            assert token not in result.output, f"config get {key} 泄露了 token"
            assert password not in result.output, f"config get {key} 泄露了口令"

    def test_config_diff_masks_secrets(self, cli_env) -> None:
        """回归：diff 就是配置原文，不打码会把机密从这一个出口漏光。"""
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        runner.invoke(app, ["config", "set", "maxPortsPerClient", "30"])
        raw = (cli_env / "instances" / "default" / "frps.toml").read_text("utf-8")
        import re

        token = re.search(r'token = "([^"]+)"', raw).group(1)

        result = runner.invoke(app, ["config", "diff", "--json"])
        assert result.exit_code == 0, result.output
        assert token not in result.output, "config diff --json 泄露了 token"


class TestNumericOptionValidation:
    """数值选项的非法值必须归入**用法错误(2)**，而不是"未分类错误(1)"或危险行为。

    回归：`status --watch --interval -1` 会让 `time.sleep` 抛 ValueError →
    "未分类错误(1)"；`stop --timeout -1` 更糟——它跳过等待直接升级 SIGKILL，
    一个参数笔误造成了不可逆动作。
    """

    def test_negative_interval_is_usage_error(self, cli_env) -> None:
        result = runner.invoke(app, ["status", "--watch", "--interval", "-1"])
        assert result.exit_code == 2, result.output

    def test_negative_lines_is_usage_error(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["log", "-n", "-5"])
        assert result.exit_code == 2, result.output

    def test_negative_stop_timeout_is_usage_error(self, cli_env) -> None:
        result = runner.invoke(app, ["stop", "--timeout", "-1"])
        assert result.exit_code == 2, result.output

    def test_negative_health_timeout_is_usage_error(self, cli_env) -> None:
        result = runner.invoke(app, ["start", "--health-timeout", "-1"])
        assert result.exit_code == 2, result.output


class TestInitConfig:
    """来自真机事故的回归组：顶层键的位置。"""

    def test_top_level_keys_precede_any_table(self, cli_env) -> None:
        """`allowPorts` 等顶层键必须出现在第一个 `[table]` 之前。

        写在表头之后，TOML 会把它归入那张表（`log.allowPorts`），frps 报
        `unknown field "allowPorts"`——一个把人引向错误方向的错误信息。
        """
        result = runner.invoke(app, ["init", "--no-input", "--allow-ports", "6000-6100"])
        assert result.exit_code == 0, result.output
        text = (cli_env / "instances" / "default" / "frps.toml").read_text("utf-8")

        first_table = min(
            (text.index(line) for line in text.splitlines() if line.startswith("[")),
            default=len(text),
        )
        for key in ("bindAddr", "bindPort", "maxPortsPerClient", "allowPorts"):
            index = text.index(f"\n{key}")
            assert index < first_table, f"{key} 写在了表头之后，会被归入那张表"

    def test_generated_config_parses_to_top_level_keys(self, cli_env) -> None:
        """用 TOML 解析器验证：这些键确实落在文档根，而不是某张表里。"""
        runner.invoke(
            app,
            ["init", "--no-input", "--allow-ports", "6000-6100", "--bind-port", "17000"],
        )
        raw = (cli_env / "instances" / "default" / "frps.toml").read_bytes()
        data = tomllib.loads(raw.decode())

        assert data["bindPort"] == 17000
        assert data["maxPortsPerClient"] == 20
        assert data["allowPorts"] == [{"start": 6000, "end": 6100}]
        assert "allowPorts" not in data.get("log", {}), "allowPorts 掉进了 log 表"
        # 安全基线（§10）
        assert data["transport"]["tls"]["force"] is True
        assert data["webServer"]["addr"] == "127.0.0.1"
        assert data["webServer"]["user"] and data["webServer"]["password"]
        assert data["auth"]["token"]

    def test_generated_config_is_0600(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        path = cli_env / "instances" / "default" / "frps.toml"
        assert path.stat().st_mode & 0o777 == 0o600

    def test_invalid_port_spec_is_rejected(self, cli_env) -> None:
        result = runner.invoke(app, ["init", "--no-input", "--allow-ports", "6100-6000"])
        assert result.exit_code == 3, result.output

    def test_init_validates_generated_config(self, cli_env) -> None:
        """非法端口必须在 init 阶段失败，且**不落盘**。

        回归：`init --bind-port 99999` 此前会成功生成一份必然被 verify 拒绝的
        配置，把问题推迟到 start 才暴露。范围约束现在由 Click 的 min/max 承担
        （归入用法错误 2，与 `stop --timeout` 等数值选项一致）；
        `cfg.validate_semantics(text)` 仍作为生成后的兜底自检保留。
        """
        result = runner.invoke(app, ["init", "--no-input", "--bind-port", "99999"])
        assert result.exit_code == 2, result.output
        assert not (cli_env / "instances" / "default" / "frps.toml").exists()

    def test_bind_port_zero_is_usage_error(self, cli_env) -> None:
        """`--bind-port 0` 直接归入用法错误(2)。

        实测：frp 对 `bindPort = 0` 回落到默认 7000——写成 0 会得到"我设了 0
        怎么监听 7000"的困惑。引导用户写真实端口，而不是悄悄回落。
        """
        result = runner.invoke(app, ["init", "--no-input", "--bind-port", "0"])
        assert result.exit_code == 2, result.output

    def test_second_init_requires_force(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["init", "--no-input"])
        assert result.exit_code == 3
        assert "--force" in result.output

    def test_shell_completion_is_available(self, cli_env) -> None:
        """补全选项必须存在（此前 `add_completion=False` 把它整个关掉了）。"""
        result = runner.invoke(app, ["--show-completion"])
        assert result.exit_code == 0, result.output
        assert result.stdout.strip(), "--show-completion 没有输出补全脚本"


class TestConfigSetValidation:
    def test_invalid_value_rejected_before_touching_file(self, cli_env) -> None:
        """§9 第 3 步：非法候选值必须在**动线上文件之前**就被拒绝。"""
        runner.invoke(app, ["init", "--no-input"])
        path = cli_env / "instances" / "default" / "frps.toml"
        before = path.read_text("utf-8")

        result = runner.invoke(app, ["config", "set", "bindPort", "99999"])
        assert result.exit_code == 3, result.output
        assert path.read_text("utf-8") == before, "非法值改动了线上文件"

    def test_template_syntax_rejected(self, cli_env) -> None:
        """§3.1 推论 2：值含 `{{` 会被 frp 当模板渲染，必须拒绝。"""
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["config", "set", "subDomainHost", '"{{ .Envs.X }}"'])
        assert result.exit_code == 3
        assert "模板" in result.output

    def test_noop_set_reports_no_change(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["config", "set", "maxPortsPerClient", "20"])
        assert result.exit_code == 0
        assert "无需变更" in result.output

    def test_noop_set_reports_json(self, cli_env) -> None:
        """`--json` 模式下 noop 也必须输出 JSON（此前会打印人读文本）。"""
        import json

        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["config", "set", "maxPortsPerClient", "20", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["noop"] is True
        assert payload["applied"] is False

    def test_edit_missing_config_is_a_config_error(self, cli_env, monkeypatch) -> None:
        """配置不存在时 `config edit` 必须是配置错误(3)，而不是未分类错误(1)。

        回归：直接 `read_text` 让裸 `FileNotFoundError` 冒到 CLI 顶层。
        """
        monkeypatch.setenv("EDITOR", "true")
        result = runner.invoke(app, ["config", "edit"])
        assert result.exit_code == 3, result.output
        assert "配置文件不存在" in result.output

    def test_editor_with_arguments_is_supported(self, cli_env, monkeypatch) -> None:
        """`EDITOR="true --whatever"`（带参数写法）必须可用。

        回归：此前把整个字符串当可执行文件路径 → `FileNotFoundError: 'true --whatever'`
        → "未分类错误(1)"，而带参数的 EDITOR 是完全正常的配置方式。
        """
        runner.invoke(app, ["init", "--no-input"])
        monkeypatch.setenv("EDITOR", "true --wait --reuse-window")
        result = runner.invoke(app, ["config", "edit"])
        assert result.exit_code == 0, result.output
        assert "没有改动" in result.output

    def test_editor_with_unbalanced_quotes_is_usage_error(self, cli_env, monkeypatch) -> None:
        runner.invoke(app, ["init", "--no-input"])
        monkeypatch.setenv("EDITOR", "vi'm")
        result = runner.invoke(app, ["config", "edit"])
        assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# 回归：曾因漏加参数而 NameError 的两条命令（此前零覆盖）
# ---------------------------------------------------------------------------


class TestCommandsThatWereBroken:
    """`log` 与 `config edit` 曾因批量加 `--json` 时漏参数而 NameError。

    133 个测试都没抓到，原因是这两条命令当时**零覆盖**。这类"代码能 import
    但一执行就崩"的问题只有真正调用才能发现，因此这里补上冒烟级断言。
    """

    def test_log_does_not_crash_and_reports_missing_file(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["log"])
        # 日志文件还不存在：必须给出**明确**的配置错误（退出码 3），而不是 NameError
        assert "NameError" not in result.output
        assert result.exit_code == 3, result.output
        assert "日志文件不存在" in result.output

    def test_log_tails_an_existing_file(self, cli_env) -> None:
        """`log` 输出文件尾部（纯 Python tail，写回 CLI 的 stdout）。"""
        runner.invoke(app, ["init", "--no-input"])
        log_file = cli_env / "instances" / "default" / "frps.log"
        log_file.write_text("第一行\n第二行\n", "utf-8")

        result = runner.invoke(app, ["log", "-n", "5"])
        assert result.exit_code == 0, result.output
        assert "第二行" in result.stdout

    def test_log_limits_lines(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        log_file = cli_env / "instances" / "default" / "frps.log"
        log_file.write_text("1\n2\n3\n4\n5\n", "utf-8")

        result = runner.invoke(app, ["log", "-n", "2"])
        assert result.exit_code == 0, result.output
        assert result.stdout == "4\n5\n"

    def test_log_does_not_depend_on_tail(self, cli_env, monkeypatch) -> None:
        """`log` 不得依赖外部 `tail`（最小化镜像里可能没有它）。

        回归：此前用 `subprocess.call(["tail", ...])`，缺失时的裸
        `FileNotFoundError` 会被映射成"未分类错误(1)"——把环境缺命令
        误报成工具内部错误。
        """
        import subprocess

        runner.invoke(app, ["init", "--no-input"])
        log_file = cli_env / "instances" / "default" / "frps.log"
        log_file.write_text("hello-line\n", "utf-8")

        def boom(*args, **kwargs):
            raise AssertionError("log 不应调用外部命令")

        monkeypatch.setattr(subprocess, "call", boom)
        result = runner.invoke(app, ["log", "-n", "5"])
        assert result.exit_code == 0, result.output
        assert "hello-line" in result.stdout

    def test_config_edit_reports_no_change(self, cli_env, monkeypatch) -> None:
        """`EDITOR=true` 不修改文件 → 应当报告"没有改动"而不是崩掉。"""
        runner.invoke(app, ["init", "--no-input"])
        monkeypatch.setenv("EDITOR", "true")
        result = runner.invoke(app, ["config", "edit"])
        assert "NameError" not in result.output
        assert result.exit_code == 0, result.output
        assert "没有改动" in result.output

    def test_every_subcommand_is_invokable(self, cli_env) -> None:
        """把每条命令都真正调用一次，捕捉"能 import 但一跑就崩"的漏网之鱼。

        刻意不校验业务结果（各命令有自己的用例），只要求**不出现 NameError /
        TypeError 这类编程错误**。
        """
        runner.invoke(app, ["init", "--no-input"])
        for args in (
            ["--version"],
            ["status"],
            ["status", "--json"],
            ["doctor"],
            ["verify"],
            ["log"],
            ["config", "get", "bindPort"],
            ["config", "diff"],
            ["service", "status"],
            ["plugin", "check"],
            ["plugin", "--help"],
            ["kick", "some-proxy"],
            ["stop"],
            ["restart"],
        ):
            result = runner.invoke(app, args)
            assert "NameError" not in result.output, f"{args} 触发 NameError"
            assert "TypeError" not in result.output, f"{args} 触发 TypeError"
            assert "UnboundLocalError" not in result.output, f"{args} 触发 UnboundLocalError"


class TestHealthGate:
    """健康 gate（L1 ∧ L2）失败必须可见（退出码 12，§3.7）。

    回归：`start` 此前在 L2 失败时**完全静默**——退出码 0、stderr 一行都没有，
    脚本会把"dashboard 起不来"当成成功。gate 是 start 的语义，失败必须以
    非零退出码收场；同时进程仍被托管（status 可见 RUNNING、stop 停得掉），
    所以不能用"启动失败(10)"来混淆两种情形。
    """

    def test_l2_failure_exits_12_and_keeps_process_managed(self, cli_env) -> None:
        import socket

        from .conftest import free_ports

        install_fake_binary(cli_env)
        # 占住 dashboard 端口：假 frps 绑定失败（进程仍存活）→ L2 必然失败
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        dash_port = blocker.getsockname()[1]
        bind_port = free_ports(1)[0]
        try:
            init = runner.invoke(
                app,
                [
                    "init",
                    "--no-input",
                    "--bind-port",
                    str(bind_port),
                    "--dashboard-port",
                    str(dash_port),
                ],
            )
            assert init.exit_code == 0, init.output
            result = runner.invoke(app, ["start", "--health-timeout", "1"])
        finally:
            blocker.close()

        try:
            assert result.exit_code == 12, result.output
            assert "健康检查未通过" in result.stderr, result.stderr

            status = runner.invoke(app, ["status", "--json"])
            import json

            payload = json.loads(status.stdout)
            assert payload["state"] == "RUNNING", payload
            assert payload["health"]["l2_control"] == "fail", payload
        finally:
            stop = runner.invoke(app, ["stop"])
            assert stop.exit_code == 0, stop.output


class TestStatusListen:
    """`status` 必须展示控制端口（设计文档 §7.4 的 listen 行）。"""

    def test_status_line_shows_listen(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input", "--bind-port", "17000", "--dashboard-port", "17500"])
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "listen   : 0.0.0.0:17000" in result.stdout

    def test_status_json_exposes_listen(self, cli_env) -> None:
        import json

        runner.invoke(app, ["init", "--no-input", "--bind-port", "17000"])
        result = runner.invoke(app, ["status", "--json"])
        payload = json.loads(result.stdout)
        assert payload["listen"] == {"addr": "0.0.0.0", "port": 17000}


class _FakeAdminClient:
    """AdminClient 的替身：只实现新命令用到的方法。"""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self) -> "_FakeAdminClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def list_clients(self) -> list[dict]:
        return [
            {
                "key": "alice.abc123",
                "user": "alice",
                "hostname": "test-host",
                "online": True,
                "clientIP": "127.0.0.1",
                "version": "0.71.0",
            }
        ]

    def list_proxies(self) -> list:
        from frpsctl.core.admin import V2Proxy

        return [
            V2Proxy(
                name="alice.web",
                user="alice",
                type="tcp",
                remote_port=6000,
                phase="online",
                cur_conns=2,
                today_traffic_in=1024,
                today_traffic_out=2048,
            )
        ]


class TestClientsAndProxiesCommands:
    def test_clients_requires_dashboard(self, cli_env) -> None:
        """dashboard 未启用 → 退出码 7（与 kick 同一判据）。"""
        runner.invoke(app, ["init", "--no-input", "--dashboard-port", "0"])
        result = runner.invoke(app, ["clients"])
        assert result.exit_code == 7, result.output

    def test_clients_renders_rows(self, cli_env, monkeypatch) -> None:
        runner.invoke(app, ["init", "--no-input"])
        monkeypatch.setattr("frpsctl.cli.AdminClient", _FakeAdminClient)
        result = runner.invoke(app, ["clients"])
        assert result.exit_code == 0, result.output
        assert "alice" in result.stdout
        assert "test-host" in result.stdout

    def test_clients_json_shape(self, cli_env, monkeypatch) -> None:
        import json

        runner.invoke(app, ["init", "--no-input"])
        monkeypatch.setattr("frpsctl.cli.AdminClient", _FakeAdminClient)
        result = runner.invoke(app, ["clients", "--json"])
        payload = json.loads(result.stdout)
        assert payload["clients"][0]["user"] == "alice"

    def test_proxies_renders_and_filters(self, cli_env, monkeypatch) -> None:
        import json

        runner.invoke(app, ["init", "--no-input"])
        monkeypatch.setattr("frpsctl.cli.AdminClient", _FakeAdminClient)
        result = runner.invoke(app, ["proxies"])
        assert result.exit_code == 0, result.output
        assert "alice.web" in result.stdout
        assert "6000" in result.stdout

        filtered = runner.invoke(app, ["proxies", "--type", "http"])
        assert "没有代理" in filtered.stdout

        as_json = runner.invoke(app, ["proxies", "--json"])
        assert json.loads(as_json.stdout)["proxies"][0]["name"] == "alice.web"


class TestInstancesCommand:
    def test_lists_nothing_gracefully(self, cli_env) -> None:
        result = runner.invoke(app, ["instances"])
        assert result.exit_code == 0, result.output
        assert "未找到任何实例" in result.output

    def test_lists_all_instances(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        runner.invoke(app, ["--instance", "web", "init", "--no-input"])
        result = runner.invoke(app, ["instances"])
        assert result.exit_code == 0, result.output
        assert "default" in result.stdout
        assert "web" in result.stdout
        assert "STOPPED" in result.stdout

    def test_json_shape(self, cli_env) -> None:
        import json

        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["instances", "--json"])
        payload = json.loads(result.stdout)
        assert payload["instances"][0]["instance"] == "default"
        assert payload["instances"][0]["state"] == "STOPPED"


class TestConfigListCommand:
    def test_lists_keys_and_masks_secrets(self, cli_env) -> None:
        import re

        runner.invoke(app, ["init", "--no-input"])
        raw = (cli_env / "instances" / "default" / "frps.toml").read_text("utf-8")
        token = re.search(r'token = "([^"]+)"', raw).group(1)

        result = runner.invoke(app, ["config", "list"])
        assert result.exit_code == 0, result.output
        assert "bindPort = 7000" in result.stdout
        assert "auth.token = " in result.stdout
        assert token not in result.stdout, "config list 泄露了 token"

    def test_prefix_filter(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["config", "list", "--prefix", "webServer"])
        assert result.exit_code == 0, result.output
        assert "webServer.port" in result.stdout
        assert "bindPort" not in result.stdout

    def test_tree_view_groups_tables(self, cli_env) -> None:
        """`--tree` 按表分组缩进展示（表头只显示末段名）。"""
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["config", "list", "--tree"])
        assert result.exit_code == 0, result.output
        assert "[auth]" in result.stdout
        assert "  token = " in result.stdout
        assert "[webServer]" in result.stdout
        assert "  port = " in result.stdout
        # 顶层键不缩进
        assert "\nbindPort = " in "\n" + result.stdout

    def test_tree_view_with_prefix(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["config", "list", "--prefix", "transport", "--tree"])
        assert result.exit_code == 0, result.output
        assert "[transport.tls]" in result.stdout
        assert "  force = true" in result.stdout

    def test_unknown_prefix_is_a_config_error(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["config", "list", "--prefix", "noSuchTable"])
        assert result.exit_code == 3, result.output

    def test_json_shape(self, cli_env) -> None:
        import json

        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["config", "list", "--json"])
        payload = json.loads(result.stdout)
        keys = {item["key"] for item in payload["keys"]}
        assert "bindPort" in keys
        token_item = next(item for item in payload["keys"] if item["key"] == "auth.token")
        assert "***" in str(token_item["value"])


class TestServiceLogs:
    """`service logs`（journald 集成）：argv 构造与缺命令时的一致性报错。"""

    def test_builds_journalctl_argv(self, cli_env, monkeypatch) -> None:
        import subprocess as sp

        runner.invoke(app, ["init", "--no-input"])
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/journalctl" if name == "journalctl" else None
        )
        monkeypatch.setattr(sp, "call", lambda argv: calls.append(list(argv)) or 0)

        result = runner.invoke(app, ["service", "logs", "-n", "50", "-f"])
        assert result.exit_code == 0, result.output
        assert calls == [
            ["journalctl", "-u", "frps@default.service", "-n", "50", "--no-pager", "-f"]
        ]

    def test_missing_journalctl_is_usage_error(self, cli_env, monkeypatch) -> None:
        runner.invoke(app, ["init", "--no-input"])
        monkeypatch.setattr("shutil.which", lambda _name: None)
        result = runner.invoke(app, ["service", "logs"])
        assert result.exit_code == 2, result.output
        assert "journalctl" in result.output


class TestHealthWaitHint:
    def test_start_prints_wait_hint_to_stderr(self, cli_env) -> None:
        """等待健康检查时给一行 stderr 提示（长等待不再静默）。

        必须走 stderr：`--json` 的 stdout 是机器可读契约。
        """
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input", "--dashboard-port", "0"])
        result = runner.invoke(app, ["start", "--health-timeout", "1"])
        try:
            assert result.exit_code == 0, result.output
            assert "等待健康检查" in result.stderr
            assert "等待健康检查" not in result.stdout
        finally:
            runner.invoke(app, ["stop"])

    def test_wait_progress_lines_are_emitted_during_failure(self, cli_env, monkeypatch) -> None:
        """gate 未通过时等待期有**逐轮进度**（非终端按行输出）。

        回归：进度此前只有一行静态提示，等待过程没有任何实时反馈。
        注意 `--health-timeout` 必须大于首轮探测耗时（L2 的 `/healthz` 超时
        1.5s），否则循环不进入、也就没有后续轮次可报告。
        """
        import socket

        from .conftest import free_ports

        monkeypatch.setattr("frpsctl.cli.ui._progress_last", 0.0)  # 消除跨测试限流
        install_fake_binary(cli_env)
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        dash_port = blocker.getsockname()[1]
        bind_port = free_ports(1)[0]
        try:
            runner.invoke(
                app,
                [
                    "init",
                    "--no-input",
                    "--bind-port",
                    str(bind_port),
                    "--dashboard-port",
                    str(dash_port),
                ],
            )
            result = runner.invoke(app, ["start", "--health-timeout", "3"])
        finally:
            blocker.close()
        try:
            assert result.exit_code == 12, result.output
            progress_lines = [
                line
                for line in result.stderr.splitlines()
                if line.startswith("等待健康检查 ") and "：L1" in line
            ]
            assert progress_lines, result.stderr
        finally:
            runner.invoke(app, ["stop"])

    def test_json_mode_stdout_stays_clean(self, cli_env) -> None:
        import json

        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input", "--dashboard-port", "0"])
        result = runner.invoke(app, ["start", "--health-timeout", "1", "--json"])
        try:
            assert result.exit_code == 0, result.output
            json.loads(result.stdout)  # 混入提示就会在这里炸
        finally:
            runner.invoke(app, ["stop"])


class TestInstallMirrorOption:
    """`install --mirror` / `FRPSCTL_MIRROR` 必须真的传到 release.install。"""

    @staticmethod
    def _capture(monkeypatch) -> dict:
        captured: dict = {}

        def fake_install(**kwargs):
            captured.update(kwargs)
            from pathlib import Path

            from frpsctl.core import release

            return release.InstallResult(
                version=str(kwargs["version"]),
                binary=Path("/tmp/frps-0.71.0"),
                switched=False,
                downloaded=False,
            )

        monkeypatch.setattr("frpsctl.core.release.install", fake_install)
        return captured

    def test_cli_mirror_is_passed_through(self, cli_env, monkeypatch) -> None:
        captured = self._capture(monkeypatch)
        result = runner.invoke(app, ["install", "--mirror", "https://example.com/dl/"])
        assert result.exit_code == 0, result.output
        assert captured["mirrors"] == ("https://example.com/dl",)

    def test_env_mirror_is_used_when_no_flag(self, cli_env, monkeypatch) -> None:
        monkeypatch.setenv("FRPSCTL_MIRROR", "https://a.example, https://b.example")
        captured = self._capture(monkeypatch)
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 0, result.output
        assert captured["mirrors"] == ("https://a.example", "https://b.example")

    def test_default_version_comes_from_single_source(self, cli_env, monkeypatch) -> None:
        """`install` 的默认版本来自 `RECKONED_VERSION`，不再两处硬编码。"""
        from frpsctl.core.version import RECKONED_VERSION

        captured = self._capture(monkeypatch)
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 0, result.output
        assert captured["version"] == ".".join(map(str, RECKONED_VERSION))


class TestConfigEdit:
    """`config edit` 的**落盘路径**此前零覆盖（只测了"没有改动"那条早退分支）。

    它是唯一一条"用户在编辑器里自由改写、然后走同一事务闭环"的入口，因此也是最
    容易把 §9 的校验/备份/替换顺序写错的地方。
    """

    def _append_max_ports(self, cli_env) -> str:
        """返回一段把 `maxPortsPerClient` 改成 30 的 sed 脚本内容。"""
        return "s/^maxPortsPerClient = .*/maxPortsPerClient = 30/"

    def test_edit_applies_change_and_restarts_nothing(self, cli_env, monkeypatch) -> None:
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        config = cli_env / "instances" / "default" / "frps.toml"
        assert "maxPortsPerClient = 20" in config.read_text("utf-8")

        editor = cli_env / "editor.sh"
        editor.write_text(
            f"#!/bin/sh\nsed -i '{self._append_max_ports(cli_env)}' \"$1\"\n",
            "utf-8",
        )
        editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(editor))

        result = runner.invoke(app, ["config", "edit", "--yes"])
        assert result.exit_code == 0, result.output
        assert "maxPortsPerClient = 30" in config.read_text("utf-8")
        # 实例没在跑：必须如实说明"尚未生效"，而不是谎报"已重启"
        assert "未运行" in result.output or "start 后生效" in result.output, result.output
        # 变更前必须先备份（§9 第 6 步）
        history = cli_env / "instances" / "default" / "config-history"
        assert history.is_dir() and list(history.iterdir()), "编辑前没有留下快照"

    def test_edit_rejects_editor_failure(self, cli_env, monkeypatch) -> None:
        """编辑器非零退出 = 用户放弃 → 配置必须零改动，且退出码 3。"""
        runner.invoke(app, ["init", "--no-input"])
        config = cli_env / "instances" / "default" / "frps.toml"
        before = config.read_text("utf-8")

        editor = cli_env / "editor.sh"
        editor.write_text("#!/bin/sh\nexit 1\n", "utf-8")
        editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(editor))

        result = runner.invoke(app, ["config", "edit"])
        assert result.exit_code == 3, result.output
        assert config.read_text("utf-8") == before

    def test_edit_rejects_invalid_result_before_touching_file(self, cli_env, monkeypatch) -> None:
        """编辑器写坏了配置 → 必须在**动线上文件之前**被拒绝（§9 第 3/5 步）。"""
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        config = cli_env / "instances" / "default" / "frps.toml"
        before = config.read_text("utf-8")

        editor = cli_env / "editor.sh"
        editor.write_text(
            "#!/bin/sh\nprintf 'bindPort = 99999\\n' > \"$1\"\n",
            "utf-8",
        )
        editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(editor))

        result = runner.invoke(app, ["config", "edit", "--yes"])
        assert result.exit_code == 3, result.output
        assert config.read_text("utf-8") == before, "非法草稿动到了线上文件"


class TestVerboseOption:
    """`--verbose` 曾是一个"被文档承诺、被解析、然后**从未被读过**"的空选项。

    那比没有这个选项更糟：用户加上它以为能看到诊断，实际什么都没变。现在它必须
    产生**可见**的诊断输出，且那些输出**只能进 stderr**——`--json` 的 stdout 是
    机器可读契约，混进诊断会让 `jq` 直接解析失败。
    """

    def test_verbose_emits_diagnostics_to_stderr(self, cli_env) -> None:
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["--verbose", "verify"])
        assert result.exit_code == 0, result.output
        assert "[trace]" in result.stderr, "--verbose 没有产生任何诊断输出"
        assert "verify" in result.stderr
        assert "[trace]" not in result.stdout, "诊断污染了 stdout"

    def test_verbose_works_after_subcommand(self, cli_env) -> None:
        """全局选项写在子命令**之后**也必须生效（README 的承诺）。

        此前只有 `--json` 被逐命令打过补丁，`--verbose` / `--yes` 写在后面会直接
        报 `No such option` —— 文档与实现不一致。现在由 `_AnywhereGroup` 统一处理。
        """
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["verify", "--verbose"])
        assert result.exit_code == 0, result.output
        assert "[trace]" in result.stderr

    def test_without_verbose_stderr_is_clean(self, cli_env) -> None:
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["verify"])
        assert result.exit_code == 0, result.output
        assert "[trace]" not in result.stderr

    def test_verbose_never_pollutes_json_stdout(self, cli_env) -> None:
        """`--verbose --json` 的 stdout 必须仍是**纯 JSON**。"""
        import json

        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["--verbose", "status", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)  # 混进 trace 就会在这里炸
        assert payload["instance"] == "default"
        assert "[trace]" in result.stderr


class TestGlobalOptionPlacement:
    """`_AnywhereGroup` 的行为契约。

    它必须把全局选项搬到前面，同时**不能**碰子命令自己的同名/相近选项——否则
    `install --version 0.71.0` 会被根命令的 `--version` 抢走，变成"打印版本后退出"。
    """

    def test_json_after_subcommand(self, cli_env) -> None:
        import json

        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["status", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["state"] == "STOPPED"

    def test_instance_after_subcommand(self, cli_env) -> None:
        result = runner.invoke(app, ["config", "get", "bindPort", "-i", "default"])
        assert result.exit_code == 3, result.output  # 配置不存在 → 3，但**不是**用法错误 2
        assert "No such option" not in result.output
        assert result.exit_code != 2

    def test_subcommand_version_still_belongs_to_install(self, cli_env) -> None:
        """`--version` **不在**全局白名单里：它是 `install` 的版本参数。

        若误把它当全局选项搬走，`install --version 0.69.1` 会变成"打印 frpsctl
        版本并退出 0"——一个**静默的错误成功**，比报错危险得多。
        """
        result = runner.invoke(app, ["install", "--version", "0.69.1"])
        assert result.exit_code == 4, result.output
        assert "不支持的 frps 版本" in result.output
        assert "0.69.1" in result.output

    def test_local_options_keep_their_position(self, cli_env) -> None:
        """局部选项的**值**不能被误当成子命令名搬走。"""
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["log", "-n", "5"])
        assert result.exit_code == 3, result.output  # 日志文件不存在
        assert "日志文件不存在" in result.output

    def test_yes_after_subcommand(self, cli_env, monkeypatch) -> None:
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        monkeypatch.setenv("EDITOR", "true")
        result = runner.invoke(app, ["config", "edit", "--yes"])
        assert result.exit_code == 0, result.output
        assert "没有改动" in result.output

    def test_double_dash_stops_hoisting(self, cli_env) -> None:
        """`--` 之后的值必须原样保留——哪怕它长得像全局选项。

        回归：`frpsctl config set subDomainHost -- --json` 曾把 `--json` 搬到
        命令最前面，于是 value 丢失（MissingParameter），用户无法写入任何
        以 `--` 开头的字符串值。
        """
        install_fake_binary(cli_env)
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["config", "set", "subDomainHost", "--", "--json"])
        assert result.exit_code == 0, result.output
        config = cli_env / "instances" / "default" / "frps.toml"
        assert 'subDomainHost = "--json"' in config.read_text("utf-8")

    def test_double_dash_with_global_option_value(self, cli_env) -> None:
        """`--` 之前的全局选项仍要正常前移（终止符不能把功能整体关掉）。"""
        import json

        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["status", "-i", "default", "--", "--json"])
        # `--json` 在终止符之后 → 属于多余位置参数，按用法错误收场；
        # 但 `-i default` 必须已经被正确解析（否则会报实例名相关错误）。
        assert result.exit_code == 2, result.output
        assert "No such option" not in result.output
        status = runner.invoke(app, ["--json", "status"])
        assert json.loads(status.stdout)["state"] == "STOPPED"
