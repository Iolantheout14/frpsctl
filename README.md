# frpsctl

**把 [frp](https://github.com/fatedier/frp) 服务端（frps）包装成命令行工具 + 内置 Web 管理台。**

CLI 负责精确控制与脚本化，Web 管理台负责可视化与日常操作——两者**共用同一套核心逻辑**，
同样的操作不会有两套行为。frps 二进制始终是官方原版：本工具只做
**配置翻译器、进程保镖、状态聚合器**，转发逻辑一行都不碰。

> 设计哲学：配置合法性走官方 `frps verify`；状态采集走官方 v2 Admin API；
> 生命周期走进程管理 / systemd。完整设计（含每条事实的源码依据与复现命令）见
> [`frpsctl-设计方案.md`](frpsctl-设计方案.md)。

---

## 目录

- [介绍](#介绍)
  - [它解决什么问题](#它解决什么问题)
  - [两副面孔，一套内核](#两副面孔一套内核)
  - [一条硬边界](#一条硬边界)
- [环境要求](#环境要求)
- [安装](#安装)
  - [方式一：一键安装（uv，无需 Python）（推荐）](#方式一一键安装uv无需-python推荐)
  - [方式二：pipx / pip（已有 Python ≥ 3.11）](#方式二pipx--pip已有-python--311)
  - [方式三：源码安装](#方式三源码安装)
  - [服务器没有 Python 怎么办](#服务器没有-python-怎么办)
  - [安装 frps 二进制](#安装-frps-二进制)
  - [验证安装](#验证安装)
  - [卸载](#卸载)
- [五分钟上手](#五分钟上手)
- [CLI 使用教程](#cli-使用教程)
  - [全局选项与命令总览](#全局选项与命令总览)
  - [教程 1：看状态](#教程-1看状态)
  - [教程 2：看客户端、代理与流量](#教程-2看客户端代理与流量)
  - [教程 3：改配置（核心事务）](#教程-3改配置核心事务)
  - [教程 4：看日志](#教程-4看日志)
  - [教程 5：启停](#教程-5启停)
  - [教程 6：体检与巡检](#教程-6体检与巡检)
  - [教程 7：清理离线记录](#教程-7清理离线记录)
- [Web 管理台教程](#web-管理台教程)
  - [启动与登录](#启动与登录)
  - [仪表盘](#仪表盘)
  - [配置编辑：预览、应用与回滚](#配置编辑预览应用与回滚)
  - [口令、部署与安全](#口令部署与安全)
  - [命令参考、会话与审计过滤（v0.3.5）](#命令参考会话与审计过滤v035)
  - [CLI 与 Web 的对应关系](#cli-与-web-的对应关系)
- [进阶用法](#进阶用法)
  - [多实例](#多实例)
  - [服务端插件（多用户鉴权 + 端口白名单）](#服务端插件多用户鉴权--端口白名单)
  - [用 systemd 托管 frps](#用-systemd-托管-frps)
  - [用 systemd 托管 Web 管理台与插件](#用-systemd-托管-web-管理台与插件)
  - [升级 frps](#升级-frps)
- [参考手册](#参考手册)
  - [退出码（脚本化契约）](#退出码脚本化契约)
  - [环境变量与全局选项](#环境变量与全局选项)
  - [目录布局](#目录布局)
  - [备份与回滚](#备份与回滚)
- [排障](#排障)
- [安全说明](#安全说明)
- [开发](#开发)
- [已知边界](#已知边界)
- [许可](#许可)

---

## 介绍

### 它解决什么问题

直接用官方 `frps` 管服务端，有四件事必须手工完成，且都容易出错：

| 痛点 | frpsctl 的做法 |
|------|---------------|
| **手写 TOML**：字段是驼峰、嵌套结构、取值范围分散在文档各处，写错了要等启动才报错 | `init` 交互式生成安全基线配置；`config set` 改单键，注释与排版原样保留 |
| **配进程守护**：`frps -c frps.toml` 前台运行，关掉 SSH 就断 | `start` 派生后台进程（脱离会话），管 pid、优雅停止、开机自启（systemd） |
| **看状态靠翻日志**：谁在线、有几个代理、跑了多少流量 | `status` 一条命令聚合，支持 `--json`；Web 仪表盘自动刷新 |
| **改配置必然重启**：frps **没有热重载**，改一个端口就要停服 | `config set` 走**带回滚的事务**：校验 → 备份 → 替换 → 重启 → 失败自动恢复 |

### 两副面孔，一套内核

**CLI**——精确、可脚本化、可接 CI：

| 分类 | 命令 |
|------|------|
| 二进制与配置 | `install` / `init` / `verify` |
| 生命周期 | `start` / `stop` / `restart` / `status` / `log` |
| 配置子命令 | `config get` / `set` / `apply`（多键一次事务） / `unset` / `edit` / `list` / `diff` / `rollback`（`set` 支持 `--dry-run` / `--stdin` / `--prompt`） |
| 观测与运维 | `clients` / `proxies` / `traffic` / `instances` / `doctor` / `prune` / `capabilities`（能力清单） |
| 卸载 | `uninstall`（`--all` / `--keep-data` / `--keep-bin` / `--force`） |
| systemd 集成 | `service install` / `uninstall` / `status` / `logs` |
| 服务端插件 | `plugin init` / `check` / `serve`、`plugin start|stop|restart|status`（后台，非 systemd）、`plugin user set|remove|list`、`plugin audit tail|stats`、`plugin config list|set`、`plugin service install|uninstall|start|stop|restart|status` |
| Web 管理台 | `web serve`（可选 `--metrics`）、`web start|stop|restart|status`（后台，非 systemd）、`web service install|uninstall|start|stop|restart|status`、`web password show|set`、`web audit tail|stats` |

**Web 管理台**——浏览器中的同等能力（`web serve` 启动，默认只绑回环）：

| 区域 | 能力 |
|------|------|
| 登录 | **登录页 2.0**（v0.3.5）：赛博暗/亮色同款视觉 + 提交互斥（连点不会耗尽自己的失败配额）+ 口令可见性切换 + CapsLock 提示 + 会话过期原因提示 + 实例名/版本信息（来自免认证的 `/api/login-info`，只含非敏感静态字段） |
| 仪表盘 | 实例状态 / 三层健康 / 概览统计 / 近 7 天流量图（按需下钻单代理）/ 会话内实时速率曲线 / 客户端与代理列表（可展开代理曲线）/ **按用户聚合**（v0.3.5：`/api/v2/users` 的客户端数与代理数）/ 日志（跟随与暂停、行数可选）/ **系统体检**（只读 doctor，按钮触发） |
| 配置 | 逐字段表单（敏感值打码提示）→ 预览 diff（+绿/−红）→ 应用（一次事务、一次重启）→ 失败自动回滚；可删除键、可新增键 |
| 历史 | 快照列表，**先看差异再回滚** |
| 审计 | **双视图**：插件审计（统计 / 按用户与按操作分布 / 最近记录）与 **Web 操作**（谁在什么时候改配置 / 停服务，登录与变更动作留痕、来源与结果）；策略缺失或关闭时说明原因而不报错；**服务端过滤 + 加载更多 + 导出**（v0.3.5：`action/result/source` 或 `op/decision/user/source` 子串过滤、按 offset 翻页、JSONL/CSV 附件导出） |
| 配置编辑增强 | 键名搜索过滤；数组/内联表值即时校验；有未保存修改时离页确认 |
| 进程 | 启动 / 重启 / 停止 / 清理离线记录（返回清理条数；操作期间按钮禁用、状态徽章显示"操作中…"） |
| 主题 | 赛博暗（默认）/ 亮色双主题：跟随系统（可实时变化）或手动切换 |
| 服务 | **服务视图**（v0.3.4）：frps / Web 管理台 / 服务端插件的托管状态；插件 `start|stop|restart` 可在页面操作；**Web 自身可自重启**（v0.3.5：仅 systemd 托管，后端先应答后延迟执行，页面轮询等待恢复；direct 模式给 CLI 指引） |
| 版本 | **版本管理**（v0.3.4）：运行中/磁盘版本与一致性；表单提交安装任务（后台执行 + 进度轮询）；"重启使新版本生效"入口 |
| 交互 | **命令面板 `Ctrl+K`**（视图/动作/主题/复制/**CLI 命令**）、快捷键 `g d/c/a/s/v/k`、`r` 刷新、`/` 聚焦过滤、表格排序与过滤、**客户端/代理详情抽屉**（v0.3.4）、**会话管理抽屉**（v0.3.5） |
| 命令参考 | **命令视图**（v0.3.5，`g k`）：CLI 全部命令与参数（数据由 Typer 反射生成，与 `--install-completion` 的补全同源）、只读/变更/root/systemd 徽章、一键复制、跳转到界面中的对应能力 |
| 审计增强 | 时间窗（1h/24h/7d，与 CLI `--since` 同源）；**诊断导出**下载（打码配置 + 体检 + 日志尾）（v0.3.4） |
| 传输优化 | 条件请求（ETag/304：数据未变时响应体为 0 字节）+ 服务端分层缓存；变更操作后立即失效 |
| 并发护栏 | 请求线程有界（默认 32），超限快速 503——慢 dashboard 下不会无限堆线程 |
| 监控 | `web serve --metrics` 暴露 Prometheus 文本（实例状态 / 健康 / dashboard 统计；Basic auth） |

### 一条硬边界

**绝不重新实现 frp 已有的能力。**

| 能力 | 由谁提供 |
|------|---------|
| 配置合法性判定 | 官方 `frps verify -c`（唯一权威） |
| 状态、统计、代理列表 | 官方 v2 Admin API |
| 服务进程 | 官方 `frps` 二进制，或 systemd |
| 配置生成、进程编排、失败回滚、终端/浏览器呈现 | **frpsctl** |

---

## 环境要求

| 项 | 要求 | 为什么 |
|----|------|-------|
| 操作系统 | **仅 Linux** | 进程身份校验依赖 `/proc/<pid>/stat`；互斥用 `flock`；原子写用 `fchmod` |
| Python | **≥ 3.11** | 依赖标准库 `tomllib` |
| frps | **≥ 0.70.0** | v2 Admin API 自 0.70.0 引入，本工具只用 v2 |
| 建议版本 | 0.71.0 | 0.70.x 可用，但缺少一个已知远程 DoS 的修复（`start` / `doctor` 会告警） |

非 Linux 内核、或未挂载 `/proc` 的容器会被**直接拒绝启动**，而不是降级——
身份校验失效的代价是杀掉无关进程。

---

## 安装

### 方式一：一键安装（uv，无需 Python）（推荐）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool install frpsctl
```

一条命令搞定。`uv` 是单文件静态二进制，**自带 Python 版本管理**——服务器上
没有 Python 时它会自动下载一个独立构建，因此**这是唯一不依赖系统 Python 的
安装方式**。装完命令在 `~/.local/bin/frpsctl`。

```bash
uv tool upgrade frpsctl          # 升级
uv tool uninstall frpsctl        # 卸载（不动实例数据）
```

### 方式二：pipx / pip（已有 Python ≥ 3.11）

```bash
pipx install frpsctl        # 或：pip install frpsctl
# 升级：pipx upgrade frpsctl / pip install -U frpsctl
```

建议开启 shell 补全（支持 bash/zsh/fish）：

```bash
frpsctl --install-completion
```

> **要用 systemd 托管时（`service install` / `web service install` /
> `plugin service install`），frpsctl 必须装到系统路径**：unit 带
> `ProtectHome=true`，`~/.local/bin`（root 下即 `/root/.local/bin`）对服务用户
> 不可见，安装时会当场拒绝。
>
> ```bash
> pipx install --global frpsctl     # → /usr/local/bin/frpsctl（推荐）
> # 或：sudo pip install frpsctl
> # 或：sudo ./install.sh --system
> ```
>
> 从零开始的完整步骤与常见坑见
> [systemd 部署速查：从零开始](#systemd-部署速查从零开始)。

### 方式三：源码安装

**一键脚本**（两种跑法，效果相同）：

```bash
# A. 先 clone 再运行
git clone https://github.com/ThzxxArt/frpsctl.git
cd frpsctl
./install.sh

# B. 在线直跑：脚本自动把源码下载到数据目录（无需 git）
curl -fsSL https://raw.githubusercontent.com/ThzxxArt/frpsctl/main/install.sh | bash
```

脚本做的事：建一个独立 venv → 装依赖 → 把 `frpsctl` 注册到 `~/.local/bin` → 自检。
**不需要 pipx、不需要 uv、不需要 sudo**（venv 是标准库自带的）。

```console
$ ./install.sh
检查运行环境
 ✓ 操作系统：Linux
 ✓ Python：Python 3.13.13（/usr/bin/python3）
 ✓ venv 模块可用
 ✓ 源码目录：/mnt/d/CodeWorkspace/frpsctl（脚本同目录）

创建虚拟环境
 ✓ 已创建：/home/u/.local/share/frpsctl-src/venv
安装 frpsctl 及其依赖
 ✓ 依赖就绪（typer / pydantic / tomlkit / httpx）
注册全局命令
 ✓ 已注册：/home/u/.local/bin/frpsctl
自检
 ✓ 命令可用：frpsctl 0.3.5

frpsctl 安装完成
```

| 选项 | 作用 |
|------|------|
| （无） | 装到 `~/.local`（命令 → `~/.local/bin/frpsctl`） |
| `--system` | 装到 `/usr/local`（需要 `sudo`） |
| `--prefix DIR` | 自定义前缀 |
| `--uninstall` | 卸载（删 venv 与命令，**不动实例数据**；不需要 Python） |
| `--no-verify` | 跳过安装后自检 |

| 环境变量 | 作用 |
|---------|------|
| `FRPSCTL_INSTALL_REF` | 固定源码版本（tag / 分支，默认 `main`），管道安装用 |
| `FRPSCTL_INSTALL_URL` | 覆盖源码 tarball 地址（镜像 / 离线内网用） |

**反复运行即为升级**（会重新装依赖并重写命令）。源码用 `-e` 方式安装，因此改完
源码无需重装，命令立即生效。

> 脚本**不下载 frps 二进制**——那是 `frpsctl install` 的职责（需要网络与校验和，
> 且要写入用户数据目录）。

**手动安装**：

```bash
git clone https://github.com/ThzxxArt/frpsctl.git && cd frpsctl
uv venv && uv pip install -e ".[dev]"
.venv/bin/frpsctl --version          # 直接用 venv 里的命令，不注册全局

# 或让 pip 直接装到用户环境
pip install --user -e .
```

### 服务器没有 Python 怎么办

三条路，按省事程度排序：

1. **用 uv**（推荐）——uv 自带 Python，系统什么都不用装：

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool install frpsctl
   ```

2. **装一个 Python 再走本文任一方式**（需要 ≥ 3.11 **且 venv 组件完整**）：

   ```bash
   sudo apt install python3 python3-venv     # Debian / Ubuntu
   sudo dnf install python3                  # Fedora / RHEL
   sudo apk add python3                      # Alpine
   ```

   最小化安装的系统常出现"有 python3 但缺 venv/ensurepip"——`install.sh` 会在
   第一步检测出来，并给出上面两条命令；检测到完全没有 Python 时，会把 uv
   一键命令直接打印出来，而不是只报一句错。

3. **独立二进制**：当前不提供（设计文档 ADR-4 评估过"只打 Python 侧、收益
   有限"，内嵌 frps 明确拒绝）。

### 安装 frps 二进制

Python 侧装好后，用 `frpsctl install` 下载官方 frps（sha256 强校验）：

```console
$ frpsctl install
frps 0.71.0 → /home/u/.local/share/frpsctl/bin/frps-0.71.0
当前版本软链 → /home/u/.local/share/frpsctl/bin/frps

注意：换链**不影响正在运行的进程**（Linux 上可执行映像已绑定 inode），
      只影响下一次 start。运行 `frpsctl status` 可对比两个版本。
```

- 下载地址可被镜像替换，但**信任锚是官方校验和文件**（`frp_sha256_checksums.txt`）。
  内置了官方源与 ghproxy；需要其他镜像时：

  ```bash
  frpsctl install --mirror https://my-mirror.example/frp/releases/download
  # 或
  FRPSCTL_MIRROR=https://a.example,https://b.example frpsctl install
  ```

  命令行给的镜像会**替换**（而不是追加）内置源——指定镜像通常意味着"内置源
  在我这里不通"。
- **拿不到校验和就拒绝安装**（fail-closed）。确有需要可 `--insecure` 跳过，风险自负。
- 低于 0.70.0 的版本直接拒绝——装了也用不了。
- **幂等**：同版本已在盘上时不重复下载，但**仍会校正 `bin/frps` 软链**——手工改歪的
  链会被修回来。想"只落盘、不动链"请显式加 `--only-download`。

`--with-frpc` 会从**同一个 tar 包**里额外取出 `frpc`（测试专用件，供插件契约测试用），
不产生额外下载：

```bash
frpsctl install --with-frpc        # frps + frpc
```

### 验证安装

```bash
frpsctl --version                  # frpsctl 0.3.5
frpsctl install                    # 下载 frps 二进制
frpsctl init                       # 生成配置（下一步是五分钟上手）
```

### 卸载

```bash
frpsctl uninstall              # 卸载当前实例（先列出清单，要求确认）
frpsctl uninstall --all        # 卸载实例根下的全部实例
frpsctl uninstall --keep-data  # 保留配置/快照/审计，只停 unit 与删共享二进制
frpsctl uninstall --keep-bin   # 保留共享二进制（多实例机器上只卸一个实例）
frpsctl uninstall --force      # 运行中的实例先停止再卸载（默认拒绝）
```

- 默认只卸**当前实例**（`--instance` 指定，默认 `default`）。共享二进制（`bin/`）
  要求目标覆盖全部实例才能删——多实例机器上删掉它会让其他实例起不来，此时
  要么 `--all`，要么 `--keep-bin`。
- ⚠️ 删除**不可恢复**：实例目录含 `auth.token`、dashboard 口令、配置快照与插件
  审计。因此默认先列出将删清单并要求确认；`--yes` 跳过确认；`--json` 下**必须**
  显式 `--yes`（破坏性操作不做隐式确认）。
- 运行中的实例默认**拒绝**卸载（退出码 11，数据分毫未动）；`--force` 会先停止
  再卸载。systemd 托管的实例同样处理。
- 权限不足或不该代删的东西**不会静默跳过**：unit 清理需要 root（汇总"未清理项"
  并给出命令）；`/var/log/frps` 与 `frps` 服务账户只提示、不代删（可能另有用途）。
- 多实例机器上只卸一个实例时，共享 unit 模板（`frps@.service` 等）会保留，
  只停用当前实例的 unit——删模板会连累其他实例。
- Python 包本身用包管理器移除：

  ```bash
  pipx uninstall frpsctl            # 或 pip uninstall frpsctl
  ./install.sh --uninstall          # 源码一键脚本安装的（不动实例数据）
  ```

#### 彻底卸载（systemd 部署，按顺序执行）

```bash
# 1) 停服务并解除 systemd 托管（停用并删除对应 unit 模板）
frpsctl web service uninstall      # 若装过
frpsctl plugin service uninstall   # 若装过
frpsctl service uninstall          # frps 本体

# 2) 删实例数据与共享二进制（不可恢复；多实例时按需 --all / --keep-bin）
frpsctl uninstall

# 3) 卸 frpsctl 本体（按安装方式三选一）
pipx uninstall frpsctl             # uv 安装：uv tool uninstall frpsctl
# ./install.sh --uninstall

# 4) 清理"不属于本工具、因而不会代删"的东西（确认无用后手工执行）
sudo rm -rf /var/log/frps          # 日志目录（uninstall 会按实际 --log-dir 提示路径）
sudo userdel <服务账户>            # 服务账户（uninstall 按安装留档逐个提示实际账户）
sudo rm -rf /opt/frpsctl           # 数据目录残壳（uninstall 已删实例与 bin）
```

> `frpsctl uninstall` 只处理"实例数据 + 共享二进制 + unit"；服务账户、
> 日志目录与 frpsctl 本体不在它的代删范围（第 3 / 4 步），工具会按安装留档
> （`service.json`）提示**实际使用过**的账户与日志路径——这与第 2 步说明的
> 边界一致。

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
`prune`、Web 管理台等操作。

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
listen   : 0.0.0.0:7000
dashboard: 127.0.0.1:7500 (auth: on)
health   : L1 process ok  L2 control ok  L3 plugin skipped
clients  : 0 online
traffic  : in 0 B / out 0 B  (conns now 0)
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

> ⚠️ **TOML 位置纪律**：`allowPorts`、`maxPortsPerClient` 这类**顶层键必须写在任何
> `[table]` 之前**。写错了它们会变成那张表的子键，而 frps 的报错是极具误导性的
> `unknown field "allowPorts"`——看起来像键名错了，实际是位置错了。

**接下来**：想用浏览器看，直接读 [Web 管理台教程](#web-管理台教程)；想在终端里
深入操作，读 [CLI 使用教程](#cli-使用教程)。

---

## CLI 使用教程

### 全局选项与命令总览

```
--instance, -i NAME    实例名（默认 default，可用 FRPSCTL_INSTANCE 覆盖）
--root PATH            实例根目录（默认 ~/.local/share/frpsctl/instances）
--config PATH          直接指定配置文件（覆盖实例默认）
--binary PATH          直接指定 frps 二进制
--json                 机器可读输出（所有查询类命令支持）
--admin-password       dashboard 口令（优先于配置文件）
--yes, -y              跳过交互确认
--verbose, -v          详细输出（把外部命令与判定过程打进 stderr）
--version              frpsctl 版本
```

**长名可以写在任意位置，短名 `-v` 只能写在子命令之前**：

```bash
frpsctl status --json
frpsctl config get bindPort -i web
frpsctl verify --verbose
```

`frpsctl --verbose status` 与 `frpsctl status --verbose` 完全等价。`--verbose`
只进 stderr，因此 `--verbose --json` 的 stdout 仍是干净的 JSON，可以直接管道给 `jq`：

```console
$ frpsctl verify --verbose
[trace] 读取二进制版本：~/.local/share/frpsctl/bin/frps-0.71.0 -v
[trace] 二进制版本：0.71.0（退出码 0）
[trace] 执行权威校验：~/.local/share/frpsctl/bin/frps-0.71.0 --strict_config=true verify -c …/tmpXXXX.toml
[trace] verify 退出码 0
…/instances/default/frps.toml 校验通过（frps 0.71.0，标志：--strict_config=true）
```

### 教程 1：看状态

```bash
frpsctl status              # 人读
frpsctl status --json       # 机器可读（前后位置都行）
frpsctl status --watch      # 持续刷新
frpsctl status --watch --json   # 持续输出单行 JSON（NDJSON），可逐行消费
```

`status` **永远以退出码 0 结束**（除非参数写错）——它的职责是回答"现在什么情况"，
而不是失败。异常情况会如实报告：

```console
$ frpsctl status            # 实例没在跑
instance : default            owner : none
state    : STOPPED
config   : /home/u/.local/share/frpsctl/instances/default/frps.toml (0600)
listen   : 0.0.0.0:7000
dashboard: 127.0.0.1:7500 (auth: on)
```

```json
{
  "instance": "default",
  "owner": "direct",
  "state": "RUNNING",
  "pid": 179910,
  "uptime_seconds": 1.72,
  "binary_version": "0.71.0",
  "disk_version": "0.71.0",
  "config_mode": "0600",
  "listen": { "addr": "0.0.0.0", "port": 7000 },
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

> **`start` / `restart` 的成功判据是 L1 ∧ L2（gate）**。gate 未通过时退出码为
> **12**，同时向 stderr 告警——但进程**不会被清理**：它仍由本工具托管，
> `status` 看得见、`stop` 停得掉。直接重试 `start` 只会得到"已在运行(6)"。
>
> L3 失败**不改变退出码**，但会显著告警——因为插件是 fail-closed 的：插件不可达
> 意味着**所有客户端都无法登录**。

`owner` 字段说明"谁在管这个进程"，永远无歧义：

| owner | 含义 |
|-------|------|
| `direct` | 由 frpsctl 直接托管（state.json 是权威） |
| `systemd` | 由 systemd 托管，`start`/`stop`/`restart` 委托 systemctl |
| `none` | 没有进程在跑，也没有 unit |

### 教程 2：看客户端、代理与流量

`status` 给的是总数；"谁在线、哪个代理在跑、各跑了多少流量"用这两条
（走 v2 Admin API，自动翻页取全量，`--json` 可管道给 `jq`）：

```console
$ frpsctl clients
name                         user       hostname             online  ip               version
alice.f2a3e2edeef4a920       alice      DESKTOP-S8A3AVK      True    127.0.0.1        0.71.0

$ frpsctl proxies
name                         user       type    port   phase    conns  traffic(in/out)
alice.alice-ssh              alice      tcp     6000   online   0      0 B / 0 B

$ frpsctl proxies --type http      # 只看某类型
```

近 7 天的流量历史（日粒度；数据源与 Web 趋势图相同）：

```console
$ frpsctl traffic                  # 全部代理逐日汇总
date                  in         out
2026-09-16       1.2 MiB     3.4 MiB
2026-09-17       0.4 MiB     1.1 MiB
合计             1.6 MiB     4.5 MiB

$ frpsctl traffic alice.alice-ssh  # 单个代理的明细
```

离线或已删除的代理在数据源上返回 404 = 无数据（不是错误）；单个代理查询失败
也不拖垮整体。代理数超过 50 时会明确提示"仅统计前 50 个"（Web 同理）；慢
dashboard 下 6 秒预算内未取全的代理同样会被标记（`partial`），避免把部分
数据当全量。

### 教程 3：改配置（核心事务）

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

**常用变体**：

```bash
frpsctl config set bindPort 8000 --no-restart   # 只写不重启（输出会提示"尚未生效"）
frpsctl config set bindPort 8000 --dry-run      # 只校验并展示 diff，不写入、不重启
frpsctl config set webServer.password --prompt  # 敏感值隐藏输入（不进 argv / shell 历史）
frpsctl config set webServer.password --stdin   # 或从管道读：echo -n "$PW" | frpsctl ...
frpsctl config unset maxPortsPerClient          # 删键回落 frp 默认值（同一事务闭环）
frpsctl config list                             # 列出全部键（值自动打码）
frpsctl config list --prefix webServer          # 只看某张表
frpsctl config list --tree                      # 按表分组缩进展示
frpsctl config get bindPort                     # 读单键
frpsctl config get auth                         # 读整张表（机密自动打码）
frpsctl config get auth.token --reveal          # 需要看原值时显式索取
frpsctl config edit                             # 用 $EDITOR 改，保存后走同一闭环
frpsctl config diff                             # 当前 vs 上一份快照
frpsctl config diff --steps 3                   # 当前 vs 第 3 新的一份
frpsctl config rollback                         # 回滚到上一份
frpsctl config rollback 3                       # 回滚到 3 份之前
```

`config list` 的输出（值自动打码）：

```console
$ frpsctl config list --prefix webServer
webServer.addr = "127.0.0.1"
webServer.port = 7500
webServer.user = "admin"
webServer.password = hQ***x3
```

> `--dry-run` 与 `--no-restart` 的区别：前者**什么都不写**（只校验+预览），
> 后者已经落盘、只是没有重启；`config unset` 删除不存在的键会报配置错误(3)
> ——拼错键名的"成功删除"会让人以为清掉了某个设置。
>
> `--no-restart` 之后别忘了 `frpsctl restart` 让变更生效（`status` 会提示）。

**机密保护**：`config get` 默认打码（`hQ***x3` 形式，保留首尾便于核对是不是同一个
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
此时有 Basic Auth 保护）。拦截的是"无鉴权 + 对外暴露"这个组合本身。

### 教程 3.5：一次改多个键（config apply）

```bash
frpsctl config apply --set maxPortsPerClient=30 --set bindPort=7001 --unset log.maxDays
frpsctl config apply --set transport.tls.force=true --dry-run   # 只校验 + 看 diff
```

与 `config set` 的差别只在"一次改几个键"：所有键（赋值与删除）作用在**同一份
文档**上，只产生一份快照、一次重启——拆成多条 `config set` 会重启多次，中间
那次还可能撞上危险组合检查。Web 配置表单走的就是同一条路径。

### 教程 3.6：能力自查（capabilities）

```bash
frpsctl capabilities --json | jq '.commands | length'   # 55（叶子命令）
frpsctl capabilities                                     # 人读：命令/退出码/环境变量
```

清单**从代码派生**（命令树、`ExitCode`、`env.py`）——它是什么，这里就有什么。

### 教程 4：看日志

```bash
frpsctl log                 # 最近 100 行
frpsctl log -n 500          # 最近 500 行
frpsctl log -f              # 持续跟踪（tail -f）
frpsctl service logs -f     # systemd 模式：unit 级日志（journalctl -u）
```

日志路径取自配置里的 `log.to`（相对路径按实例目录解析）。frpsctl **不写**这个
文件——它由 frp 自己写并按天轮转。多一个写入者会和轮转互相破坏。

`service logs` 看的是 **journald** 里的 unit 级日志（启动失败、OOM、权限拒绝
这类"frp 还没写进自己的日志文件"的问题），两者互补。

### 教程 5：启停

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

### 教程 6：体检与巡检

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
| dashboard 暴露面 | ERROR | 绑非回环 **且** user/password 全空 = 完全不鉴权 |
| dashboard 弱口令 | WARN | user/password 全空；**password 为空而 user 非空**（frp 把空口令当合法口令）；或 admin/admin |
| `transport.tls.force` | WARN | 未开启时可接受明文 frpc |
| `allowPorts` / `maxPortsPerClient` | WARN | 未设置时端口可被任意申请 |
| 端口可绑定性 | ERROR | 探测各监听端口；自己占着会识别为"正常" |
| `< 1024` 端口 | INFO | 提示需 `CAP_NET_BIND_SERVICE` |
| 进程所有权冲突 | ERROR | systemd 与 direct 同时成立 |
| 插件暴露面 | ERROR | 插件回调指向非回环（协议无认证） |
| 插件可达性 | WARN | 不可达时提示"客户端将无法登录" |
| Web 口令文件权限 | WARN | `web service install` 生成的口令文件权限过宽（应 0600） |

**有 ERROR 时退出码为 1**，可直接接进 CI 或监控。

多实例巡检用 `instances`（一行一个：owner / 状态 / pid / 版本 / 健康）：

```console
$ frpsctl instances
default          direct   RUNNING (pid 10582, up 2m10s)  frps 0.71.0  L1 process ok  L2 control ok  L3 plugin skipped
web              systemd  SYSTEMD_ACTIVE (pid 20041)
```

默认不做网络探测（快速）；加 `--health` 会对运行中的实例跑三层健康检查。
`instances --json` 输出与 `status --json` 同构的数组。

### 教程 7：清理离线记录

```bash
frpsctl prune               # 清理 dashboard 统计里的离线代理记录
```

⚠️ **frp 没有强制下线在线代理的 API**——`DELETE /api/proxies` 的实际语义是
`ClearOfflineProxies()`（只接受 `?status=offline`，源码与真机均已核实）。
要断开某个客户端请停掉它的 frpc。此前版本的 `kick` 基于对该端点的误读，
从未真正工作过，已由 `prune` 取代。

---

### 教程 8：看谁操作过管理台（web audit）

```bash
frpsctl web audit stats --since 7d     # 登录与变更动作的统计（按动作/来源）
frpsctl web audit tail -n 20           # 最近 20 条（谁、从哪来、成没成）
frpsctl web audit tail -f              # 实时跟随
```

登录成功与失败、start/stop/restart/prune/rollback/config apply 都会留痕
（来源 IP + 会话指纹，不落任何凭据）。Web 管理台"审计"页有对应的 **Web 操作**
标签页。

## Web 管理台教程

### 启动与登录

```bash
frpsctl web serve                                  # 默认只绑 127.0.0.1:8787
frpsctl web serve --bind 127.0.0.1:9000            # 换端口
FRPSCTL_WEB_PASSWORD=my-pw frpsctl web serve       # 指定口令（默认自动生成并打印一次）
frpsctl web serve --password-file ./web-password   # 从 0600 文件读口令（systemd 部署用）
```

```console
$ frpsctl web serve
Web 管理台：http://127.0.0.1:8787/
登录口令（仅显示这一次）：Ih2x...（24 字符）
Ctrl-C 停止。
```

浏览器打开上面的 URL，输入口令即进入主界面。会话默认 8 小时（只存服务端内存，
重启即失效）；刷新页面不会掉线（Cookie 还在，前端自动恢复会话）。

**后台运行（非 systemd 环境）**：

```bash
frpsctl web start                    # 后台启动（direct 模式；口令自动落实例文件）
frpsctl web status                   # owner=direct；systemd 托管时显示 systemd
frpsctl web restart                  # 复用上次参数（--bind 等可覆盖）
frpsctl web stop                     # SIGTERM 优雅退出（等待 → SIGKILL 兜底）
```

子进程就是 `web serve`（与 systemd 的 `ExecStart` 同构），进程与启动参数记录在
`<实例>/web-state.json`；日志写到 `<实例>/web.log`（超过 8 MiB 轮转一次）。
口令统一走实例内 `web-password`（0600，与 `web password set|show` 同一文件）——
不再"打印一次即丢失"；`--password` 给的值也会先写该文件（避免出现在进程命令行里）。

⚠️ **两种托管模式互斥**：systemd 托管（`web service ...`）与 direct 后台
（`web start`）双向拒绝同时使用，并给出切换命令。容器场景不要用 `web start`
——容器里前台 `web serve`（进程即容器主进程）才是正确形态。

**忘了口令？** 分两种情况：

- **systemd 部署**（`web service install` 会把口令写入实例目录的文件）：

  ```bash
  frpsctl web password show        # 文件不存在时报配置错误；权限过宽时向 stderr 告警
  ```

- **前台 `web serve` 且口令是自动生成的**：它只在启动时打印一次、**不落盘**，
  因此无法找回——重新启动并显式指定一个新口令即可：

  ```bash
  FRPSCTL_WEB_PASSWORD='your-new-password' frpsctl web serve   # 或 --password / --password-file
  ```

  想以后随时能取回，改用 `sudo frpsctl web service install`：口令会持久化到
  实例目录的 `web-password`（0600），之后用 `frpsctl web password show` 读回。

### 仪表盘

打开后是仪表盘页，每 5 秒自动刷新（右上角可关，或点"刷新"手动更新）：

| 区域 | 内容 | 要点 |
|------|------|------|
| 实时概览 | 客户端 / 代理总数 / 当前连接 / 今日入站出站 / TLS 强制（渐变指标条） | 数字来自 dashboard 统计；首次进入滚动到位 |
| 实例状态 | owner / 状态 / pid / 运行时长 / 版本 / 监听地址 / systemd unit | 二进制版本与磁盘版本不一致时显示"重启生效" |
| 控制面健康 | L1 / L2 / L3 三层 + gate + 详情 | L2 或 L3 失败时详情直接给出原因 |
| 近 7 天流量 | 按天柱状图（渐变蓝=入站 绿=出站） | 悬停显示浮动提示（日期/入出）；标题栏带合计；点下方代理行可下钻该代理曲线 |
| 实时流量 | 页面打开期间的速率面积图（每 5 秒采样一次，累计值差分） | 平滑曲线 + 悬浮十字线提示；峰值/均值进 Hero 指标条；采样存本地浏览器，刷新不丢 |
| 客户端 | name / user / hostname / ip / 状态 / 版本 | 本地过滤 + 点表头排序；双击 name 复制；**单击查看详情抽屉**（v0.3.4） |
| 代理 | name / user / 类型 / 端口 / 状态 / 连接 / 今日流量 | **点击任意行展开该代理的 7 天曲线**；行尾"详情"打开抽屉；表头排序、本地过滤；右上角"清理离线记录" |
| 代理类型分布 | 环图（来自 `proxyTypeCount`） | 悬停查看占比；颜色随主题 |
| 今日流量 Top 5 | 按今日入站+出站合计排序的代理排行 | 双击代理名复制 |
| 日志 | 最近的日志尾部（复用 `log.to` 解析） | 增量拉取（只取新增行）；ERROR/WARN 着色；向上滚动自动暂停；"复制"按钮一键复制 |

**交互速查**：快捷键 `g d` / `g c` / `g a` / `g s`（服务）/ `g v`（版本）切换视图、
`r` 刷新、`/` 聚焦过滤框、`Ctrl+K`（或 `Cmd+K`）打开**命令面板**（视图/动作/主题/
复制实例名的统一入口，↑↓ 选择、Enter 执行）、`Esc` 关闭弹层；危险操作走自绘
确认弹层（可 Enter/Esc）；审计页有独立过滤框与时间窗（1h/24h/7d）。

**服务视图**（导航"服务"）：frps / Web 管理台 / 服务端插件的托管状态一屏可见；
**插件服务可在页面直接 start/stop/restart**（按当前托管模式分派：systemd 优先、
direct 次之，两个方向都经互斥守卫）——插件是登录单点（fail-closed），挂了在这里
能第一时间看到并拉起。Web 管理台自身是只读的：停止/重启会断开当前会话，页面
会提示改用 CLI。

**版本视图**（导航"版本"）：运行中/磁盘版本与一致性、最低与建议版本；提交安装
任务后可在页面看进度（下载百分比→校验→落盘→换链，后台执行、1 秒轮询）；
完成后"重启实例使新版本生效"。Web 不提供 `--insecure/--mirror`（始终强校验 +
默认镜像）；bin 目录不可写（Web 服务用户无权限）时会明确提示改用 CLI。

页面顶部的**横幅**会明确报告异常：

- state.json 损坏（附处置指引）；0.70.x 缺安全修复；插件不可达（客户端将无法登录）；
  流量超过 50 个代理时提示已截断；**配置文件已改但未重启**（v0.3.4，附"立即重启"按钮）。

**进程操作**在右上角：启动 / 重启 / 停止。操作进行中按钮会禁用、状态徽章显示
"操作中…"——启动最长要等 10 秒健康检查，此前完全无反馈。

### 配置编辑：预览、应用与回滚

配置页分三块：**编辑表单**、**变更预览**、**历史与回滚**。

**编辑**：

1. 每个字段一行（按表分组）。敏感值（token / 口令）不回显，显示打码提示
   `hQ***x3（已设置；留空不改）`——留空表示保留原值，填入新值才会覆盖。
2. 想删除某个键（回落 frp 默认值），点行尾的"删除"（会划线标记，再点"恢复"可取消）。
3. 想**新增**配置里还没有的键（例如 `kcpBindPort`），用表单底部的"新增键"输入
   键名与值（值语法与 CLI 一致：`7001`、`"text"`、`[1,2]`…）。
4. 顶部计数会显示"N 项待应用（改 x / 删 y）"，点它可以跳到第一处修改。

**预览 → 应用**：

5. 点"预览变更"：服务端在锁内取当前配置快照并生成**打码 diff**（`+` 绿、`-` 红）。
   预览有效期 10 分钟；期间配置若被 CLI 改过，应用会被**拒绝**而不是覆盖
   （CAS 保护）——重新预览即可。
6. 确认 diff 无误后点"确认应用（重启服务）"：多键修改与删除合并成**一次事务**
   （一份快照、一次重启）；健康检查失败会自动回滚并如实告知。

**历史与回滚**：

7. "历史与回滚"卡片列出最近 10 份快照（时间 / 操作 / 步数）。
8. 每行先点**"查看差异"**——展开该快照与当前配置的打码 diff（与 CLI
   `config diff --steps N` 同一实现）。回滚是危险操作，先看清会改什么。
9. 确认后点"回滚到此份"：与 CLI 一样走完整闭环（校验 → 替换 → 重启 → 失败再回滚）。

> **配置原文绝不下发浏览器**：界面与 API 只返回打码值；快照列表只读元数据
> （时间/动作/步数），差异接口也返回打码 diff。要看明文用 CLI `config get --reveal`。

### 口令、部署与安全

**默认只绑回环**。要远程访问必须显式 `--allow-non-loopback`：

```bash
frpsctl web serve --bind 0.0.0.0:8787 --allow-non-loopback
```

建议再套一层反向代理终结 TLS；反代后加 `--trusted-proxy`，让登录失败限速按
`X-Forwarded-For` 的**最后一跳**区分来源（否则所有请求同源，攻击者的失败会
连带把管理员锁在冷却之外）。默认关闭时该头完全不被读取——伪造它既不能绕开
限速、也不能制造新来源。

**用 systemd 托管**（生成 0600 口令文件，unit 只引用路径，明文不落 unit）：

```bash
sudo frpsctl web service install    # 体检 → 渲染 frpsctl-web@.service → enable
sudo frpsctl web service status
sudo frpsctl web service uninstall  # 口令文件保留
```

`web service install` 同样需要 `frpsctl` 位于系统路径（不能被 `ProtectHome`
挡住）——与插件服务的部署要求一致。`--trusted-proxy` 会写进 unit。

**安全设计**（比 frp 自带 dashboard 更严——它正是本项目安全决策的来源）：

- 默认只绑回环；绑非回环必须显式开关；
- **不允许空口令**：自动生成（仅打印一次）或显式指定；会话 Cookie 带
  `HttpOnly` + `SameSite=Strict`；
- 一切变更请求要求 `X-CSRF-Token`（登录时下发，仅存浏览器内存）；
- 登录失败按来源限速（60 秒 5 次），错误口令与限速的响应完全一致；
- 配置原文绝不回显；响应带 CSP（`default-src 'none'`）与 `no-store`；
- 前端为**同源多文件**（ESM 模块 + 独立 CSS，静态路由白名单 + 防穿越），
  **零外部域**（不加载任何 CDN/第三方资源）；CSP nonce-only
  （每请求 nonce，外链脚本/样式同样注入）。

### 命令参考、会话与审计过滤（v0.3.5）

- **命令视图**（导航「命令」或 `g k`）：CLI 全部命令与参数（flag/默认值/范围/必填）、
  只读/变更/需 root/需 systemd 徽章、一键复制命令骨架、跳转到界面里的对应能力。
  数据由 Typer 反射生成，**与 `--install-completion` 的 shell 补全同源**——界面不会
  落后于 CLI；命令面板 `Ctrl+K` 里同样能搜到并复制任意命令。
- **会话管理**：右上角「会话」按钮打开抽屉，列出活跃会话（来源 / 剩余有效期 /
  指纹）并支持「登出其他所有会话」（保留当前）。口令疑似泄露时这是最直接的止血
  动作；被登出的浏览器下一次请求会回到登录页。
- **审计过滤与导出**：审计视图可选服务端过滤字段（Web 操作：动作/结果/来源；
  插件审计：操作/判定/用户/来源）、时间窗预设（1h/24h/7d/全部）或**自定义起止**
  （ISO 时间 / unix 时间戳），「加载更多」按 offset 翻页，导出为 JSONL 或 CSV。
  注意：上方统计是**时间窗内全量**，不随记录过滤变化（界面有说明）；导出上限
  10000 行，超出时通过响应头 `X-Export-Truncated` / `X-Export-Records` 声明，
  正文保持严格可解析（JSONL 可整文件喂 `jq`）。
- **按用户**：仪表盘新增「按用户」卡片（客户端数与代理数，来自 `/api/v2/users`）。
  该端点没有流量字段，因此卡片不含流量维度。
- **管理台自重启**：服务视图中 Web 卡片的「重启」按钮（仅 systemd 托管）。
  后端先返回应答、延迟 1 秒再执行 `systemctl restart`，页面轮询等待恢复后自动
  重新加载；direct 后台模式请用 CLI（`frpsctl web restart`）。

### CLI 与 Web 的对应关系

Web 的每个操作都调用与 CLI **同一套 core 函数**——不存在"界面专用"的第二套逻辑：

| 操作 | CLI | Web |
|------|-----|-----|
| 启停 / 重启 | `start` / `stop` / `restart` | 右上角按钮 → `POST /api/actions/*` |
| 看状态与健康 | `status` | 仪表盘各卡片 |
| 客户端 / 代理 / 流量 | `clients` / `proxies` / `traffic` | 列表与图表（同一 v2 数据源） |
| 改配置 | `config set` / `config unset` | 表单 → 预览 → 应用（多键一次事务） |
| 看差异 | `config diff --steps N` | "查看差异"（同一 `snapshot_diff`） |
| 回滚 | `config rollback [N]` | "回滚到此份" |
| 日志 | `log` | 日志面板（同一 `core/logs`） |
| 清理离线记录 | `prune` | "清理离线记录" |
| 口令 | `web password show` | 登录页输入（含可见性切换与 CapsLock 提示） |
| 体检 | `doctor` | 仪表盘「运行体检」（同一 `run_doctor`） |
| 审计 | `plugin audit` / `web audit` | 审计视图（双 scope）+ **服务端过滤/翻页/导出**（v0.3.5） |
| 会话 | （无 CLI 命令） | 头部「会话」抽屉：查看 + 登出其他所有会话（v0.3.5） |
| 命令自查 | `capabilities` | **命令视图**（`g k`）：全部命令与参数、一键复制、跳转（v0.3.5） |
| 升级 frps | `install` | 版本视图（后台任务 + 进度）（v0.3.4） |
| 管理台自管 | `web restart` | 服务视图「重启」（仅 systemd；先应答后动作）（v0.3.5） |

---

## 进阶用法

### 多实例

一台机器跑多个 frps：用 `--instance` 或环境变量区分，各自有独立的配置、状态、
日志、锁。

```bash
frpsctl --instance web init --bind-port 7001 --dashboard-port 7501
frpsctl --instance web start
frpsctl --instance web status

FRPSCTL_INSTANCE=web frpsctl status    # 或长期用环境变量
```

优先级：`--instance` > `FRPSCTL_INSTANCE` > `default`。服务端场景可把实例根目录
放到 `/etc`：

```bash
frpsctl --root /etc/frps/instances --instance web init
```

### 服务端插件（多用户鉴权 + 端口白名单）

frp 的服务端插件是一个 HTTP 回调：frps 在 `Login` / `NewProxy` 等事件发生时 POST
一段 JSON，由插件决定放行还是拒绝。本工具用 Python（标准库）实现该回调。

```bash
frpsctl plugin init      # 生成策略模板（0600，默认 fail-closed）
frpsctl plugin check     # 离线校验 + 试算典型裁决
frpsctl plugin serve     # 前台启动（只允许绑回环）
frpsctl plugin start     # 后台启动（direct 模式，非 systemd；SIGTERM 先刷审计再退出）
frpsctl plugin status    # 托管状态（systemd / direct / none）
```

策略里的用户可以用命令维护——不必手写 JSON（写入前用与 `plugin check` 相同的
判据复验，0600 原子写，`_comment` 等自定义字段原样保留）：

```bash
frpsctl plugin user list                               # 用户与权限摘要（不显示策略级凭据）
frpsctl plugin user set alice --ports 6000-6010 --max-proxies 5
frpsctl plugin user set alice --no-random-port         # 只改这一个字段，其余保持
frpsctl plugin user set bob --names ""                 # 空串 = 删除该字段（不限名称）
frpsctl plugin user remove alice
```

> 插件服务在启动时载入策略：改完记得重启它才生效
> （`systemctl restart frpsctl-plugin@<实例>`）。

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

#### 策略文件

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

#### frps 侧配置

```toml
[[httpPlugins]]
name = "frpsctl"
addr = "http://127.0.0.1:8080"
path = "/handler"
ops  = ["Login", "NewProxy"]
```

`ops` 至少要有 `Login` 与 `NewProxy`：前者做鉴权，后者做端口与配额治理。

#### frpc 侧配置

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

#### 审计

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

`plugin serve` 收到 **SIGTERM**（`systemctl stop` 发的就是它）会**优雅退出**：先停
服务、再把审计缓冲刷盘，然后才退出。

### 用 systemd 托管 frps

> 以下命令需要 root：建议 `sudo -i` 进入 root shell 后执行。单条 `sudo` 会
> **重置环境变量**——数据目录不在默认位置时必须写成
> `sudo FRPSCTL_DATA_HOME=/opt/frpsctl frpsctl …`（或 `sudo -E`）。

```bash
frpsctl service install           # 安装 frps@.service 模板并 enable
frpsctl service status            # 查看托管状态（含运行账户 user/group）
frpsctl service uninstall         # 解除托管
```

安装后 `owner` 变为 `systemd`，`start` / `stop` / `restart` 全部**委托 systemctl**，
pid 文件不再参与任何判定。

#### 部署前置（`service install` 会在安装前检查）

unit 只是第一步——下面各项不满足时 `systemctl start` 必然失败。`service install`
会在**安装之前**逐项检查并当场拒绝，而不是让你事后去 systemctl 的报错里找原因：

| 检查 | 不满足时的典型表现 | 处置 |
|------|------------------|------|
| 服务用户存在（默认优先 `frps`，否则当前用户；可 `--user` 任意账户） | `Failed to determine user credentials` | 用 `--user` 指定已有账户，或加 `--create-user` 自动创建系统账户（需 root） |
| 二进制对服务用户可执行 | `Permission denied` | `sudo frpsctl install` 默认装在 `/root/.local/share`（`/root` 是 0700，frps 用户读不到）。改用共享目录：`sudo FRPSCTL_DATA_HOME=/opt/frpsctl frpsctl install` |
| 日志目录可写（默认 `/var/log/frps`） | `Failed to set up mount namespacing` | `service install` 会自动创建并 chown；无法写入时会被拒绝，可用 `--log-dir` 换位置 |
| 二进制与实例目录**不在家目录下** | unit 看不到路径（`ProtectHome=true` 的挂载隔离） | 用 `/opt`、`/etc`、`/srv` 等系统路径，别用 `~/.local` |

`service install` 还会把**实例目录移交给服务用户**（权限仍是 0700，只是属主从
root 换成服务用户）——否则 frps 进程读不到目录里的 `frps.toml`。安全性不降级：
同机其他用户依然读不到。因此 systemd 模式下请统一用 root 执行 frpsctl
（systemctl 委托本来也需要 root）。

#### systemd 部署速查：从零开始

> 这一节按"从零到跑起来"排序。**第 0 / 1 步是所有坑的来源**：frpsctl 与数据
> 目录只要沾了 `/root`、`/home`，unit 的 `ProtectHome=true`（挂载隔离，
> **改权限无效**）就会让安装被拒或服务起不来。

**第 0 步：frpsctl 装到系统路径；服务账户按需选择**

```bash
pipx install --global frpsctl        # → /usr/local/bin/frpsctl（推荐）
# 或：sudo pip install frpsctl ／ sudo ./install.sh --system
hash -r && command -v frpsctl        # 必须解析到 /usr/local/bin，而不是 ~/.local/bin
```

服务账户三种选择（`service install` 时解析——**不再写死 `frps`**，任何账户都可用）：

| 选择 | 做法 | 适用 |
|------|------|------|
| 默认（零配置） | 什么都不做 | 系统已有 `frps` 用户则用它；否则用**当前用户**（root 部署即 `root`） |
| 指定已有账户 | `--user alice`（`--group` 默认同名组，缺失则回退用户主组） | 已有专用账户 |
| 自动创建 | `--user frps --create-user` | 没有账户、让 frpsctl 建（`--system`、无家目录、nologin；需 root） |

> 以 `root` 作服务用户可用（安装时会给出安全警告），但专用账户是推荐做法。

**第 1 步：数据目录放到系统路径（并持久化）**

```bash
export FRPSCTL_DATA_HOME=/opt/frpsctl
echo 'export FRPSCTL_DATA_HOME=/opt/frpsctl' >> ~/.bashrc   # 后续命令都要用同一路径
```

> 默认数据目录是 `~/.local/share/frpsctl`——root 下即
> `/root/.local/share/frpsctl`，会被 `ProtectHome=true` 挡住；`/opt`、`/etc`、
> `/srv` 均可。普通用户以 `sudo` 执行时环境变量会被 sudo 重置，请写成
> `sudo FRPSCTL_DATA_HOME=/opt/frpsctl frpsctl …`（或 `sudo -E`）。

**第 2 步：下载二进制 → 生成配置 → 装 unit → 启动**

```bash
frpsctl install                 # 下载 frps（sha256 强校验）→ /opt/frpsctl/bin
frpsctl init --no-input         # ⚠️ 打印的 token 与口令只显示这一次，记录下来
frpsctl service install         # 体检（账户/可达/日志/家目录）→ 渲染 unit → enable
frpsctl start                   # 委托 systemctl
frpsctl status                  # owner 应显示 systemd
frpsctl doctor                  # 部署体检：账户 / 二进制可达性 / ReadWritePaths 等
```

**第 3 步（可选）：Web 管理台与插件**

```bash
frpsctl web service install && frpsctl web service start
frpsctl plugin service install && frpsctl plugin service start
```

**常见坑（都由第 0 / 1 步提前规避）**

| 报错 | 原因 | 处置 |
|------|------|------|
| `系统用户或组不存在：<用户>/<组>` | 目标服务账户不存在 | `--user <账户> --create-user` 自动创建，或 `--user` 指定已有账户；不带 `--user` 时按第 0 步默认解析 |
| `服务用户 '<实际用户>' 无法执行 frpsctl：目录 /root 对服务用户缺少执行（x）权限` | frpsctl 装在 `/root/.local/bin` | 第 0 步改用系统路径（`pipx install --global` 等） |
| `frpsctl 位于 /root 下…会被 unit 的 ProtectHome=true 挡住`／`实例目录位于 /home 下…` | 数据目录仍在家目录 | 第 1 步的 `FRPSCTL_DATA_HOME=/opt/frpsctl` |
| 后续命令报 `实例不存在`／改动不生效 | 新 shell 丢了 `FRPSCTL_DATA_HOME` | 第 1 步的持久化（写进 `~/.bashrc`） |
| `日志目录对服务用户不可写` | `/var/log/frps` 权限不足 | `service install` 会自动创建并 chown；仍失败用 `--log-dir` 换位置 |

unit 模板（示意；`User=`/`Group=` 与各路径以实际解析结果和数据目录为准）：

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
# 实例目录也在列：ProtectSystem=strict 下其余路径只读，
# 而 frp 默认要往实例目录写 ./frps.log
ReadWritePaths=/var/log/frps /etc/frps/instances/%i

[Install]
WantedBy=multi-user.target
```

多实例天然对齐：实例名就是 systemd 的 `%i`（`frps@web.service`）。

> ⚠️ unit 的 `ExecStart` 写的是**具体二进制路径**，因此 `install` 换版本后需要
> `systemctl restart` 才生效（软链换向不影响已加载的 unit）。

### 用 systemd 托管 Web 管理台与插件

两者的部署要求与 frps 一致（frpsctl 在系统路径 / 实例目录不在家目录 / 服务
账户存在），都由安装命令在**安装前**体检；数据目录也要与 frps 一致（同一
`FRPSCTL_DATA_HOME`），否则会操作到另一个实例。账户选项（`--user` /
`--group` / `--create-user`）与 frps 完全相同——但注意 unit 模板是共享的，
同一模板下的实例共用同一个服务用户。

```bash
# Web 管理台：Restart=on-failure（交互工具，正常停止不自启）
frpsctl web service install
frpsctl web service start          # 即 systemctl start frpsctl-web@<实例>

# 插件：Restart=always（登录单点，退出必须立刻拉起）
frpsctl plugin service install
frpsctl plugin service start       # 即 systemctl start frpsctl-plugin@<实例>
```

> 从零开始的完整步骤（含账户 / 路径坑）见
> [systemd 部署速查：从零开始](#systemd-部署速查从零开始)。

### 升级 frps

```bash
frpsctl install --version 0.72.0                    # 下载 + 校验 + 落盘 + 换软链
frpsctl install --version 0.72.0 --only-download    # 只落盘，稍后统一切换
frpsctl install --version 0.72.0 --mirror URL       # 指定镜像源（也可用 FRPSCTL_MIRROR）
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

## 参考手册

### 退出码（脚本化契约）

| 码 | 含义 | 典型触发 |
|----|------|---------|
| 0 | 成功 | |
| 1 | 未分类错误 / `doctor` 发现 ERROR | 意外异常；或体检不通过 |
| 2 | 用法 / 参数错误 | 未知选项、缺子命令 |
| 3 | 配置非法 | 语义校验或 `frps verify` 拒绝；键不存在；状态文件损坏 |
| 4 | 二进制缺失 / 不可执行 / 版本不受支持 | 未 install，或版本 `< 0.70.0` |
| 5 | 实例未运行 | `stop` 时无进程 |
| 6 | 实例已在运行 | 重复 `start` |
| 7 | dashboard 不可达 / 未启用 | `clients` / `proxies` / `traffic` / `prune` 时 `webServer.port = 0`；v2 API 缺失 |
| 8 | 权限不足 | 需要 root 的操作 |
| 9 | 变更已自动回滚 | 配置写入后启动/健康检查失败，已恢复上一版 |
| 10 | 启动失败 / 进程停不下来 | 启动即退出（附 frp 原始报错）；SIGKILL 后仍存在 |
| 11 | 进程所有权冲突 | 身份校验不通过；systemd 与 direct 混用；锁被占用 |
| 12 | 已启动但健康检查未通过 | L1 进程在、L2 控制面不可达（进程仍被托管，见健康分层） |

> 两个**信号惯例**退出码不在业务表内：Ctrl-C（SIGINT）以 **130**（128+2）结束；
> 管道提前关闭（`frpsctl log | head`）按正常终止处理（退出码 0）。

脚本里应当据此分支：

```bash
frpsctl start
case $? in
  0)  echo "启动成功" ;;
  6)  echo "已在运行，跳过" ;;
  4)  echo "需要先 frpsctl install" >&2; exit 4 ;;
  10) echo "启动失败，查看 frpsctl log" >&2; exit 10 ;;
  12) echo "进程已启动但控制面异常，查看 frpsctl status" >&2; exit 12 ;;
  *)  exit 1 ;;
esac
```

### 环境变量与全局选项

| 环境变量 | 作用 |
|---------|------|
| `FRPSCTL_INSTANCE` | 默认实例名 |
| `FRPSCTL_ROOT` | 实例根目录（默认 `~/.local/share/frpsctl/instances`） |
| `FRPSCTL_DATA_HOME` | 数据根目录（默认 `$XDG_DATA_HOME/frpsctl`） |
| `FRPSCTL_ADMIN_PASSWORD` | dashboard 口令（优先于配置文件） |
| `FRPSCTL_PLUGIN_POLICY` | 插件策略文件路径 |
| `FRPSCTL_MIRROR` | frps 下载镜像（逗号分隔；`install --mirror` 优先于它） |
| `FRPSCTL_WEB_PASSWORD` | Web 管理台登录口令（`web serve --password` 优先于它） |
| `FRPSCTL_TRACEBACK` | 设为 `1` 时打印完整回溯（排查未分类错误用） |

全局选项见 [上文](#全局选项与命令总览)。凭据优先级：
`--admin-password` > `FRPSCTL_ADMIN_PASSWORD` > 配置文件里的 `webServer.password`。

### Web 管理台的纵深防御（v0.3.0）

| 机制 | 说明 |
|------|------|
| CSP nonce | 前端脚本/样式每请求 nonce，无 `'unsafe-inline'`；追加 `base-uri` / `form-action` / `frame-ancestors` 限制 |
| 条件请求 | 已认证 GET 才参与 ETag/304；写响应、错误、登录与 `/api/session` 一律 `no-store` |
| 操作审计 | 登录与变更动作写入实例目录 `web-audit.jsonl`（来源 IP + 会话**指纹**，不落 token）；失败登录审计限速，防爆破刷爆 |
| 并发护栏 | 请求线程有界 + `503`，慢后端不堆积线程 |
| `/metrics` | 默认关闭；开启后需 Basic auth（口令 = 管理台口令） |

### 目录布局

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
        ├── web-password          # Web 管理台口令文件（web service install 生成，0600）
        ├── plugin-policy.json    # 插件策略（若使用插件）
        ├── plugin-audit.jsonl    # 插件审计
        ├── service.json          # systemd 安装留档（user/group/log_dir…，0600）
        ├── config-history/       # 最近 10 份配置快照（0600）+ meta.json
        │   └── 0001-20260915-211140/
        └── startup/              # 最近 3 份启动日志（0600，诊断"启动即退出"）
```

`state.json` 记录 `{pid, start_time, binary, config, version, started_at, owner}`——
其中 `start_time` 是识别 pid 复用的唯一依据，`binary` 存的是**真实路径**（不是软链，
否则换版本后身份校验会失配）。

`service.json` 是 systemd 服务的**安装留档**（v0.3.2）：记录三个服务各自实际
使用的 `user`/`group` 与渲染参数（`log_dir`/`bind` 等），供卸载提示与 `doctor`
部署检查跟随实际配置；不装任何 systemd 服务时该文件不存在。

### 备份与回滚

每次 `config set` / `config unset` / `config edit` / `config rollback` 都会先把当前
配置存进 `config-history/NNNN-<时间戳>/`（保留最近 10 份，含 `meta.json` 记录操作
与结果）。

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
状态不可信时猜测——因为猜错的代价是杀掉无关进程。Web 管理台遇到同一情况会在
页面顶部显示红色横幅。

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

### `service install` 被拒（服务用户 / 家目录）

报错集中在三类：**服务账户不存在**、**frpsctl 装在 `/root` 或家目录下**、
**实例目录在家目录下**（后两者是 `ProtectHome=true` 的挂载隔离，改权限无效）。
逐条处置见 [systemd 部署速查：从零开始](#systemd-部署速查从零开始) 的"常见坑"表格。

### Web 管理台打不开 / 登录不了

- **打不开**：确认 `web serve` 还在前台运行（`Ctrl-C` 会停掉它）；systemd 托管时
  `frpsctl web service status` 看 `active`。
- **口令不对**：自动生成的口令只在启动时显示一次；systemd 部署的口令用
  `frpsctl web password show` 取回。
- **连续失败后被拒**：登录失败限速（60 秒 5 次），表现与"口令错误"完全一致
  ——等一分钟后重试；反代部署请加 `--trusted-proxy`，否则所有人的失败会算在同一来源上。

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

### dashboard 鉴权的真实语义（实测）

"两者全空 = 完全不鉴权"容易让人以为"只要填了 user 就安全了"。真机上逐项实测
（frps 0.71.0）后，完整语义是这样的：

| `webServer.user` | `webServer.password` | 无 `Authorization` 头 | `user:(空口令)` |
|---|---|---|---|
| `"admin"` | 未设 / 空 | **401** | **200** |
| 未设 | 未设 | **200** | 200（任意凭据均可） |
| `"admin"` | `"secret"` | 401 | 401（须 `admin:secret`） |
| 未设 | `"secret"` | 401 | 401（须 `:secret`） |

两条推论：

1. **鉴权开关是"任一非空即启用"**。因此 `frpsctl` 只拒绝"两者全空 + 绑非回环"
   ——那是真·完全不鉴权；而"有 user、口令为空"属于**强度不足**，由 `doctor`
   报 **WARN** 而不是否决写配置。
2. **Basic Auth 里的空口令是合法口令**。`user = "admin"` 而 `password = ""` 时，
   任何人用 `admin` + 空口令就能进 dashboard——等于只用用户名保护。`doctor`
   会明确报出来。

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
.venv/bin/pytest                       # 全部 1008 条（契约层缺二进制时自动 skip）
.venv/bin/pytest -m "not contract"     # 快速回归（973 条）
.venv/bin/pytest --cov=frpsctl         # 覆盖率（CI 门禁 80%）
.venv/bin/ruff check src/ tests/       # 静态分析
node --test tests/frontend/*.test.mjs  # 前端纯逻辑模块单测（直接 import 生产模块）
```

CI（Linux，Python 3.11/3.12/3.13/3.14）还包含：ruff、覆盖率门禁、真 frp 0.71.0
契约层、**真 frp 0.70.0 下界契约矩阵**、无二进制降级路径、端到端冒烟与
Web 管理台冒烟（含配置差异接口）、**前端模块守卫**（逐模块 ESM `node --check`
+ import 图核对 + 禁 innerHTML/外部域 + JS 与 HTML 的 id 双向核对）与
**`node --test` 纯逻辑单测**、
**文档一致性守卫**（README/设计文档/API 表 vs 代码的交叉核对；命令/退出码/环境变量三表由 `python -m frpsctl.docs check` 与代码双向对账）与**命令面生成物守卫**（`python -m frpsctl.cli.introspect --check`：Web「命令」视图的数据必须与 CLI 命令面一致）。

### 测试分七层

| 层 | 文件 | 目标 |
|----|------|------|
| 单元 | `tests/test_units.py` | 进程原语、锁、原子写、无损补丁、标志构造、机密打码、**二进制解包与复验**、**systemd unit 渲染与部署体检** |
| 集成 | `tests/test_integration.py` | 生命周期与回滚（假 frps 驱动确定性故障） |
| CLI | `tests/test_cli.py` | 退出码契约、`--json` 形态、机密不外泄、全局选项位置、`config edit` 闭环 |
| 契约 | `tests/test_facts.py` | **设计文档事实基线的自动化守卫**（需真 frps） |
| 故障注入 | `tests/test_faults.py` | 注入系统调用失败，验证异常路径的五项不变量 |
| 插件 | `tests/test_plugin.py` | 协议报文、裁决、审计、配额；含真 frpc 端到端契约 |
| 前端与文档 | `tests/test_web_frontend.py`、`tests/frontend/*.test.mjs`、`tests/test_docs.py` | 前端**模块**守卫（ESM `node --check`、import 图、禁 innerHTML/外部域、CSS 变量对齐、id 双向核对）、`node --test` 纯逻辑真单测与 README / 设计文档 / API 表的双向一致性 |

让契约层跑起来（需要真实二进制）：

```bash
frpsctl install --with-frpc        # 一次下载，同时得到 frps 与 frpc
export FRPSCTL_TEST_BINARY=~/.local/share/frpsctl/bin/frps-0.71.0
export FRPSCTL_TEST_FRPC=~/.local/share/frpsctl/bin/frpc-0.71.0
.venv/bin/pytest tests/test_facts.py tests/test_plugin.py -m contract
```

没有 frpc 时，`tests/test_plugin.py::TestRealFrpcContract` 的 4 条会 **skip**（不是
fail）——它们是"插件真的接得住 frp 调用"的唯一证明，因此宁可跳过也不删掉。

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
| **`start --foreground` 不写 state** | 前台模式只用于调试：进程不进入本工具的托管视图，`stop` 管不到它（`--help` 里已警示） |
| **不做并发连接上限** | 它只能在 `NewUserConn` 上实施，而那落在每次用户连接的关键路径上、错误只以 info 级记录、且回调内容里没有连接 id。需要真并发限制请在 frpc 侧用连接池与限流 |
| **`max_proxies` 计数需配 `admin_url`** | 不配时只在插件进程内计数（重启归零、多实例各算各的），`plugin check` 会告警 |
| **frps 没有热重载** | 改配置必然重启，因此 `config set` 的设计目标就是"失败了要能退回去" |
| **systemd 模式下 pid 文件不参与判定** | 所有权委托 systemctl；`install` 换版本后需 `systemctl restart` |
| **同一 unit 模板的实例共享一个服务用户** | `frps@.service` / `frpsctl-web@.service` / `frpsctl-plugin@.service` 都是模板：`User=` 是模板级参数，改它会影响**所有**使用该模板的实例（`--force` 覆盖时会明确警告）。per-instance 不同用户需要 systemd drop-in，属未做项 |
| **systemd 服务账户不自动删除** | 账户可能另有用途，`uninstall` 只按安装留档逐个提示 `userdel` 命令，不代删（v0.3.2） |
| **Web 管理台并发有界** | 请求线程上限默认 32，超限直接 `503`（v0.3.0 起；此前无限排队）。单机工具不面向高并发，公网暴露仍建议前置反代限流 |
| **插件服务并发有界（64）** | 超限连接立即 `503`；高并发冲撞下部分连接会由内核按 TCP 语义重置（同为"该操作失败"，**刻意不做等待**以免拖慢 accept——frp 对插件请求没有超时）。插件请求极轻，正常规模远达不到（v0.3.1 起与 Web 共用同一实现，accept 队列 256） |
| **`/api/status` 有 6 秒服务端缓存** | 页面轮询与外部变化最多滞后一个 TTL；管理台自身的写操作会立即失效缓存，注册登录等即时操作不受影响（v0.3.1） |
| **趋势汇总有 6 秒整体预算** | 慢 dashboard 下汇总可能只含部分代理，响应带 `partial` 标记（CLI 与 Web 图表均如实提示，不会把部分数据当全量）（v0.3.1） |
| **`--trusted-proxy` 只信 X-Forwarded-For 的最后一跳** | 前提是前面确实有一层会重写该头的可信代理；直连部署不要开启 |
| **Web 无 WebSocket/SSE 推送** | 5 秒轮询足够，且省掉长连接的生命周期管理；日志面板用**增量 tail**（`?since=offset`）减少重复传输（v0.3.4） |
| **Web 前端是同源多文件（零构建链）** | v0.3.4 起从单文件拆为 ESM 模块 + 独立 CSS（`/static/*`，白名单扩展名 + ETag 条件请求 + `no-cache`）；**零外部域**与 CSP nonce-only 不变。不引入任何构建链/第三方前端依赖 |
| **Web 不做 DOM 级前端测试框架** | 引入 jsdom/截图框架会破坏"零外部域"这个安全资产；语法与静态纪律（含 **import 图**：路径存在/符号有导出/无孤儿模块）由 CI 守卫，**纯逻辑模块由 `node --test` 直接 import 断言**（v0.3.4 起；v0.3.5 增至 34 条），DOM 分支仍靠人工点验（残余风险已记账） |
| **版本安装任务不可取消** | 下载中断需要 core 级取消协议（协作式读取 + 事件）；页面提示等待完成。任务单飞行、有界 8 条，版本号格式与 bin 目录可写性在提交时同步校验（v0.3.4） |
| **Web 的版本管理不暴露 `--insecure/--mirror`** | 始终用默认镜像 + 官方 sha256 强校验；镜像与跳过校验属于 CLI 的显式权力（v0.3.4） |
| **登录页有一个免认证端点** | `GET /api/login-info`（v0.3.5）只返回实例名、frpsctl 版本、frps 门槛与"是否非回环"——版本号可从 PyPI/仓库公开推知，不构成新泄露；响应键集合由测试锁死并断言不含敏感词。免认证入口因此只有 `POST /api/login` 与它两个 |
| **Web 自重启只支持 systemd** | `POST /api/actions/web-restart`（v0.3.5）先返回应答、延迟 1 秒再由后台线程 `systemctl restart`（同步执行会在响应写回前杀掉自己）；direct 后台模式没有"自杀后重新拉起"的机制，接口明确拒绝并给 CLI 指引（`frpsctl web restart`） |
| **会话管理不做持久化** | 会话只存服务端内存（管理台重启即全部失效）；"登出其他所有会话"保留当前会话，被登出方下一次请求 401 自动回登录页。会话列表只暴露指纹/来源/到期，**绝不含 token**（v0.3.5） |
| **审计过滤后的统计仍是全量** | `GET /api/audit` 的 `stats` 是所选时间窗内的**全量**统计，不随记录过滤变化（响应在 `page.filters` 回显生效条件，界面有文字说明）；导出走同一套过滤条件（上限 `MAX_EXPORT_ROWS`，超出时由响应头 `X-Export-Truncated` / `X-Export-Records` 声明，**正文保持严格可解析**）（v0.3.5） |
| **命令视图的数据是构建产物** | `web/static/js/data/commands.js` 由 `cli/introspect.py` 从 Typer 反射生成（与命令面契约快照、`--install-completion` 的补全同源）。CI 跑 `introspect --check` 断言无漂移——界面不会落后于 CLI，但**改命令面后必须重新生成并提交**（v0.3.5） |
| **不实现 OIDC 协议** | `auth.method = "oidc"` 的配置完整性与 doctor 提示已支持（v0.3.0）；OIDC 协议本身由 frps 实现——本工具绝不重新实现 frp 已有能力 |
| **一个 Web 进程服务一个实例** | 多实例请起多个 `web serve`（各自 `--instance` 区分）；管理台不做实例切换器 |
| **direct 后台无开机自启** | `web start` / `plugin start` 是"进程级后台化"（非 systemd）：机器重启后不自启——systemd 环境用 `service install`，容器用 restart policy（v0.3.3） |
| **systemd 与 direct 后台互斥** | 同一实例的 Web/插件只能被一种模式托管；两个方向都会拒绝并给出切换命令（v0.3.3）。`uninstall` 对运行中的 direct 服务同样拒绝（`--force` 先停），不留孤儿 |

---

## 许可

frpsctl 自身代码以 **MIT** 发布，全文见 [LICENSE](LICENSE)。

本仓库**不包含** frp 的源代码或二进制。`frpsctl install` 会按需从官方发布页下载
`frps`，该二进制是独立第三方软件，遵循其自身的 **Apache-2.0**，不随本包分发。
详见 [NOTICE](NOTICE)。
