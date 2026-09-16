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
