# frpsctl

用 Python 把 [frp](https://github.com/fatedier/frp) 服务端（frps）包装成命令行工具。

> **设计哲学**：frps 是黑盒服务，Python 只做**配置翻译器、进程保镖、状态聚合器**——
> 转发逻辑一行都不碰，生命周期走进程管理，配置校验走官方 `verify`，状态采集走官方
> v2 Admin API。

完整设计见 [`frpsctl-设计方案.md`](frpsctl-设计方案.md)。

---

## 它解决什么

直接用官方 `frps` 管服务端，有四件事必须手工完成，且都容易出错：

1. **手写 TOML** —— 字段名是驼峰、嵌套结构，写错了要等启动才报错。
2. **配进程守护** —— `frps -c frps.toml` 前台运行，关掉 SSH 就断。
3. **看状态靠翻日志** —— 谁在线、跑了多少流量，终端里取数得另想办法。
4. **改配置必然重启** —— frps **没有热重载**，改一个端口就要停服重启。

`frpsctl` 把这四件事收敛为一组命令，并遵守一条硬边界：**绝不重新实现 frp 已有的能力**。

| 能力 | 由谁提供 |
|------|---------|
| 配置合法性判定 | 官方 `frps verify -c`（唯一权威） |
| 状态、统计、代理列表 | 官方 v2 Admin API |
| 服务进程 | 官方 `frps` 二进制，或 systemd |
| 配置生成、进程编排、失败回滚、终端呈现 | **frpsctl** |

## 平台与版本要求

| 项 | 要求 |
|----|------|
| 操作系统 | **仅 Linux**（依赖 `/proc` 做进程身份校验、`flock` 做互斥、`fchmod` 做无权限窗口的原子写） |
| Python | ≥ 3.11（依赖标准库 `tomllib`） |
| frps | **≥ 0.70.0**（v2 Admin API 自 0.70.0 引入） |
| 目标版本 | 0.71.0（0.70.x 可用，但缺少已知远程 DoS 的修复） |

> 非 Linux 内核、或未挂载 `/proc` 的容器会被**直接拒绝**，而不是降级运行——
> 身份校验失效的代价是杀掉无关进程。

## 安装

```bash
pipx install frpsctl      # 或 pip install frpsctl
frpsctl install           # 下载官方 frps 二进制并强校验 sha256
frpsctl init              # 交互式生成安全基线配置
frpsctl start
frpsctl status
```

## 命令一览

```text
生命周期   install / init / verify / start / stop / restart / status / log
配置       config get|set|edit|diff|rollback
运维       service install|uninstall|status / doctor / kick
插件       plugin init|check|serve        # 多用户鉴权 + 端口白名单 + 审计
```

### 退出码（脚本化契约）

| 码 | 含义 | 码 | 含义 |
|----|------|----|------|
| 0 | 成功 | 6 | 实例已在运行 |
| 1 | 未分类错误 | 7 | dashboard 不可达 |
| 2 | 用法 / 参数错误 | 8 | 权限不足 |
| 3 | 配置非法 | 9 | 变更已自动回滚 |
| 4 | 二进制缺失 / 版本不受支持 | 10 | 启动即失败 |
| 5 | 实例未运行 | 11 | 进程所有权冲突 |

## 服务端插件（多用户鉴权 + 端口白名单）

frp 的服务端插件是一个 HTTP 回调：frps 在 `Login` / `NewProxy` 时 POST 一段
JSON，由插件决定放行还是拒绝。本工具用 Python 实现该回调。

```bash
frpsctl plugin init      # 生成策略模板（0600，fail-closed 默认）
frpsctl plugin check     # 离线校验 + 试算典型裁决
frpsctl plugin serve     # 启动（只允许绑回环）
```

frps 侧配置：

```toml
[[httpPlugins]]
name = "frpsctl"
addr = "http://127.0.0.1:8080"
path = "/handler"
ops  = ["Login", "NewProxy"]
```

frpc 侧声明身份（两个字段必须一致）：

```toml
user = "alice"
metadatas = { client_id = "alice" }
```

> ⚠️ **两条硬约束**
>
> 1. **插件是全部客户端登录的单点，且 fail-closed**——它挂掉 = 所有人登录不了。
>    生产环境必须用 systemd 守护并设 `Restart=always`。
> 2. **frp 的插件协议没有任何认证**（配置项只有 `name/addr/path/ops/tlsVerify`），
>    所以插件只允许绑回环。指向非回环地址会被 `frpsctl plugin serve` 拒绝，
>    `doctor` 也会报 ERROR。

策略文件是 JSON，支持按用户限制端口段、代理名通配、代理类型、代理数上限，
以及是否允许随机端口（默认不允许——否则白名单形同虚设）。每次裁决写入
`plugin-audit.jsonl`（异步落盘，不拖慢登录）。

`max_proxies` 的计数默认只在本进程内有效（重启归零）。要拿到权威计数，请在
策略里填 `admin_url`（dashboard 地址），插件会读 frps 自己的 `data.total`。

> **不提供"并发连接上限"**：它只能在 `NewUserConn` 上实施，而那落在每次用户
> 连接的关键路径上、错误只以 info 级记录、且回调内容里没有连接 id——无法可靠
> 统计。需要真并发限制请在 frpc 侧用连接池与限流解决。

## 设计要点

- **实例所有权显式化**：一个实例的进程任何时刻只有一个所有者（`systemd` 或
  `direct`），运行时探测，绝不混用。
- **配置文件是唯一真相**：`config set` 只对目标键做定点赋值，注释与排版原样保留，
  模型未覆盖的键（`allowPorts`、`httpPlugins`…）不会被悄悄丢掉。
- **变更即事务**：候选生成 → 权威校验 → 原子替换 → 重启 → 健康检查 → 失败自动回滚。
  frps 没有热重载，所以"改配置"和"重启"是同一件事。
- **三层健康**：L1 进程 / L2 控制面 / L3 插件。**只有 L1 ∧ L2 有回滚权**——
  插件抖动不该被误判成"这份配置有问题"。
- **不确定的一律拒绝**：进程身份不符、值含 `{{`、拿不到校验和、所有权冲突，
  全部以"拒绝 + 明确指引"收场，而不是尝试自愈。

## 开发

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                  # 单元 + 集成；契约层在缺二进制时自动 skip
.venv/bin/python -m frpsctl --help
```

契约测试（`tests/test_facts.py`）是设计文档中所有"事实"的自动化守卫：
frp 一旦改动 API 字段或标志，CI 会先于用户发现。

## 许可

MIT。frps 二进制由 `frpsctl install` 按需下载，遵循其自身的 Apache-2.0 许可，
不随本包分发。
