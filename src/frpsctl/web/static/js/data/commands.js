/** 命令面数据（Web"命令"视图与 Ctrl+K 的数据源）。
 *
 *  ⚠️ 本文件由生成器写入：`python -m frpsctl.cli.introspect --write`
 *  数据源 = `cli/introspect.py::command_surface()`，与命令面契约快照
 *  （tests/snapshots/cli_commands.json）同源。**请勿手工编辑**——CI 会跑
 *  `--check` 断言重新生成后无 diff（界面与 CLI 命令面不允许漂移）。
 */

export const COMMAND_SURFACE = {
  "commands": [
    {
      "group": "",
      "name": "capabilities",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "capabilities",
      "readonly": true,
      "summary": "输出能力清单：命令 / 退出码 / 环境变量 / 版本门槛。",
      "web_view": "commands"
    },
    {
      "group": "",
      "name": "clients",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "clients",
      "readonly": true,
      "summary": "列出在线客户端（v2 Admin API，自动翻页取全量）。",
      "web_view": "dash"
    },
    {
      "group": "config",
      "name": "apply",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "只校验并展示 diff，不写入、不重启",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "dry_run",
          "opts": [
            "--dry-run"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 10.0,
          "default_text": "10.0",
          "help": "健康检查等待秒数（上限 600）",
          "is_flag": false,
          "kind": "opt",
          "max": 600,
          "min": 0,
          "multiple": false,
          "name": "health_timeout",
          "opts": [
            "--health-timeout"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "只写不重启（变更尚未生效）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "no_restart",
          "opts": [
            "--no-restart"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "<tuple>:()",
          "default_text": null,
          "help": "键=值（可重复）；与 --unset 至少给一个",
          "is_flag": false,
          "kind": "opt",
          "multiple": true,
          "name": "sets",
          "opts": [
            "--set"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "<tuple>:()",
          "default_text": null,
          "help": "删除键（可重复，回落 frp 默认值）",
          "is_flag": false,
          "kind": "opt",
          "multiple": true,
          "name": "unsets",
          "opts": [
            "--unset"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "config apply",
      "readonly": false,
      "summary": "**多键**变更：一次提交 → 一份快照 → 一次重启（与 Web 配置表单同语义）。",
      "web_view": "config"
    },
    {
      "group": "config",
      "name": "diff",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 1,
          "default_text": "1",
          "help": "与第 N 新的快照比较",
          "is_flag": false,
          "kind": "opt",
          "max": null,
          "min": 1,
          "multiple": false,
          "name": "steps",
          "opts": [
            "--steps"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "config diff",
      "readonly": true,
      "summary": "当前配置 vs 历史快照（unified diff）。",
      "web_view": "config"
    },
    {
      "group": "config",
      "name": "edit",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": 10.0,
          "default_text": "10.0",
          "help": "健康检查等待秒数（上限 600）",
          "is_flag": false,
          "kind": "opt",
          "max": 600,
          "min": 0,
          "multiple": false,
          "name": "health_timeout",
          "opts": [
            "--health-timeout"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "跳过「应用以上改动并重启？」的确认",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "yes",
          "opts": [
            "--yes",
            "-y"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "config edit",
      "readonly": false,
      "summary": "用 $EDITOR 编辑，保存后走完全相同的闭环（先展示 diff 让人确认）。",
      "web_view": "config"
    },
    {
      "group": "config",
      "name": "get",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "点分键，如 transport.tls.force",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "key",
          "opts": [
            "key"
          ],
          "positional": true,
          "required": true,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "显示敏感值（默认打码）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "reveal",
          "opts": [
            "--reveal"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "config get",
      "readonly": true,
      "summary": "读单个键。敏感键默认打码（§10 硬约束 2）。",
      "web_view": "config"
    },
    {
      "group": "config",
      "name": "list",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "",
          "default_text": null,
          "help": "只看某个前缀下的键（点分路径，如 webServer）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "prefix",
          "opts": [
            "--prefix"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "按表分组缩进展示（默认平铺点分键）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "tree",
          "opts": [
            "--tree"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "config list",
      "readonly": true,
      "summary": "列出全部配置键（点分路径 + 打码后的值）。",
      "web_view": "config"
    },
    {
      "group": "config",
      "name": "rollback",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": 10.0,
          "default_text": "10.0",
          "help": "健康检查等待秒数（上限 600）",
          "is_flag": false,
          "kind": "opt",
          "max": 600,
          "min": 0,
          "multiple": false,
          "name": "health_timeout",
          "opts": [
            "--health-timeout"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 1,
          "default_text": "1",
          "help": "回滚到 N 份之前的快照",
          "is_flag": false,
          "kind": "opt",
          "max": null,
          "min": 1,
          "multiple": false,
          "name": "steps",
          "opts": [
            "steps"
          ],
          "positional": true,
          "required": false,
          "secondary": []
        }
      ],
      "path": "config rollback",
      "readonly": false,
      "summary": "回滚到 N 份之前。**复用同一闭环**，而不是简单 cp 覆盖。",
      "web_view": "config"
    },
    {
      "group": "config",
      "name": "set",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "只校验并展示 diff，不写入、不重启",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "dry_run",
          "opts": [
            "--dry-run"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 10.0,
          "default_text": "10.0",
          "help": "健康检查等待秒数（上限 600）",
          "is_flag": false,
          "kind": "opt",
          "max": 600,
          "min": 0,
          "multiple": false,
          "name": "health_timeout",
          "opts": [
            "--health-timeout"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "点分键，如 bindPort",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "key",
          "opts": [
            "key"
          ],
          "positional": true,
          "required": true,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "只写不重启（变更尚未生效）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "no_restart",
          "opts": [
            "--no-restart"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "交互式隐藏输入值（敏感值不进 argv 与 shell 历史）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "prompt",
          "opts": [
            "--prompt"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "从标准输入读值（敏感值不进 argv 与 shell 历史）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "read_stdin",
          "opts": [
            "--stdin"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "新值；或用 --stdin / --prompt 提供（敏感值不进 argv）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "value",
          "opts": [
            "value"
          ],
          "positional": true,
          "required": false,
          "secondary": []
        }
      ],
      "path": "config set",
      "readonly": false,
      "summary": "写单个键，走 §9 事务闭环（校验 → 备份 → 原子替换 → 重启 → 失败回滚）。",
      "web_view": "config"
    },
    {
      "group": "config",
      "name": "unset",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "只校验并展示 diff，不写入、不重启",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "dry_run",
          "opts": [
            "--dry-run"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 10.0,
          "default_text": "10.0",
          "help": "健康检查等待秒数（上限 600）",
          "is_flag": false,
          "kind": "opt",
          "max": 600,
          "min": 0,
          "multiple": false,
          "name": "health_timeout",
          "opts": [
            "--health-timeout"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "要删除的点分键（回落 frp 默认值）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "key",
          "opts": [
            "key"
          ],
          "positional": true,
          "required": true,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "只写不重启（变更尚未生效）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "no_restart",
          "opts": [
            "--no-restart"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "config unset",
      "readonly": false,
      "summary": "删除一个键，让它回落到 frp 的默认值。",
      "web_view": "config"
    },
    {
      "group": "",
      "name": "doctor",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "doctor",
      "readonly": true,
      "summary": "体检：二进制 / 配置 / 权限 / 暴露面 / 端口 / 所有权 / 插件。",
      "web_view": "dash"
    },
    {
      "group": "",
      "name": "init",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": "",
          "default_text": null,
          "help": "端口白名单，如 6000-6100 或 6000,6001（留空 = 不限，不推荐）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "allow_ports",
          "opts": [
            "--allow-ports"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 7000,
          "default_text": "7000",
          "help": "控制端口 bindPort",
          "is_flag": false,
          "kind": "opt",
          "max": 65535,
          "min": 1,
          "multiple": false,
          "name": "bind_port",
          "opts": [
            "--bind-port"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 7500,
          "default_text": "7500",
          "help": "dashboard 端口（0 = 不启用）",
          "is_flag": false,
          "kind": "opt",
          "max": 65535,
          "min": 0,
          "multiple": false,
          "name": "dashboard_port",
          "opts": [
            "--dashboard-port"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "覆盖已存在的配置（会先备份）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "force",
          "opts": [
            "--force"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "全部使用默认值，不交互",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "no_input",
          "opts": [
            "--no-input"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "init",
      "readonly": false,
      "summary": "生成安全基线的入门配置（§10）。",
      "web_view": "config"
    },
    {
      "group": "",
      "name": "install",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "已存在同版本时重新下载",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "force",
          "opts": [
            "--force"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "拿不到官方校验和时仍继续（风险自负）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "insecure",
          "opts": [
            "--insecure"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "下载源，可重复指定；默认内置源（也可用 FRPSCTL_MIRROR，逗号分隔）",
          "is_flag": false,
          "kind": "opt",
          "multiple": true,
          "name": "mirror",
          "opts": [
            "--mirror"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "只落盘，不切换软链（§8.6.1）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "only_download",
          "opts": [
            "--only-download"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "0.71.0",
          "default_text": "0.71.0",
          "help": "要安装的 frps 版本（默认当前推荐版本）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "version",
          "opts": [
            "--version"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "同时取出 frpc（供插件契约测试使用，不额外下载）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "with_frpc",
          "opts": [
            "--with-frpc"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "install",
      "readonly": false,
      "summary": "下载官方 frps 二进制并强校验 sha256。",
      "web_view": "versions"
    },
    {
      "group": "",
      "name": "instances",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "同时做三层健康探测（每个运行中的实例一次网络往返）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "health",
          "opts": [
            "--health"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "instances",
      "readonly": true,
      "summary": "列出全部实例的一行式概览（多实例运维入口）。",
      "web_view": null
    },
    {
      "group": "",
      "name": "log",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "持续跟踪",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "follow",
          "opts": [
            "--follow",
            "-f"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 100,
          "default_text": "100",
          "help": "显示行数（0 = 不显示历史；上限 100000）",
          "is_flag": false,
          "kind": "opt",
          "max": 100000,
          "min": 0,
          "multiple": false,
          "name": "lines",
          "opts": [
            "--lines",
            "-n"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "log",
      "readonly": true,
      "summary": "看日志。优先 `log.to` 指向的文件；缺失时回退到 startup 日志（ADR-5）。",
      "web_view": "dash"
    },
    {
      "group": "plugin",
      "name": "stats",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "窗口起点：24h / 7d / 30m、ISO 时间或 unix 时间戳（默认全量）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "since",
          "opts": [
            "--since"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin audit stats",
      "readonly": true,
      "summary": "统计审计：总量 / 允许 / 拒绝 / 用户与操作分布 / 限速抑制累计。",
      "web_view": "audit"
    },
    {
      "group": "plugin",
      "name": "tail",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "持续跟踪新记录（Ctrl-C 退出）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "follow",
          "opts": [
            "--follow",
            "-f"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出（不能与 -f 同用）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 50,
          "default_text": "50",
          "help": "显示条数",
          "is_flag": false,
          "kind": "opt",
          "max": 10000,
          "min": 1,
          "multiple": false,
          "name": "lines",
          "opts": [
            "--lines",
            "-n"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin audit tail",
      "readonly": true,
      "summary": "看审计日志的尾部（JSONL，与 `frpsctl log` 同规格的反向读取）。",
      "web_view": "audit"
    },
    {
      "group": "plugin",
      "name": "check",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": "127.0.0.1:8080",
          "default_text": "127.0.0.1:8080",
          "help": "将要绑定的地址（用于校验）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "bind",
          "opts": [
            "--bind"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin check",
      "readonly": true,
      "summary": "离线校验策略：能载入吗？绑回环吗？典型裁决是否符合预期？",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "list",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin config list",
      "readonly": true,
      "summary": "列出策略级设置（不显示用户表——那用 `plugin user list`）。",
      "web_view": null
    },
    {
      "group": "plugin",
      "name": "set",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "字段名（见 `plugin config list`）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "key",
          "opts": [
            "key"
          ],
          "positional": true,
          "required": true,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "交互式隐藏输入值",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "prompt",
          "opts": [
            "--prompt"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "从标准输入读值",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "read_stdin",
          "opts": [
            "--stdin"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "新值；或用 --stdin / --prompt（敏感值不进 argv）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "value",
          "opts": [
            "value"
          ],
          "positional": true,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin config set",
      "readonly": false,
      "summary": "设置一个策略级字段，写入前用与 `plugin check` 相同的判据复验。",
      "web_view": null
    },
    {
      "group": "plugin",
      "name": "init",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "覆盖已存在的策略文件",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "force",
          "opts": [
            "--force"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径（默认 <实例>/plugin-policy.json）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin init",
      "readonly": false,
      "summary": "生成一份策略模板。",
      "web_view": null
    },
    {
      "group": "plugin",
      "name": "restart",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": null,
          "default_text": null,
          "help": "把每个请求打进日志文件",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "access_log",
          "opts": [
            "--access-log"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "绑定地址（默认复用上次参数）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "bind",
          "opts": [
            "--bind"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "回调路径（默认复用上次参数）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "path",
          "opts": [
            "--path"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件（默认复用上次参数）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin restart",
      "readonly": false,
      "summary": "重启后台运行的插件服务（未在运行时直接启动）。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "serve",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "把每个请求打进 stderr",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "access_log",
          "opts": [
            "--access-log"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "127.0.0.1:8080",
          "default_text": "127.0.0.1:8080",
          "help": "绑定地址（必须回环）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "bind",
          "opts": [
            "--bind"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "启动前以 JSON 输出一次状态（随后仍前台运行）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "/handler",
          "default_text": "/handler",
          "help": "插件回调路径（需与 frps 的 httpPlugins.path 一致）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "path",
          "opts": [
            "--path"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin serve",
      "readonly": false,
      "summary": "启动插件服务（前台）。",
      "web_view": null
    },
    {
      "group": "plugin",
      "name": "install",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "把逐请求日志写进 journald（排查用；unit 模板为全部实例共享）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "access_log",
          "opts": [
            "--access-log"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "127.0.0.1:8080",
          "default_text": "127.0.0.1:8080",
          "help": "监听地址（必须回环）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "bind",
          "opts": [
            "--bind"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "服务用户不存在时自动创建系统账户（需 root；须配合 --user）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "create_user",
          "opts": [
            "--create-user"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "覆盖已存在的 unit 模板",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "force",
          "opts": [
            "--force"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "运行插件的系统组（默认：同名组，缺失则用户主组）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "group",
          "opts": [
            "--group"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "/handler",
          "default_text": "/handler",
          "help": "回调路径（需与 frps 的 httpPlugins.path 一致）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "handler_path",
          "opts": [
            "--path"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件（默认 <实例>/plugin-policy.json）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "运行插件的系统用户（默认：优先 frps，其次当前用户）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "user",
          "opts": [
            "--user"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin service install",
      "readonly": false,
      "summary": "安装 frpsctl-plugin@.service 并 enable（需要 root）。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "restart",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin service restart",
      "readonly": false,
      "summary": "重启插件服务（systemd，需要 root）——改完策略后让它载入新配置的常用动作。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "start",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin service start",
      "readonly": false,
      "summary": "启动插件服务（systemd，需要 root）。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "status",
      "needs_root": false,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin service status",
      "readonly": true,
      "summary": "显示插件服务的 systemd 托管状态（active 与 enabled 分开报告）。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "stop",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin service stop",
      "readonly": false,
      "summary": "停止插件服务（systemd，需要 root）。⚠️ 停止期间所有客户端都无法登录（fail-closed）。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "uninstall",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin service uninstall",
      "readonly": false,
      "summary": "停用并删除插件 unit 模板（需要 root）。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "start",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": null,
          "default_text": null,
          "help": "把每个请求打进日志文件",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "access_log",
          "opts": [
            "--access-log"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "绑定地址（默认复用上次参数，否则 127.0.0.1:8080）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "bind",
          "opts": [
            "--bind"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "回调路径（默认复用上次参数，否则 /handler）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "path",
          "opts": [
            "--path"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件（默认复用上次参数，否则实例默认）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin start",
      "readonly": false,
      "summary": "后台启动插件服务（direct 模式；非 systemd 环境使用）。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "status",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin status",
      "readonly": true,
      "summary": "插件服务的托管状态（systemd / direct / 未运行 的统一视图）。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "stop",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin stop",
      "readonly": false,
      "summary": "停止后台运行的插件服务（SIGTERM 优雅退出并先刷审计）。",
      "web_view": "services"
    },
    {
      "group": "plugin",
      "name": "list",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin user list",
      "readonly": true,
      "summary": "列出策略里的用户与权限摘要（不显示策略级凭据）。",
      "web_view": null
    },
    {
      "group": "plugin",
      "name": "remove",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "用户名",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "name",
          "opts": [
            "name"
          ],
          "positional": true,
          "required": true,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin user remove",
      "readonly": false,
      "summary": "删除一个用户（不存在时报配置错误，不静默成功）。",
      "web_view": null
    },
    {
      "group": "plugin",
      "name": "set",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "代理数上限（0 = 不限）",
          "is_flag": false,
          "kind": "opt",
          "max": null,
          "min": 0,
          "multiple": false,
          "name": "max_proxies",
          "opts": [
            "--max-proxies"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "用户名",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "name",
          "opts": [
            "name"
          ],
          "positional": true,
          "required": true,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "允许的代理名通配，逗号分隔（如 'alice-*'）；空串 = 不限名称",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "names",
          "opts": [
            "--names"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "不允许随机端口",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "no_random_port",
          "opts": [
            "--no-random-port"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "备注（空串 = 清除）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "note",
          "opts": [
            "--note"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "策略文件路径",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "policy",
          "opts": [
            "--policy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "允许的端口/端口段，逗号分隔（如 6000-6010,7000）；空串 = 清空白名单",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "ports",
          "opts": [
            "--ports"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "允许 remote_port = 0（由 frps 分配）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "random_port",
          "opts": [
            "--random-port"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "允许的代理类型，逗号分隔（如 tcp,udp）；空串 = 不限",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "types",
          "opts": [
            "--types"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "plugin user set",
      "readonly": false,
      "summary": "新增或修改一个用户：只改**显式给出**的字段，其余保持原值。",
      "web_view": null
    },
    {
      "group": "",
      "name": "proxies",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "",
          "default_text": null,
          "help": "只看某类型（tcp/udp/http/https/stcp/xtcp/tcpmux/sudp）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "ptype",
          "opts": [
            "--type"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "proxies",
      "readonly": true,
      "summary": "列出代理（v2 Admin API，自动翻页取全量）。",
      "web_view": "dash"
    },
    {
      "group": "",
      "name": "prune",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "prune",
      "readonly": false,
      "summary": "清理 dashboard 统计里的离线代理记录。",
      "web_view": "dash"
    },
    {
      "group": "",
      "name": "restart",
      "needs_root": false,
      "needs_systemd": true,
      "params": [
        {
          "default": 10.0,
          "default_text": "10.0",
          "help": "健康检查等待秒数（上限 600）",
          "is_flag": false,
          "kind": "opt",
          "max": 600,
          "min": 0,
          "multiple": false,
          "name": "health_timeout",
          "opts": [
            "--health-timeout"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 10.0,
          "default_text": "10.0",
          "help": "停止等待秒数（上限 600）",
          "is_flag": false,
          "kind": "opt",
          "max": 600,
          "min": 0,
          "multiple": false,
          "name": "timeout",
          "opts": [
            "--timeout"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "restart",
      "readonly": false,
      "summary": "重启实例（stop → start）。配置变更请用 `config set`，它会自动回滚。",
      "web_view": "dash"
    },
    {
      "group": "service",
      "name": "install",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "服务用户不存在时自动创建系统账户（需 root；须配合 --user）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "create_user",
          "opts": [
            "--create-user"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "覆盖已存在的 unit 模板",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "force",
          "opts": [
            "--force"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "运行 frps 的系统组（默认：同名组，缺失则用户主组）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "group",
          "opts": [
            "--group"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "<PosixPath>:/var/log/frps",
          "default_text": "/var/log/frps",
          "help": "unit 中 ReadWritePaths 的目录",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "log_dir",
          "opts": [
            "--log-dir"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "运行 frps 的系统用户（默认：优先 frps，其次当前用户）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "user",
          "opts": [
            "--user"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "service install",
      "readonly": false,
      "summary": "安装 `frps@.service` 模板并 enable（需要 root）。",
      "web_view": "services"
    },
    {
      "group": "service",
      "name": "logs",
      "needs_root": false,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "持续跟踪",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "follow",
          "opts": [
            "--follow",
            "-f"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 100,
          "default_text": "100",
          "help": "显示行数（上限 100000）",
          "is_flag": false,
          "kind": "opt",
          "max": 100000,
          "min": 1,
          "multiple": false,
          "name": "lines",
          "opts": [
            "--lines",
            "-n"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "service logs",
      "readonly": true,
      "summary": "查看 systemd 托管的实例日志（`journalctl -u frps@<name>`）。",
      "web_view": "services"
    },
    {
      "group": "service",
      "name": "status",
      "needs_root": false,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "service status",
      "readonly": true,
      "summary": "显示 systemd 托管状态（active / enabled / 运行账户）。",
      "web_view": "services"
    },
    {
      "group": "service",
      "name": "uninstall",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "service uninstall",
      "readonly": false,
      "summary": "停用并删除 unit 模板（需要 root）。",
      "web_view": "services"
    },
    {
      "group": "",
      "name": "start",
      "needs_root": false,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "前台运行（调试用：不写 state、脱离本工具的托管，stop 管不到它）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "foreground",
          "opts": [
            "--foreground"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 10.0,
          "default_text": "10.0",
          "help": "健康检查等待秒数（上限 600）",
          "is_flag": false,
          "kind": "opt",
          "max": 600,
          "min": 0,
          "multiple": false,
          "name": "health_timeout",
          "opts": [
            "--health-timeout"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "start",
      "readonly": false,
      "summary": "启动实例：verify → 加锁 → 派生 → 早退检测 → 写 state → 健康检查。",
      "web_view": "dash"
    },
    {
      "group": "",
      "name": "status",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": 2.0,
          "default_text": "2.0",
          "help": "--watch 的刷新间隔秒数（上限 3600）",
          "is_flag": false,
          "kind": "opt",
          "max": 3600,
          "min": 0.1,
          "multiple": false,
          "name": "interval",
          "opts": [
            "--interval"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "持续刷新",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "watch",
          "opts": [
            "--watch"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "status",
      "readonly": true,
      "summary": "状态聚合：owner / 状态 / pid / 版本 / 运行时长 / 客户端 / 代理 / 流量 / 健康。",
      "web_view": "dash"
    },
    {
      "group": "",
      "name": "stop",
      "needs_root": false,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "直接 SIGKILL（不做 SIGTERM 等待）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "force",
          "opts": [
            "--force"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 10.0,
          "default_text": "10.0",
          "help": "SIGTERM 后等待秒数（上限 600）",
          "is_flag": false,
          "kind": "opt",
          "max": 600,
          "min": 0,
          "multiple": false,
          "name": "timeout",
          "opts": [
            "--timeout"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "stop",
      "readonly": false,
      "summary": "停止实例。身份校验不通过时**拒绝**（退出码 11），绝不冒险 kill。",
      "web_view": "dash"
    },
    {
      "group": "",
      "name": "traffic",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "",
          "default_text": null,
          "help": "只看某个代理（留空 = 全部代理逐日汇总）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "name",
          "opts": [
            "name"
          ],
          "positional": true,
          "required": false,
          "secondary": []
        }
      ],
      "path": "traffic",
      "readonly": true,
      "summary": "代理流量历史（近 7 天，日粒度）。",
      "web_view": "dash"
    },
    {
      "group": "",
      "name": "uninstall",
      "needs_root": true,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "卸载实例根下的全部实例（默认只卸当前实例）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "all_instances",
          "opts": [
            "--all"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "运行中的实例先停止再卸载",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "force",
          "opts": [
            "--force"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "保留共享二进制（其它实例仍要用）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "keep_bin",
          "opts": [
            "--keep-bin"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "保留实例数据（配置/快照/审计），只清 unit 与二进制",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "keep_data",
          "opts": [
            "--keep-data"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "uninstall",
      "readonly": false,
      "summary": "完整卸载：实例数据 / unit / 共享二进制（默认先列出清单并要求确认）。",
      "web_view": null
    },
    {
      "group": "",
      "name": "verify",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": null,
          "default_text": null,
          "help": "要校验的文件（默认实例配置）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "file",
          "opts": [
            "--file"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "verify",
      "readonly": true,
      "summary": "双保险校验：pydantic 语义 + 官方 `frps verify`。",
      "web_view": "config"
    },
    {
      "group": "web",
      "name": "stats",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "窗口起点：24h / 7d / 30m、ISO 时间或 unix 时间戳（默认全量）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "since",
          "opts": [
            "--since"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web audit stats",
      "readonly": true,
      "summary": "统计 Web 操作审计：总量 / 成功 / 失败 / 按动作与来源分布。",
      "web_view": "audit"
    },
    {
      "group": "web",
      "name": "tail",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "持续跟踪新记录（Ctrl-C 退出）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "follow",
          "opts": [
            "--follow",
            "-f"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出（不能与 -f 同用）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": 50,
          "default_text": "50",
          "help": "显示条数",
          "is_flag": false,
          "kind": "opt",
          "max": 10000,
          "min": 1,
          "multiple": false,
          "name": "lines",
          "opts": [
            "--lines",
            "-n"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web audit tail",
      "readonly": true,
      "summary": "看 Web 操作审计的尾部（谁在什么时候改了配置 / 停了服务）。",
      "web_view": "audit"
    },
    {
      "group": "web",
      "name": "set",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "交互式隐藏输入新口令",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "prompt",
          "opts": [
            "--prompt"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "从标准输入读新口令（不进 argv 与 shell 历史）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "read_stdin",
          "opts": [
            "--stdin"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web password set",
      "readonly": false,
      "summary": "设置（轮换）Web 管理台登录口令，写入 0600 口令文件。",
      "web_view": null
    },
    {
      "group": "web",
      "name": "show",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web password show",
      "readonly": true,
      "summary": "显示 `web service install` 生成的口令（文件缺失时报配置错误）。",
      "web_view": null
    },
    {
      "group": "web",
      "name": "restart",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": null,
          "default_text": null,
          "help": "把逐请求日志写进日志文件",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "access_log",
          "opts": [
            "--access-log"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "允许绑定非回环地址",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "allow_non_loopback",
          "opts": [
            "--allow-non-loopback"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "监听地址（默认复用上次启动参数）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "bind",
          "opts": [
            "--bind"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "暴露 /metrics",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "metrics",
          "opts": [
            "--metrics"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "轮换口令并写入实例口令文件",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "password",
          "opts": [
            "--password"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "口令文件",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "password_file",
          "opts": [
            "--password-file"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "信任反向代理的 X-Forwarded-For",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "trusted_proxy",
          "opts": [
            "--trusted-proxy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web restart",
      "readonly": false,
      "summary": "重启后台运行的 Web 管理台（未给参数时复用上次的启动参数）。",
      "web_view": "services"
    },
    {
      "group": "web",
      "name": "serve",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "把每个 HTTP 请求打进 stderr（排查用；默认关闭）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "access_log",
          "opts": [
            "--access-log"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "显式允许绑定非回环地址（建议配合反向代理 + TLS）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "allow_non_loopback",
          "opts": [
            "--allow-non-loopback"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "127.0.0.1:8787",
          "default_text": "127.0.0.1:8787",
          "help": "监听地址（默认只绑回环）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "bind",
          "opts": [
            "--bind"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "暴露 /metrics（Prometheus 文本；Basic auth：用户名任意、口令=管理台口令）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "metrics",
          "opts": [
            "--metrics"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "登录口令（默认自动生成并打印一次；也可用 FRPSCTL_WEB_PASSWORD。⚠ 命令行参数对本机其他用户可见，生产建议用环境变量或 --password-file）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "password",
          "opts": [
            "--password"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "从文件读取口令（systemd 部署用；文件需 0600）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "password_file",
          "opts": [
            "--password-file"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "信任反向代理的 X-Forwarded-For（取最后一跳作为登录限速来源；默认关闭）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "trusted_proxy",
          "opts": [
            "--trusted-proxy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web serve",
      "readonly": false,
      "summary": "启动 Web 管理台（前台运行）。",
      "web_view": null
    },
    {
      "group": "web",
      "name": "install",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "把逐请求日志写进 journald（写入 unit 的 serve 参数；unit 模板为全部实例共享）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "access_log",
          "opts": [
            "--access-log"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "显式允许绑定非回环地址（建议配合反向代理 + TLS）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "allow_non_loopback",
          "opts": [
            "--allow-non-loopback"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": "127.0.0.1:8787",
          "default_text": "127.0.0.1:8787",
          "help": "监听地址（默认只绑回环）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "bind",
          "opts": [
            "--bind"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "服务用户不存在时自动创建系统账户（需 root；须配合 --user）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "create_user",
          "opts": [
            "--create-user"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "覆盖已存在的 unit 模板",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "force",
          "opts": [
            "--force"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "运行管理台的系统组（默认：同名组，缺失则用户主组）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "group",
          "opts": [
            "--group"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "暴露 /metrics（写入 unit 的 serve 参数；unit 模板为全部实例共享）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "metrics",
          "opts": [
            "--metrics"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "信任反向代理的 X-Forwarded-For（写入 unit 的 serve 参数）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "trusted_proxy",
          "opts": [
            "--trusted-proxy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "运行管理台的系统用户（默认：优先 frps，其次当前用户）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "user",
          "opts": [
            "--user"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web service install",
      "readonly": false,
      "summary": "安装 frpsctl-web@.service 并 enable（需要 root）。",
      "web_view": "services"
    },
    {
      "group": "web",
      "name": "restart",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web service restart",
      "readonly": false,
      "summary": "重启 Web 管理台（systemd，需要 root）——口令轮换等改动重启后生效。",
      "web_view": "services"
    },
    {
      "group": "web",
      "name": "start",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web service start",
      "readonly": false,
      "summary": "启动 Web 管理台（systemd，需要 root）。",
      "web_view": "services"
    },
    {
      "group": "web",
      "name": "status",
      "needs_root": false,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web service status",
      "readonly": true,
      "summary": "显示 Web 管理台的 systemd 托管状态（active 与 enabled 分开报告）。",
      "web_view": "services"
    },
    {
      "group": "web",
      "name": "stop",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web service stop",
      "readonly": false,
      "summary": "停止 Web 管理台（systemd，需要 root）。",
      "web_view": "services"
    },
    {
      "group": "web",
      "name": "uninstall",
      "needs_root": true,
      "needs_systemd": true,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web service uninstall",
      "readonly": false,
      "summary": "停用并删除 Web 管理台 unit 模板（需要 root）。口令文件保留。",
      "web_view": "services"
    },
    {
      "group": "web",
      "name": "start",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": null,
          "default_text": null,
          "help": "把逐请求日志写进日志文件",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "access_log",
          "opts": [
            "--access-log"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "允许绑定非回环地址（持久化到状态文件）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "allow_non_loopback",
          "opts": [
            "--allow-non-loopback"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "监听地址（默认复用上次参数，否则 127.0.0.1:8787）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "bind",
          "opts": [
            "--bind"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "暴露 /metrics（Prometheus）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "metrics",
          "opts": [
            "--metrics"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "登录口令（写入口令文件；不出现在进程命令行里）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "password",
          "opts": [
            "--password"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "口令文件（默认实例内 web-password，不存在则生成）",
          "is_flag": false,
          "kind": "opt",
          "multiple": false,
          "name": "password_file",
          "opts": [
            "--password-file"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        },
        {
          "default": null,
          "default_text": null,
          "help": "信任反向代理的 X-Forwarded-For（持久化）",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "trusted_proxy",
          "opts": [
            "--trusted-proxy"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web start",
      "readonly": false,
      "summary": "后台启动 Web 管理台（direct 模式；非 systemd 环境使用）。",
      "web_view": "services"
    },
    {
      "group": "web",
      "name": "status",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web status",
      "readonly": true,
      "summary": "Web 管理台的托管状态（systemd / direct / 未运行 的统一视图）。",
      "web_view": "services"
    },
    {
      "group": "web",
      "name": "stop",
      "needs_root": false,
      "needs_systemd": false,
      "params": [
        {
          "default": false,
          "default_text": "false",
          "help": "机器可读输出",
          "is_flag": true,
          "kind": "opt",
          "multiple": false,
          "name": "json_output",
          "opts": [
            "--json"
          ],
          "positional": false,
          "required": false,
          "secondary": []
        }
      ],
      "path": "web stop",
      "readonly": false,
      "summary": "停止后台运行的 Web 管理台（direct 模式；SIGTERM → 等待 → SIGKILL 兜底）。",
      "web_view": "services"
    }
  ],
  "env_vars": {
    "EDITOR": "`config edit` 使用的编辑器（`VISUAL` 次之，默认 vi）",
    "FRPSCTL_ADMIN_PASSWORD": "dashboard 口令（`--admin-password` 优先，其次配置文件）",
    "FRPSCTL_DATA_HOME": "数据根目录（`bin/` 与 `instances/` 的父目录）",
    "FRPSCTL_INSTALL_REF": "install.sh：源码 ref（tag/分支，默认 main）",
    "FRPSCTL_INSTALL_URL": "install.sh：源码 tarball 地址（镜像/离线内网）",
    "FRPSCTL_INSTANCE": "实例名（`--instance` 的默认值，默认 default）",
    "FRPSCTL_MIRROR": "下载镜像列表（逗号分隔；`install --mirror` 优先）",
    "FRPSCTL_PLUGIN_POLICY": "插件策略文件路径（`--policy` 优先）",
    "FRPSCTL_ROOT": "实例根目录（默认 `<data_home>/instances`）",
    "FRPSCTL_TEST_BINARY": "测试：真实 frps 路径（契约层）",
    "FRPSCTL_TEST_FRPC": "测试：真实 frpc 路径（插件契约层）",
    "FRPSCTL_TRACEBACK": "任何非空值：未分类错误时打印完整回溯",
    "FRPSCTL_WEB_PASSWORD": "Web 管理台口令（`--password` 优先）",
    "XDG_DATA_HOME": "XDG 数据目录（未设 `FRPSCTL_DATA_HOME` 时使用）"
  },
  "exit_codes": {
    "ADMIN_UNREACHABLE": 7,
    "ALREADY_RUNNING": 6,
    "BINARY": 4,
    "CONFIG_INVALID": 3,
    "NOT_RUNNING": 5,
    "OK": 0,
    "OWNERSHIP_CONFLICT": 11,
    "PERMISSION": 8,
    "ROLLED_BACK": 9,
    "STARTUP_FAILED": 10,
    "UNCLASSIFIED": 1,
    "UNHEALTHY": 12,
    "USAGE": 2
  },
  "frps_minimum": "0.70.0",
  "frps_reckoned": "0.71.0",
  "version": "0.3.5"
};
