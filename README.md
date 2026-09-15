# frpsctl

把 [frp](https://github.com/fatedier/frp) 服务端（frps）包装成命令行工具。

> **设计哲学**：frps 是黑盒服务，Python 只做**配置翻译器、进程保镖、状态聚合器**。
> 转发逻辑一行都不碰；配置合法性走官方 `verify`；状态采集走官方 v2 Admin API；
> 生命周期走进程管理。

完整设计（含每条事实的源码依据与复现命令）见 [`frpsctl-设计方案.md`](frpsctl-设计方案.md)。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [环境要求](#环境要求)
- [安装](#安装)
- [五分钟上手](#五分钟上手)
- [日常使用](#日常使用)
  - [看状态](#看状态)
  - [改配置](#改配置)
  - [看日志](#看日志)
  - [停下来](#停下来)
  - [体检](#体检)
  - [下线代理](#下线代理)
- [多实例](#多实例)
- [服务端插件（多用户鉴权 + 端口白名单）](#服务端插件多用户鉴权-端口白名单)
- [用 systemd 托管](#用-systemd-托管)
- [升级 frps](#升级-frps)
- [退出码（脚本化契约）](#退出码脚本化契约)
- [环境变量与全局选项](#环境变量与全局选项)
- [目录布局](#目录布局)
- [备份与回滚](#备份与回滚)
- [排障](#排障)
- [安全说明](#安全说明)
- [开发](#开发)
- [已知边界](#已知边界)

---

## 它解决什么问题

直接用官方 `frps` 管服务端，有四件事必须手工完成，且都容易出错：

| 痛点 | frpsctl 的做法 |
|------|---------------|
| **手写 TOML**：字段是驼峰、嵌套结构、取值范围分散在文档各处，写错了要等启动才报错 | `init` 交互式生成安全基线配置；`config set` 改单键 |
| **配进程守护**：`frps -c frps.toml` 前台运行，关掉 SSH 就断 | `start` 派生后台进程（脱离会话），管 pid、优雅停止、开机自启 |
| **看状态靠翻日志**：谁在线、有几个代理、跑了多少流量 | `status` 一条命令聚合，支持 `--json` |
| **改配置必然重启**：frps **没有热重载**，改一个端口就要停服 | `config set` 走**带回滚的事务**：校验 → 备份 → 替换 → 重启 → 失败自动恢复 |

一条硬边界：**绝不重新实现 frp 已有的能力**。

| 能力 | 由谁提供 |
|------|---------|
| 配置合法性判定 | 官方 `frps verify -c`（唯一权威） |
| 状态、统计、代理列表 | 官方 v2 Admin API |
| 服务进程 | 官方 `frps` 二进制，或 systemd |
| 配置生成、进程编排、失败回滚、终端呈现 | **frpsctl** |

---

## 环境要求

| 项 | 要求 | 为什么 |
|----|------|-------|
| 操作系统 | **仅 Linux** | 进程身份校验依赖 `/proc/<pid>/stat`；互斥用 `flock`；原子写用 `fchmod` |
| Python | **≥ 3.11** | 依赖标准库 `tomllib` |
| frps | **≥ 0.70.0** | v2 Admin API 自 0.70.0 引入，本工具只用 v2 |
| 建议版本 | 0.71.0 | 0.70.x 可用，但缺少一个已知远程 DoS 的修复（`start`/`doctor` 会告警） |

非 Linux 内核、或未挂载 `/proc` 的容器会被**直接拒绝启动**，而不是降级——
身份校验失效的代价是杀掉无关进程。

---

## 安装

```bash
# 1) 装 Python 侧
pipx install frpsctl          # 推荐；或 pip install frpsctl

# 2) 装 frps 二进制（从官方发布页下载，sha256 强校验）
frpsctl install

# 3) 生成配置
frpsctl init
```

`install` 会做这些事：

```console
$ frpsctl install
frps 0.71.0 → /home/u/.local/share/frpsctl/bin/frps-0.71.0
当前版本软链 → /home/u/.local/share/frpsctl/bin/frps

注意：换链**不影响正在运行的进程**（Linux 上可执行映像已绑定 inode），
      只影响下一次 start。运行 `frpsctl status` 可对比两个版本。
```

- 下载地址可被镜像替换，但**信任锚是官方校验和文件**（`frp_sha256_checksums.txt`）。
- **拿不到校验和就拒绝安装**（fail-closed）。确有需要可 `--insecure` 跳过，风险自负。
- 低于 0.70.0 的版本直接拒绝——装了也用不了。

### 从源码安装（一键脚本）

```bash
git clone https://github.com/ThzxxArt/frpsctl.git
cd frpsctl
./install.sh
```

脚本做的事：建一个独立 venv → 装依赖 → 把 `frpsctl` 注册到 `~/.local/bin` → 自检。
**不需要 pipx、不需要 uv、不需要 sudo**（venv 是标准库自带的）。

```console
$ ./install.sh
检查运行环境
 ✓ 操作系统：Linux
 ✓ Python：Python 3.13.5（/usr/bin/python3）
 ✓ 源码目录：/mnt/d/CodeWorkspace/frpsctl
 ✓ venv 模块可用

创建虚拟环境
 ✓ 已创建：/home/u/.local/share/frpsctl-src/venv
安装 frpsctl 及其依赖
 ✓ 依赖就绪（typer / pydantic / tomlkit / httpx）
注册全局命令
 ✓ 已注册：/home/u/.local/bin/frpsctl
自检
 ✓ 命令可用：frpsctl 0.1.0

frpsctl 安装完成
```

| 选项 | 作用 |
|------|------|
| （无） | 装到 `~/.local`（命令 → `~/.local/bin/frpsctl`） |
| `--system` | 装到 `/usr/local`（需要 `sudo`） |
| `--prefix DIR` | 自定义前缀 |
| `--uninstall` | 卸载（删 venv 与命令，**不动实例数据**） |
| `--no-verify` | 跳过安装后自检 |

**反复运行即为升级**（会重新装依赖并重写命令）。源码用 `-e` 方式安装，因此改完
源码无需重装，命令立即生效。

> 脚本**不下载 frps 二进制**——那是 `frpsctl install` 的职责（需要网络与校验和，
> 且要写入用户数据目录）。安装器只负责让 `frpsctl` 这个命令可用。

### 从源码安装（手动）

```bash
git clone https://github.com/ThzxxArt/frpsctl.git && cd frpsctl
uv venv && uv pip install -e ".[dev]"
.venv/bin/frpsctl --version          # 直接用 venv 里的命令，不注册全局

# 或让 pip 直接装到用户环境
pip install --user -e .
```

---

## 五分钟上手

```console
$ frpsctl init
将生成一份安全基线的 frps 配置。直接回车使用方括号中的默认值。

控制端口 bindPort [7000]: 
dashboard 端口（0 = 不启用，将失去状态聚合能力） [7500]: 
允许客户端申请的端口段（如 6000-6100；留空 = 不限，不推荐）: 6000-6100

已生成 /home/u/.local/share/frpsctl/instances/default/frps.toml（权限 0600）

  auth.token          ZuqUzY4Huv588VJzEspC-0GZTiXl6dY9
  webServer.user      admin
  webServer.password  NIY0Z-zd8dmeTg59YXbITe9z

⚠ 上面三项只会显示这一次，配置里已写入（文件权限 0600）。

下一步：frpsctl verify && frpsctl start
```

**把这三项记下来**——`auth.token` 要填到每个 frpc 客户端，dashboard 口令用于
`kick` 等操作。

```console
$ frpsctl verify
/home/u/.local/share/frpsctl/instances/default/frps.toml 校验通过（frps 0.71.0，标志：--strict_config=true）

$ frpsctl start
已启动：pid 179841，frps 0.71.0
health   : L1 process ok  L2 control ok  L3 plugin skipped

$ frpsctl status
instance : default            owner : direct
state    : RUNNING (pid 179841, up 1s)
binary   : frps 0.71.0
config   : /home/u/.local/share/frpsctl/instances/default/frps.toml (0600)
dashboard: 127.0.0.1:7500 (auth: on)
health   : L1 process ok  L2 control ok  L3 plugin skipped
clients  : 0 online
traffic  : in 0 B / out 0 B  (conns now 0)
```

`init` 生成的配置长这样（每条安全项旁边都写了它为什么在那儿）：

```toml
# frps 配置 —— 由 frpsctl init 生成
# 权限已设为 0600：文件内含 auth.token 与 dashboard 口令。

# ── 顶层键（必须写在任何 [table] 之前）─────────────────────
bindAddr = "0.0.0.0"
bindPort = 7000

# 单客户端可申请的端口数上限。frp 默认为 0（不限），
# 不设的话单个客户端就能把端口耗尽。
maxPortsPerClient = 20

# 端口白名单：只允许客户端申请这些端口 / 端口段。
# 不设的话，任何持有 token 的客户端都能申请任意端口。
allowPorts = [
  { start = 6000, end = 6100 },
]

[auth]
# 为空表示不校验客户端 token（等于没有客户端鉴权）。
token = "……"

[webServer]
# dashboard 只有 Basic Auth 一层防护，因此默认只监听本机。
# 改成 0.0.0.0 之前请确认口令已设。
addr = "127.0.0.1"
port = 7500
user = "admin"
password = "……"

[transport.tls]
# 拒绝明文 frpc 连接（frp 默认是 false）。
force = true

[log]
to = "./frps.log"
level = "info"
maxDays = 7
```

frpc 侧对应配置：

```toml
serverAddr = "your-server"
serverPort = 7000
auth.token = "上面那个 auth.token"

[[proxies]]
name = "my-ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 6000        # 必须落在 allowPorts 范围内
```

> ⚠️ **TOML 位置纪律**：`allowPorts`、`maxPortsPerClient` 这类**顶层键必须写在任何
> `[table]` 之前**。写错了它们会变成那张表的子键，而 frps 的报错是极具误导性的
> `unknown field "allowPorts"`——看起来像键名错了，实际是位置错了。

---

## 日常使用

### 看状态

```bash
frpsctl status              # 人读
frpsctl status --json       # 机器可读（前后位置都行：status --json / --json status）
frpsctl status --watch      # 持续刷新
```

`status` **永远以退出码 0 结束**（除非参数写错）——它的职责是回答"现在什么情况"，
而不是失败。异常情况会如实报告：

```console
$ frpsctl status            # 实例没在跑
instance : default            owner : none
state    : STOPPED
config   : /home/u/.local/share/frpsctl/instances/default/frps.toml (0600)
dashboard: 127.0.0.1:7500 (auth: on)
```

```console
$ frpsctl status --json
{
  "instance": "default",
  "owner": "direct",
  "state": "RUNNING",
  "pid": 179910,
  "uptime_seconds": 1.72,
  "binary_version": "0.71.0",
  "disk_version": "0.71.0",
  "config_mode": "0600",
  "health": {
    "l1_process": "ok",
    "l2_control": "ok",
    "l3_plugin": "skipped",
    "detail": "/healthz 200, 2ms"
  },
  "clients": 0,
  "proxy_type_counts": {},
  "proxy_total": null
}
```

**三个健康层，含义不同**：

| 层 | 探针 | 回答的问题 |
|----|------|-----------|
| L1 进程 | pid + 启动时刻 + 命令行三重校验 | 进程还在，且**确实是我们的** |
| L2 控制面 | `GET /healthz`（免认证） | frp 的 HTTP 服务在正常应答（`webServer.port = 0` 时为 `skipped`） |
| L3 插件面 | 对 `httpPlugins[].addr` 做 TCP 探测 | 登录链路是否可能成功（未配插件时为 `skipped`） |

> L3 失败**不改变退出码**，但会显著告警——因为插件是 fail-closed 的：插件不可达
> 意味着**所有客户端都无法登录**。

`owner` 字段说明"谁在管这个进程"，永远无歧义：

| owner | 含义 |
|-------|------|
| `direct` | 由 frpsctl 直接托管（state.json 是权威） |
| `systemd` | 由 systemd 托管，`start`/`stop`/`restart` 委托 systemctl |
| `none` | 没有进程在跑，也没有 unit |

### 改配置

frps 没有热重载，所以"改配置"和"重启"是同一件事。`config set` 把它实现为一次
**带回滚的事务**：

```console
$ frpsctl config set maxPortsPerClient 30
--- a/frps.toml
+++ b/frps.toml
@@ -7,7 +7,7 @@
 
 # 单客户端可申请的端口数上限。frp 默认为 0（不限），
 # 不设的话单个客户端就能把端口耗尽。
-maxPortsPerClient = 20
+maxPortsPerClient = 30
 
 # 端口白名单：只允许客户端申请这些端口 / 端口段。
 # 不设的话，任何持有 token 的客户端都能申请任意端口。

✓ 已写入并重启，健康检查通过
```

注意 diff 里**注释和排版都原样保留**——`config set` 只对目标键做定点赋值，
文件其余字节完全不变。模型没覆盖的键（`allowPorts`、`httpPlugins`…）也不会被丢掉。

事务的 9 个步骤：

```
取锁 → 内存定点补丁 → 语义校验 → 官方 verify → 备份 → 原子替换
     → 重启 → 健康检查 → 失败自动回滚（退出码 9，并如实告知回滚结果）
```

常用变体：

```bash
frpsctl config set bindPort 8000 --no-restart   # 只写不重启（输出会提示"尚未生效"）
frpsctl config get bindPort                     # 读单键
frpsctl config get auth                         # 读整张表（机密自动打码）
frpsctl config get auth.token --reveal          # 需要看原值时显式索取
frpsctl config edit                             # 用 $EDITOR 改，保存后走同一闭环
frpsctl config diff                             # 当前 vs 上一份快照
frpsctl config rollback                         # 回滚到上一份
frpsctl config rollback 3                       # 回滚到 3 份之前
```

**机密保护**：`config get` 默认打码（`SU***56` 形式，保留首尾便于核对是不是同一个
值），`--json` 与所有 diff 输出同样打码。要看明文必须 `--reveal`。

**拒绝危险组合**：`config set` 与 `doctor` 双重拦截"dashboard 绑非回环 **且**
user/password 都为空"。frp 在两者同时为空时**完全不鉴权**（不是"要求登录"），
因此那种配置等于把状态读取与代理下线的权限交给网络上任何人。

```console
$ frpsctl config set webServer.password '""'      # 先清空口令（此时仍只监听回环，安全）
$ frpsctl config set webServer.addr '"0.0.0.0"'   # 这一步会让它变成完全无鉴权 → 被拒绝
错误：拒绝写入危险配置：webServer 绑定非回环地址 0.0.0.0:7500，且 user/password 均为空
      = **完全不鉴权**（任何人都能读取全部状态并下线任意代理）
提示：请先设置 webServer.user 与 webServer.password，或把 webServer.addr 改回 127.0.0.1
```

反向也成立：**只要口令非空，绑非回环是允许的**（远程看 dashboard 是常见需求，
此时有 Basic Auth 保护）。拦截的是"无鉴权 + 对外暴露"这个组合本身，不是对外暴露。

### 看日志

```bash
frpsctl log                 # 最近 100 行
frpsctl log -n 500          # 最近 500 行
frpsctl log -f              # 持续跟踪（tail -f）
```

日志路径取自配置里的 `log.to`（相对路径按实例目录解析）。frpsctl **不写**这个
文件——它由 frp 自己写并按天轮转。多一个写入者会和轮转互相破坏。

### 停下来

```bash
frpsctl stop                # SIGTERM → 轮询确认 → 超时 SIGKILL
frpsctl stop --timeout 3    # 缩短等待
frpsctl stop --force        # 直接 SIGKILL
frpsctl restart             # stop → start
```

> frps **没有信号处理器**，SIGTERM 即进程立即终止，不存在"存量连接收尾"。
> SIGKILL 兜底是为了应对卡死，不是为了"更彻底"。

**安全特性**：停止前会做三重身份校验（pid 存活 + 启动时刻 + 命令行）。任何一项
对不上就**拒绝停止**（退出码 11），绝不冒险 kill 一个可能无关的进程：

```console
$ frpsctl stop
错误：pid 12345 存活但身份校验不通过，拒绝停止
提示：该 pid 可能已被复用为无关进程。确认后手工删除 …/state.json 即可恢复
```

### 体检

```console
$ frpsctl doctor
[INFO ] 二进制版本: frps 0.71.0 受支持
[INFO ] 配置校验: semantic + frps verify 均通过（标志：--strict_config=true）
[INFO ] 配置文件权限: 0600 正常
[INFO ] 端口可绑定性: bindPort = 7000 已被占用（本实例正在运行，属正常）
[INFO ] 进程所有权: 由 frpsctl 直接托管（direct）

体检通过
```

检查项与严重度：

| 检查 | 级别 | 说明 |
|------|------|------|
| 二进制存在 / 可执行 / 版本 | ERROR | `< 0.70.0` 拒绝；`0.70.x` 提示缺 DoS 修复 |
| 配置可解析 + `frps verify` | ERROR | 双保险 |
| 配置文件权限 | ERROR / WARN | 含 token 却非 0600 → ERROR |
| dashboard 暴露面 | ERROR | 绑非回环 **且** 口令为空 = 完全不鉴权 |
| dashboard 弱口令 | WARN | 口令为空，或 admin/admin |
| `transport.tls.force` | WARN | 未开启时可接受明文 frpc |
| `allowPorts` / `maxPortsPerClient` | WARN | 未设置时端口可被任意申请 |
| 端口可绑定性 | ERROR | 探测各监听端口；自己占着会识别为"正常" |
| `< 1024` 端口 | INFO | 提示需 `CAP_NET_BIND_SERVICE` |
| 进程所有权冲突 | ERROR | systemd 与 direct 同时成立 |
| 插件暴露面 | ERROR | 插件回调指向非回环（协议无认证） |
| 插件可达性 | WARN | 不可达时提示"客户端将无法登录" |

**有 ERROR 时退出码为 1**，可直接接进 CI 或监控。

### 下线代理

```bash
frpsctl kick my-ssh          # DELETE /api/proxies
```

需要 dashboard 启用（`webServer.port > 0`）；未启用时退出码 7。

---

## 多实例

一台机器跑多个 frps：用 `--instance` 或环境变量区分，各自有独立的配置、状态、
日志、锁。

```bash
frpsctl --instance web init --bind-port 7001 --dashboard-port 7501
frpsctl --instance web start
frpsctl --instance web status

FRPSCTL_INSTANCE=web frpsctl status    # 或长期用环境变量
```

优先级：`--instance` > `FRPSCTL_INSTANCE` > `default`。

服务端场景可把实例根目录放到 `/etc`：

```bash
frpsctl --root /etc/frps/instances --instance web init
```

---

## 服务端插件（多用户鉴权 + 端口白名单）

frp 的服务端插件是一个 HTTP 回调：frps 在 `Login` / `NewProxy` 等事件发生时 POST
一段 JSON，由插件决定放行还是拒绝。本工具用 Python（标准库）实现该回调。

```bash
frpsctl plugin init      # 生成策略模板（0600，默认 fail-closed）
frpsctl plugin check     # 离线校验 + 试算典型裁决
frpsctl plugin serve     # 启动（只允许绑回环）
```

`plugin check` 让你在部署前就看到"策略会怎么判"：

```console
$ frpsctl plugin check
策略文件：/home/u/.local/share/frpsctl/instances/default/plugin-policy.json
  用户数：1（未列出的用户一律拒绝）
  客户端身份校验：开启
  审计：写入 plugin-audit.jsonl
  配额计数来源：（未配置 admin_url，配额计数不准确 ⚠）
    - alice: 端口 6000-6010，最多 5 个代理

典型裁决试算：
  [允许] Login alice
  [允许] NewProxy alice 申请 6000（在许可范围内）
  [拒绝] NewProxy alice 申请 1（预期越界） — 端口 1 不在用户 'alice' 的许可范围（允许：6000-6010）
  [拒绝] Login __nobody__（未列出） — 未知用户 '__nobody__'

策略校验通过
```

### 策略文件

`plugin init` 生成 `<实例目录>/plugin-policy.json`（可用 `--policy` 或
`FRPSCTL_PLUGIN_POLICY` 指定）：

```json
{
  "allow_unknown_user": false,
  "require_client_id": true,
  "audit": { "enabled": true, "path": "./plugin-audit.jsonl" },
  "admin_url": "http://127.0.0.1:7500",
  "users": {
    "alice": {
      "allowed_ports": ["6000-6010"],
      "allow_random_port": false,
      "allowed_proxy_types": ["tcp", "udp"],
      "allowed_proxy_names": ["alice-*"],
      "max_proxies": 5
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `allow_unknown_user` | 默认 `false`：未列出的用户一律拒绝。开启等于不做鉴权 |
| `require_client_id` | 默认 `true`：frpc 必须用 `metadatas.client_id` 声明身份，且与 `user` 一致 |
| `allowed_ports` | 端口白名单，支持 `"6000"` 与 `"6000-6100"` |
| `allow_random_port` | 默认 `false`：不允许 `remotePort = 0`（否则白名单形同虚设） |
| `allowed_proxy_names` | 代理名通配（`alice-*`）。不提升安全性，用于防止用户互相抢占名字 |
| `max_proxies` | 每用户代理数上限（0 = 不限） |
| `admin_url` | 填了它，配额计数走 dashboard 的**权威**统计；不填只在插件进程内计数 |

### frps 侧配置

```toml
[[httpPlugins]]
name = "frpsctl"
addr = "http://127.0.0.1:8080"
path = "/handler"
ops  = ["Login", "NewProxy"]
```

`ops` 至少要有 `Login` 与 `NewProxy`：前者做鉴权，后者做端口与配额治理。

### frpc 侧配置

```toml
serverAddr = "your-server"
serverPort = 7000
user = "alice"
metadatas = { client_id = "alice" }     # 必须与 user 一致
auth.token = "……"

[[proxies]]
name = "alice-web"
type = "tcp"
localPort = 8080
remotePort = 6005                        # 必须在自己被允许的范围内
```

### 审计

每次裁决写入 JSONL（默认 `./plugin-audit.jsonl`，可用 `audit.path` 改）：

```console
$ tail -1 plugin-audit.jsonl
{"at":"2026-09-15T21:33:36","op":"NewProxy","user":"alice","decision":"deny",
 "reason":"frpsctl-plugin: 端口 9999 不在用户 'alice' 的许可范围（允许：6000-6010）",
 "proxy_name":"alice-web","proxy_type":"tcp","remote_port":9999,
 "elapsed_ms":0.03,"quota_source":"dashboard"}
```

审计是**异步**的：`record()` 只入队（实测单次裁决 0.02–0.03 ms），后台线程刷盘。
磁盘不可用时记录退回缓冲，**绝不让登录链路失败**——服务可用性优先于审计完整性。

> ⚠️ **两条硬约束**
>
> 1. **插件是全部客户端登录的单点，且 fail-closed**——它挂掉 = 所有人登录不了。
>    生产环境必须用 systemd 守护并设 `Restart=always`。
> 2. **frp 的插件协议没有任何认证**（配置项只有 `name/addr/path/ops/tlsVerify`），
>    所以插件只允许绑回环。指向非回环会被 `plugin serve` 拒绝，`doctor` 也报 ERROR。

---

## 用 systemd 托管

```bash
sudo frpsctl service install      # 安装 frps@.service 模板并 enable
sudo frpsctl service status       # 查看托管状态
sudo frpsctl service uninstall    # 解除托管
```

安装后 `owner` 变为 `systemd`，`start` / `stop` / `restart` 全部**委托 systemctl**，
pid 文件不再参与任何判定。

渲染出的 unit（`/etc/systemd/system/frps@.service`）：

```ini
[Unit]
Description=frps service (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=frps
Group=frps
ExecStart=/usr/local/bin/frps -c /etc/frps/instances/%i/frps.toml
WorkingDirectory=/etc/frps/instances/%i
Restart=on-failure
RestartSec=2
LimitNOFILE=65535

# 若配置使用 <1024 端口（例如 vhostHTTPPort = 80）
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# 加固
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/log/frps

[Install]
WantedBy=multi-user.target
```

多实例天然对齐：实例名就是 systemd 的 `%i`（`frps@web.service`）。

> ⚠️ unit 的 `ExecStart` 写的是**具体二进制路径**，因此 `install` 换版本后需要
> `systemctl restart` 才生效（软链换向不影响已加载的 unit）。

---

## 升级 frps

```bash
frpsctl install --version 0.72.0                    # 下载 + 校验 + 落盘 + 换软链
frpsctl install --version 0.72.0 --only-download    # 只落盘，稍后统一切换
frpsctl install --force                             # 同版本重新下载
```

**换软链不影响正在运行的进程**（Linux 上可执行映像在 `execve` 后已绑定 inode），
只影响下一次 `start`。因此 `status` 会同时显示两个版本：

```console
binary   : frps 0.70.0 (running) → 0.71.0 (on disk, restart to apply)
```

旧版本文件不会自动删除，`bin/frps-<old>` 就是回退的抓手。要回退：

```bash
frpsctl install --version 0.71.0 && frpsctl restart
```

---

## 退出码（脚本化契约）

| 码 | 含义 | 典型触发 |
|----|------|---------|
| 0 | 成功 | |
| 1 | 未分类错误 / `doctor` 发现 ERROR | 意外异常；或体检不通过 |
| 2 | 用法 / 参数错误 | 未知选项、缺子命令 |
| 3 | 配置非法 | 语义校验或 `frps verify` 拒绝；键不存在；状态文件损坏 |
| 4 | 二进制缺失 / 不可执行 / 版本不受支持 | 未 install，或版本 `< 0.70.0` |
| 5 | 实例未运行 | `stop` 时无进程 |
| 6 | 实例已在运行 | 重复 `start` |
| 7 | dashboard 不可达 / 未启用 | `kick` 时 `webServer.port = 0`；v2 API 缺失 |
| 8 | 权限不足 | 需要 root 的操作 |
| 9 | 变更已自动回滚 | 配置写入后启动/健康检查失败，已恢复上一版 |
| 10 | 启动失败 / 进程停不下来 | 启动即退出（附 frp 原始报错）；SIGKILL 后仍存在 |
| 11 | 进程所有权冲突 | 身份校验不通过；systemd 与 direct 混用；锁被占用 |

脚本里应当据此分支：

```bash
frpsctl start
case $? in
  0)  echo "启动成功" ;;
  6)  echo "已在运行，跳过" ;;
  4)  echo "需要先 frpsctl install" >&2; exit 4 ;;
  10) echo "启动失败，查看 frpsctl log" >&2; exit 10 ;;
  *)  exit 1 ;;
esac
```

---

## 环境变量与全局选项

| 环境变量 | 作用 |
|---------|------|
| `FRPSCTL_INSTANCE` | 默认实例名 |
| `FRPSCTL_ROOT` | 实例根目录（默认 `~/.local/share/frpsctl/instances`） |
| `FRPSCTL_DATA_HOME` | 数据根目录（默认 `$XDG_DATA_HOME/frpsctl`） |
| `FRPSCTL_ADMIN_PASSWORD` | dashboard 口令（优先于配置文件） |
| `FRPSCTL_PLUGIN_POLICY` | 插件策略文件路径 |
| `FRPSCTL_TRACEBACK` | 设为 `1` 时打印完整回溯（排查未分类错误用） |

全局选项（写在子命令前后都可以）：

```
--instance, -i NAME    实例名
--root PATH            实例根目录
--config PATH          直接指定配置文件
--binary PATH          直接指定 frps 二进制
--json                 机器可读输出
--admin-password       dashboard 口令
--yes, -y              跳过交互确认
--verbose, -v          详细输出
--version              frpsctl 版本
```

凭据优先级：`--admin-password` > `FRPSCTL_ADMIN_PASSWORD` > 配置文件里的
`webServer.password`。

---

## 目录布局

```
~/.local/share/frpsctl/
├── bin/                          # 全局共享：二进制按版本并存
│   ├── frps-0.71.0
│   └── frps -> frps-0.71.0       # 当前版本软链（install 唯一改动的对象）
└── instances/
    └── default/                  # 实例私有（0700）
        ├── frps.toml             # 0600，含 token 与 dashboard 口令
        ├── state.json            # 0600，所有权判定的权威来源
        ├── frps.pid              # 人类可读副本，不参与任何判定
        ├── frps.log              # 由 frp 自己写并轮转
        ├── .lock                 # flock 互斥
        ├── plugin-policy.json    # 插件策略（若使用插件）
        ├── plugin-audit.jsonl    # 插件审计
        ├── config-history/       # 最近 10 份配置快照（0600）+ meta.json
        │   └── 0001-20260915-211140/
        └── startup/              # 最近 3 份启动日志（0600，诊断"启动即退出"）
```

`state.json` 记录 `{pid, start_time, binary, config, version, started_at, owner}`——
其中 `start_time` 是识别 pid 复用的唯一依据，`binary` 存的是**真实路径**（不是软链，
否则换版本后身份校验会失配）。

---

## 备份与回滚

每次 `config set` / `config edit` / `config rollback` 都会先把当前配置存进
`config-history/NNNN-<时间戳>/`（保留最近 10 份，含 `meta.json` 记录操作与结果）。

```bash
frpsctl config diff              # 当前 vs 上一份
frpsctl config diff --steps 3    # 当前 vs 第 3 新的一份
frpsctl config rollback          # 回滚到上一份（并重启）
frpsctl config rollback 2        # 回滚到 2 份之前
```

回滚**复用同一闭环**（校验 → 替换 → 重启 → 失败再回滚），而不是简单 `cp` 覆盖——
否则回滚本身会把服务搞坏。回滚完成后会如实汇报结果：配置与服务是否都恢复了。

---

## 排障

### 启动失败

```console
$ frpsctl start
错误：frps 启动后立即退出
提示：frps: listen tcp :7000: bind: address already in use
```

`提示` 那一行是 **frp 的原始报错**，通常已经说清问题。进一步排查：

```bash
frpsctl log                                            # frp 自己的日志
ls ~/.local/share/frpsctl/instances/default/startup/   # 启动阶段日志（保留 3 份）
```

常见原因：

| 报错 | 原因 | 处置 |
|------|------|------|
| `bind: address already in use` | 端口被占 | 换端口，或找出占用者 |
| `unknown field "xxx"` | 键名写错，**或顶层键写在了 `[table]` 之后** | 检查位置 |
| `open xxx: no such file` | 证书/文件路径不存在 | 修正路径 |
| 无任何输出 | 二进制不可执行 | 跑 `frpsctl verify`、`frpsctl doctor` |

### `status` 显示 `FOREIGN`

```console
state    : FOREIGN
```

含义：`state.json` 里的 pid 存活，但**身份校验不通过**（启动时刻或命令行对不上），
通常意味着那个 pid 已被系统复用给无关进程。此时 `start` / `stop` 都会**拒绝执行**
（退出码 11）——这是刻意的安全设计：宁可停不下来，也不杀错进程。

处置：确认没有 frps 在跑，然后删除 `state.json`：

```bash
pgrep -af frps                  # 确认
rm ~/.local/share/frpsctl/instances/default/state.json
```

### `status` 显示 `STALE`

`state.json` 存在但进程已退出。这不是错误，直接 `start` 即可（会自动清理陈旧状态）。

### 状态文件损坏

```console
$ frpsctl status
⚠ 状态文件已损坏：…/state.json
  无法判断进程归属，因此 stop/start/config set 都会拒绝执行。
  请确认没有 frps 在跑，然后删除该文件。
```

`status` 仍以退出码 0 结束（它必须能回答现状），但会显著告警。**工具不会**在
状态不可信时猜测——因为猜错的代价是杀掉无关进程。

### 锁被占用

```console
错误：另一个 frpsctl 进程正持有实例锁：…/.lock
```

说明另一个 frpsctl 正在操作同一实例。等它完成即可——锁随进程退出自动释放，
不会留下需要人工清理的陈旧锁文件。

### 配置校验超时

```console
错误：配置校验超时（30 秒）：…/frps verify 没有返回
```

说明二进制卡住或不可执行。手工跑一次确认：

```bash
/path/to/frps --strict_config=true verify -c /path/to/frps.toml
```

### 想看完整回溯

```bash
FRPSCTL_TRACEBACK=1 frpsctl status
```

---

## 安全说明

`init` 生成的默认配置直接站在安全侧，每条都对应一个已确认的 frp 行为：

| 项 | 默认值 | 理由 |
|----|-------|------|
| `webServer.addr` | `127.0.0.1` | dashboard 只有 Basic Auth 一层防护 |
| `webServer.user` / `password` | `admin` / 随机 24 字符 | **两者全空 = frp 完全不鉴权**（不是"要求登录"） |
| `transport.tls.force` | `true` | 拒绝明文 frpc 连接 |
| `allowPorts` | 交互引导填写 | 否则任何持有 token 的客户端都能申请任意端口 |
| `maxPortsPerClient` | `20` | frp 默认为 0（不限），单客户端可耗尽端口 |
| `auth.token` | 随机 32 字符 | 无 token 等于无客户端鉴权 |
| 配置文件权限 | `0600` | 内含 token 与口令；原子写落地，无权限窗口 |

其他保证：

1. **拒绝危险组合**：`config set` 与 `doctor` 双重拦截"dashboard 绑非回环 + 口令为空"。
2. **不回显机密**：口令与 token 不进日志、不进 `--json`、不进异常消息；
   `config get` 默认打码，diff 输出同样打码，配置快照与启动日志按 0600 落盘。
3. **拒绝模板语法**：写入含 `{{` 的值会被拒绝——frp 会对其做 `text/template` 渲染，
   放行等于让配置被悄悄改写。
4. **拒绝来路不明的二进制**：校验和不匹配即拒绝安装。
5. **不确定就拒绝**：进程身份不符、状态文件损坏、所有权冲突——一律报错并给出处置
   指引，而不是尝试自愈。

---

## 开发

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                       # 全部（契约层缺二进制时自动 skip）
.venv/bin/pytest -m "not contract"     # 快速回归
.venv/bin/ruff check src/ tests/       # 静态分析
```

### 测试分五层

| 层 | 文件 | 目标 |
|----|------|------|
| 单元 | `tests/test_units.py` | 进程原语、锁、原子写、无损补丁、标志构造 |
| 集成 | `tests/test_integration.py` | 生命周期与回滚（假 frps 驱动确定性故障） |
| CLI | `tests/test_cli.py` | 退出码契约、`--json` 形态、机密不外泄 |
| 契约 | `tests/test_facts.py` | **设计文档事实基线的自动化守卫**（需真 frps） |
| 故障注入 | `tests/test_faults.py` | 注入系统调用失败，验证异常路径的五项不变量 |
| 插件 | `tests/test_plugin.py` | 协议报文、裁决、审计、配额；含真 frpc 端到端契约 |

让契约层跑起来（需要真实二进制）：

```bash
frpsctl install --with-frpc
export FRPSCTL_TEST_BINARY=~/.local/share/frpsctl/bin/frps-0.71.0
export FRPSCTL_TEST_FRPC=~/.local/share/frpsctl/bin/frpc-0.71.0
.venv/bin/pytest tests/test_facts.py tests/test_plugin.py -m contract
```

> **契约层为什么重要**：它断言的是 frp 的**行为事实**（字段名、形状、退出码、
> 标志可用性）。frp 一旦改动这些，CI 会先于用户发现——而不是等某个用户报告
> "状态里的数字不对"。这类错误**不会报错，只会给出错误数字**。
>
> **故障注入层为什么重要**：异常路径是缺陷聚集区。它注入真实的系统调用失败
> （`os.replace`、`subprocess`、`httpx`…），验证五项不变量：契约内异常、无遗留
> 进程、无半截/含机密的文件、锁已释放、机密不外泄。

---

## 已知边界

以下是刻意**不做**的事，以及各自的原因。它们不是待办事项，而是能力边界——
提前写出来，好过让人踩到之后才发现：

| 边界 | 说明 |
|------|------|
| **只支持 Linux** | macOS / Windows 直接拒绝启动，不提供降级 |
| **只支持 frps ≥ 0.70.0** | 因为只用 v2 Admin API，不做 v1 降级 |
| **`restart` 没有 `--no-rollback`** | 重启不读配置，不存在"新旧版本"可比；自动回滚只属于配置变更路径 |
| **不做并发连接上限** | 它只能在 `NewUserConn` 上实施，而那落在每次用户连接的关键路径上、错误只以 info 级记录、且回调内容里没有连接 id。需要真并发限制请在 frpc 侧用连接池与限流 |
| **`max_proxies` 计数需配 `admin_url`** | 不配时只在插件进程内计数（重启归零、多实例各算各的），`plugin check` 会告警 |
| **frps 没有热重载** | 改配置必然重启，因此 `config set` 的设计目标就是"失败了要能退回去" |
| **systemd 模式下 pid 文件不参与判定** | 所有权委托 systemctl；`install` 换版本后需 `systemctl restart` |

---

## 许可

MIT（见 [LICENSE](LICENSE)）。

本仓库**不包含** frp 的源代码或二进制。`frpsctl install` 会按需从官方发布页下载
frps，该二进制遵循其自身的 **Apache-2.0** 许可，由使用者自行获取与遵守。
