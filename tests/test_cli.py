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
    """最小结果对象：只有测试真正关心的两个字段。"""

    def __init__(self, exit_code: int, output: str, exception: BaseException | None = None):
        self.exit_code = exit_code
        self.output = output
        self.exception = exception


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
        return _Result(code, stdout.getvalue() + stderr.getvalue(), exception)


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

    def test_second_init_requires_force(self, cli_env) -> None:
        runner.invoke(app, ["init", "--no-input"])
        result = runner.invoke(app, ["init", "--no-input"])
        assert result.exit_code == 3
        assert "--force" in result.output


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

    def test_log_tails_an_existing_file(self, cli_env, capfd) -> None:
        """`log` 把文件交给 `tail` 子进程（流式输出），因此要断言**真实** stdout。

        用 `capfd`（捕获文件描述符）而不是 `capsys`/CliRunner：子进程直接继承
        父进程的 fd，只有 fd 级捕获才看得到它写的内容。
        """
        runner.invoke(app, ["init", "--no-input"])
        log_file = cli_env / "instances" / "default" / "frps.log"
        log_file.write_text("第一行\n第二行\n", "utf-8")

        result = runner.invoke(app, ["log", "-n", "5"])
        assert result.exit_code == 0, result.output

        captured = capfd.readouterr()
        assert "第二行" in captured.out

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
