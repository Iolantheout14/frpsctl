# frpsctl 设计文档

> **用 Python 把 frp 服务端（frps）包装成命令行工具**
>
> | 项 | 值 |
> |---|---|
> | 目标二进制 | `frps` v0.71.0（fatedier/frp） |
> | **目标平台** | **Linux（仅此一个）**，见 §3.4 |
> | 运行时 | Python ≥ 3.11（依赖标准库 `tomllib`） |
> | 事实基线 | frp 源码 tag `v0.71.0` + 逐版本源码对比（v0.51.3 – v0.71.0），核对方法与可复现命令见附录 B |
> | 文档状态 | 可实施（问题 1–5 已闭环，变更记录见 §16） |
>
> **设计哲学一句话**：frps 是黑盒服务，Python 只做**配置翻译器、进程保镖、状态聚合器**——转发逻辑一行都不碰，生命周期走进程管理，配置校验走官方 `verify`，状态采集走官方 Admin API。

---

## 目录

1. [概述](#1-概述)
2. [目标与非目标](#2-目标与非目标)
3. [事实基线](#3-事实基线)
   - 3.1–3.5 二进制接口 / Admin API / 鉴权 / 平台 / 停机语义
   - [3.6 版本门槛：只接受 ≥ 0.70.0](#36-版本门槛只接受-0700)
   - [3.7 健康语义：三层](#37-健康语义三层且只有两层有回滚权)
4. [设计决策](#4-设计决策adr)
5. [架构](#5-架构)
6. [实例模型与目录布局](#6-实例模型与目录布局)
7. [命令行契约](#7-命令行契约)
8. [核心模块设计](#8-核心模块设计)
9. [配置变更闭环](#9-配置变更闭环)
10. [安全基线](#10-安全基线)
11. [服务端插件](#11-服务端插件)
12. [分发与部署](#12-分发与部署)
13. [测试策略](#13-测试策略)
14. [里程碑与工作量](#14-里程碑与工作量)
15. [风险登记表](#15-风险登记表)
16. [设计评审变更记录](#16-设计评审变更记录问题-15-闭环)
17. [实现验证记录](#17-实现验证记录m0m5-已落地)
- [附录 A：frps 配置键速查表](#附录-afrps-配置键速查表)
- [附录 B：事实复核清单](#附录-b事实复核清单)
  - [附录 B-2：§3.6 版本矩阵的复核命令](#附录-b-236-版本矩阵的复核命令)

---

## 1. 概述

### 1.1 问题

直接用官方 `frps` 管理服务端，有四件事必须手工完成，且都容易出错：

1. **手写 TOML**：字段名是驼峰、嵌套结构、合法取值范围分散在文档各处，写错了要等启动才报错。
2. **配进程守护**：`frps -c frps.toml` 前台运行，关掉 SSH 就断；要后台化得自己写 nohup/setsid，还要管 pid 文件、优雅停止、开机自启。
3. **看状态靠翻日志**：谁在线、有几个代理、今天跑了多少流量——官方 dashboard 是给浏览器看的，终端里想快速取数得另想办法。
4. **改配置必然重启**：frps **没有热重载**（这是它和 frpc 的关键差异，frpc 有 `/api/reload`，frps 没有），改一个端口就要停服重启，而重启失败意味着服务中断。

### 1.2 解决思路

`frpsctl` 把这四件事收敛为一组命令，并遵守一条硬边界：**绝不重新实现 frp 已有的能力**。

| 能力 | 由谁提供 |
|------|---------|
| 配置合法性判定 | 官方 `frps verify -c`（唯一权威） |
| 状态、统计、代理列表 | 官方 dashboard Admin API |
| 服务进程 | 官方 `frps` 二进制，或 systemd |
| 配置生成、进程编排、失败回滚、终端呈现 | **frpsctl** |

### 1.3 交付物

```
frpsctl                          命令行程序（pip/pipx 安装）
├── 主命令集                      init / start / stop / restart / status / log
├── 配置子命令                    config get|set|edit|diff|rollback
├── 运维子命令                    install / service / doctor / kick
└── 可选：插件服务                独立进程，多用户鉴权与端口白名单（§11）
```

外部依赖只有四个纯 Python 包（`typer` / `pydantic` / `tomlkit` / `httpx`），其余全部走标准库。

---

## 2. 目标与非目标

### 2.1 目标

| # | 目标 | 可验证标准 |
|---|------|-----------|
| G1 | 不手写 TOML | `init` 问答生成可用配置；`config set` 改单键**且不破坏已有注释与排版** |
| G2 | 不记命令行参数 | `start/stop/restart/status/log` 语义明确、幂等、可脚本化 |
| G3 | 变更可回滚 | 任何配置变更在"校验通过但启动失败"时自动恢复上一版并明确告知 |
| G4 | 状态可观测 | 一条命令给出进程、健康、客户端、代理、流量；支持 `--json` |
| G5 | 默认安全 | `init` 产出的配置开箱即处于合理安全基线（§10） |
| G6 | 失败可诊断 | 启动失败时回显 frp 原始错误，而非仅给一个退出码 |

### 2.2 非目标

- **不实现任何转发逻辑**，不 fork、不 patch frps，二进制始终是官方原版。
- **不做 Web UI**：frps 自带 dashboard 已经够好，本工具只做终端侧聚合。
- **不管 frpc**：只做服务端。`frpc reload` 这类能力不在范围内。
- **只支持 Linux**：唯一目标平台是 Linux，进程身份、锁、原子写、信号语义全部建立在 Linux 原语之上（§3.4）。**不实现 macOS / Windows 兼容分支，也不做运行时降级**——非 Linux 平台在启动时直接拒绝，而不是"尽力而为"。
- **v1 不实现多租户配额**：那属于插件层（§11），列为独立阶段。

---

## 3. 事实基线

> 以下每条均已在 frp `v0.71.0` 源码或官方发布资产上实测确认。附录 B 给出可复现命令。
> **这些事实是整个设计的地基**：凡与之冲突的设计都必须让步。

### 3.1 二进制接口

| 入口 | 事实 | 证据 |
|------|------|------|
| `frps -c <file>` | 启动服务 | `cmd/frps/root.go` |
| `frps verify -c <file>` | 校验配置；非法时 `os.Exit(1)`；成功打印 `frps: the configuration file X syntax is ok` | `cmd/frps/verify.go` |
| `frps -v` | 输出版本号 `0.71.0`（**无 `v` 前缀**） | `pkg/util/version/version.go`：`Full()` 直接返回 `version` |
| `--strict_config` | **默认为 true**：配置里出现未知字段直接报错退出。**代码里的名字是下划线形式**（`strict_config`），`--help` 由 pflag 渲染成 **`--strict-config`**（连字符）；实测**两种写法都被接受** | `cmd/frps/root.go` |
| `--allow-unsafe` | `ServerUnsafeFeatures = ["TokenSourceExec"]`；配置用了 `auth.tokenSource` 的 exec 源却不加此标志 → 校验失败 | `pkg/policy/security/unsafe.go` |
| 配置格式 | TOML / YAML / JSON，INI 为遗留（启动时打印 deprecation 警告） | `pkg/config/load.go` |
| 键名规则 | TOML 先被解析为对象、再转 JSON 解码，**因此键名 = 结构体 json tag（camelCase）** | `pkg/config/load.go` |
| 模板渲染 | 配置文本在解析前会做 Go `text/template` 渲染（`{{ .Envs.X }}`、`parseNumberRange`） | `pkg/config/load.go` |

> **推论 1**：`--strict_config=true` 是免费的护栏——frpsctl 写错键名不会静默生效，而是硬报错。设计上要主动利用它：**落盘前先跑 `verify`**（§9）。
>
> **推论 2**：模板渲染意味着**写进配置文件的字符串里若含 `{{`，会被 frp 当模板求值**。因此 `config set` 必须拒绝含模板语法的值（§8.4）。

### 3.2 Admin API（dashboard）

dashboard 仅在 `webServer.port > 0` 时启动；`port = 0` 表示**完全不启动**。

**本工具只用 v2**（ADR-3，版本门槛保证 ≥ 0.70.0），因此下表只列 v2 端点与两个 v1 遗留端点——后者仅保留为"知道它存在"，不实现：

| 端点 | 鉴权 | 本工具是否使用 |
|------|------|--------------|
| `GET /healthz` | **免认证** | ✔ L2 控制面探针（§3.7） |
| `GET /api/v2/system/info` | Basic | ✔ 信封 `{code,msg,data}`；配置与统计一次拿全 |
| `GET /api/v2/clients`、`/api/v2/clients/{key}` | Basic | ✔ 客户端列表 / 详情 |
| `GET /api/v2/proxies`、`/api/v2/proxies/{name}`、`/api/v2/proxies/{name}/traffic` | Basic | ✔ 代理列表 / 详情 / 流量序列（带分页） |
| `GET /api/v2/users` | Basic | ✔ 按用户聚合 |
| `POST /api/v2/system/prune` | Basic | ✔ 供未来 `stats prune` 使用 |
| `DELETE /api/proxies` | Basic | ✔ `kick` 用（v1 路径，v2 无对应写接口） |
| `GET /api/proxy/{type}` | Basic | ✔ 逐类型代理列表（`{"proxies":[...]}`，用于 §13.1 C1 的交叉验证） |
| `GET /api/serverinfo`、`GET /api/clients` | Basic | ✘ **v1 遗留**，不实现（ADR-3） |
| `GET /metrics` | Basic | ✘ 仅当 `enablePrometheus = true`，超出 v1 范围 |

`v2` 与 v1 模型有一个**共同的字段陷阱**：

```go
// server/http/model/v2.go:50   （v1 的 server/http/model/types.go:40 同名字段，本工具不解析）
ProxyTypeCounts map[string]int64 `json:"proxyTypeCount"`
```

`proxyTypeCount` 是 **按代理类型的计数字典**（如 `{"tcp":7,"http":3}`），**不是总数**。要展示总数必须自己求和，直接当整数用会打印出一个 dict。

**真机实测的完整字段形状**（v0.71.0，实现期核对；两处都曾把实现带偏）：

| 字段 | JSON 键 | 形状 | 陷阱 |
|------|---------|------|------|
| 代理计数 | `proxyTypeCount` | `dict[str, int]` | 键名**不是** `proxyCount`；值不是总数 |
| 客户端计数 | `clientCounts` | **`int`** | **不是字典**——按字典处理会直接 `TypeError` |
| 当前连接 | `curConns` | `int` | |
| 今日流量 | `totalTrafficIn` / `totalTrafficOut` | `int` | |
| TLS 强制 | `config.tlsForce` | `bool` | 在 `data.config` 下，不在 `status` 下 |

> **字段名写错的代价特别隐蔽**：`dataclass` 的默认值（空 dict / 0）让解析"看起来成功了"，只是数字永远是 0。这正是 §13.1 C1 必须存在的原因——它同时断言字段名与形状。

### 3.3 鉴权与默认绑定

```go
// pkg/util/net/http.go:48
if (authMid.user == "" && authMid.passwd == "") || (hasAuth && ...) { next.ServeHTTP(w, r) }
```

- `webServer.user` 与 `webServer.password` **同时为空时完全不鉴权**——不是"弹窗要求登录"，是**任何人都能读全部状态、并下线任意代理**。这是本设计里最重要的安全事实。
- 默认绑定：`ServerConfig.Complete()` 先调 `WebServer.Complete()` 把 `addr` 兜底为 `127.0.0.1`，之后那个 `"0.0.0.0"` 分支因值已非空而永不生效。**因此默认只监听本机**——这是好消息，但不能依赖它，因为用户为了远程看 dashboard 常会手动改成 `0.0.0.0`。

### 3.4 平台语义：唯一目标平台是 Linux

frps 只部署在 Linux 服务器上，因此本工具**只面向 Linux**，不做任何跨平台兼容层。这不是"暂时没做"，而是明确的范围决策：每支持一个平台，进程身份校验、锁、原子写、信号四件事都要各写一套分支并各自验证，收益为零而风险翻倍。

**目标平台基线**（这些是设计的地基，缺一不可）：

| 依赖 | 用途 | 缺失后果 |
|------|------|---------|
| `/proc/<pid>/stat` | 进程启动时刻 → 识别 pid 复用（§8.1） | 无法安全停止，退化为"可能误杀" |
| `fcntl.flock` | 实例级互斥（§8.2） | 并发 start 可能双起 |
| `os.fchmod` / `os.O_DIRECTORY` | 无权限窗口的原子写（§8.4） | 短暂出现 0644 的含密配置 |
| POSIX 信号 | SIGTERM / SIGKILL（§3.5） | 无法停止 |

**仍然必须绕开的坑（POSIX 语义）：**

| 坑 | 事实 | 设计要求 |
|----|------|---------|
| **`PermissionError` 表示进程存在** | POSIX 下 `kill(pid, 0)` 对"存在但不属于当前用户"的进程抛 `PermissionError` 而非 `ProcessLookupError` | 必须判定为**存活**；判成"未运行"会导致重复启动、端口冲突 |
| **非 Linux 内核直接拒绝** | 没有 `/proc` 的系统（macOS、容器内挂载受限等）无法做进程身份校验 | 启动时探测 `/proc/self/stat` 与 `fcntl`，失败即 `EXIT_USAGE`，并提示"frpsctl 仅支持 Linux" |

> 平台检查放在 `core/platform.py` 的 `assert_supported()`（§8.1），由进程入口最前端调用：**宁可拒绝启动，也不进入"身份校验失效"的降级路径**——后者的代价是杀掉无关进程（ADR-7）。

> 措辞说明：本文档在描述具体系统调用语义时保留 "POSIX" 一词（因为 Linux 是该标准的具体实现），但**目标平台、CI、测试矩阵一律只有 Linux**，不存在"POSIX 兼容层"这层抽象。

### 3.5 停机语义：frps 没有优雅停机

全仓库 `signal.Notify` **只出现在 frpc**（`cmd/frpc/sub/root.go:117`）。frps 侧 `Service.Run` 阻塞在 `<-svr.ctx.Done()`，而没有任何代码会 cancel 它。

**结论**：给 frps 发 SIGTERM 就是进程立即终止（Go 默认行为），**不存在"存量连接收尾"**。

设计要求：
- 默认停止信号仍是 SIGTERM（systemd 语义正确、为未来版本留空间），但**文档、帮助文本、日志都不得宣称 graceful drain**；
- SIGKILL 兜底是为了应对卡死/无响应，不是为了"更彻底"；
- 因为进程退出几乎无延迟，停止流程的正常路径应在数百毫秒内完成。

### 3.6 版本门槛：只接受 ≥ 0.70.0

**决策**：`< 0.70.0` 一律拒绝（退出码 4），`>= 0.70.0` 完全支持。**不做任何兼容分支、不做能力降级**。

理由有三条，其中第三条是决定性的：

1. 二进制由 `frpsctl install` 自己下载（§8.6），"用户机器上只有旧版本"这个约束不存在；
2. 旧版本存在已知安全问题：`< 0.71.0` 有"客户端传负数 `pool_count` 致 frps panic"的**远程 DoS**，`< 0.53.0` 连键名护栏都没有；
3. **v2 Admin API 是 v0.70.0 引入的**。拒绝 `< 0.70.0` 意味着**整条 v1 降级路径可以直接删掉**——`core/admin.py` 只剩一套解析逻辑、一类信封格式，契约测试的断言对象从两套减为一套。这是一次净减法。

下表在源码上逐版本核对过（复现命令见附录 B-2）：

| 观察项 | 引入/变更版本 | 证据 |
|--------|-------------|------|
| 新配置体系（TOML/YAML/JSON + camelCase JSON tag 解码） | v0.52.0 | v0.51.3 只有 `pkg/config/parse.go`（INI + `GetRenderedConfFromFile`）；v0.52.0 出现 `pkg/config/load.go` 与 `conf/frps.toml` |
| `--strict_config` 标志 | v0.53.0 | v0.52.3 `root.go` 无此串，v0.53.0 有 |
| `--strict_config` 默认值 `false → true` | v0.66.0 | v0.53.0–v0.65.1 为 `BoolVarP(…, false, …)`；v0.66.0 起为 `true` |
| `--allow-unsafe` 标志 | v0.66.0 | v0.65.1 无，v0.66.0 有（`StringSliceVarP`） |
| **Admin API `/api/v2/*`** | **v0.70.0** | `server/http/model/v2.go`：v0.69.1 无，v0.70.0 有 |

| frps 版本 | 策略 | 说明 |
|-----------|------|------|
| `< 0.70.0` | **拒绝**（退出码 4） | 无 v2 API，且普遍存在上述安全问题。报错须写明"最低支持 0.70.0"，而不是笼统的"版本不受支持" |
| `0.70.0 – 0.70.x` | 支持 + WARNING | v2 API 可用；提示已知 DoS 修复缺失，建议升到 `>= 0.71.0` |
| `>= 0.71.0` | 完全支持 | 目标版本 |

**仍需显式传标志（而不是依赖默认值）**：

`>= 0.70.0` 上 `--strict_config` 默认已是 `true`，但 `verify_flags()` **仍然显式传 `--strict_config=true`**。原因是这条护栏值一个字节的成本，而依赖"某个版本的默认值"是脆弱的——默认值在 v0.53 到 v0.66 之间就变过一次。同理 `--allow-unsafe TokenSourceExec` 按配置内容决定是否追加。

**版本解析**：正则提取 `(\d+)\.(\d+)\.(\d+)` 后转三元组，而不是对 `split(".")` 做 `int()`——后者对 `0.71.0-rc1`、`0.71.0+dev` 这类合法后缀不够稳健。解析失败按"不支持"处理（ADR-7）。

**标志构造**集中在 `core/config.py` 的 `verify_flags()`（§8.4），`verify` 与 `start` 共用同一份逻辑——"两处调用、一处判定"，避免未来只修好其中一条路径。

### 3.7 健康语义：三层，且只有两层有回滚权

**问题**：单个 `/healthz` 200 说明不了"服务可用"。结合 §11.2 的插件事实——插件 fail-closed、且 frp 侧对插件 HTTP 客户端**没有超时**——存在两种"进程活着但业务已死"的状态：

- frps 在跑，dashboard 端口没起（`webServer.port = 0`）→ 状态可观测性为零；
- frps 在跑，但插件服务挂了 → **所有客户端都无法登录**（fail-closed），而 `/healthz` 依然 200。

**因此健康判定必须分层**，每层回答一个不同的问题：

| 层 | 探针 | 回答的问题 | 依赖 |
|----|------|-----------|------|
| **L1 进程** | `pid_alive` + `/proc/<pid>/stat` 身份校验 | 进程还在，且确实是我们的进程 | §8.1 |
| **L2 控制面** | `GET /healthz`（免认证） | frps 的 HTTP 服务在正常应答 | `webServer.port > 0` |
| **L3 插件面** | 对每个 `httpPlugins[].addr` 做 TCP 连接探测 | 登录链路（Login/NewProxy 回调）是否可能成功 | 配置了 `httpPlugins` |

**关键设计约束（回滚判据）**：

```
回滚判据 = L1 ∧ L2          ← 只认这两层，失败才回滚配置（§9 第 8 步）
L3 仅展示，不回滚             ← 插件挂掉不是这份配置的错
```

这条边界必须写死。若把 L3 纳入回滚判据，会出现最坏情形：**插件服务临时抖动 → frpsctl 把一份完全正确的配置回滚掉**，而新配置恰恰是修复问题的那一份。插件的健康由插件自己的 systemd 守护负责（`Restart=always`），frpsctl 只做**归因提示**：`L1/L2 ok + L3 fail` 时明确输出"客户端将无法登录，请检查插件服务"，而不是改动 frps 配置。

**各命令的用法差异**：

| 命令 | L1 | L2 | L3 | 说明 |
|------|----|----|----|------|
| `start` / `restart` 的健康等待 | ✔（早退检测） | ✔（`--health-timeout`，默认 10s） | ✘ | `webServer.port = 0` 时 L2 自动跳过，退化为 L1 |
| 回滚判据 | ✔ | ✔ | ✘ | 见上 |
| `status` | ✔ | ✔ | ✔ | 三层全部展示（§7.4） |
| `doctor` | ✔ | ✔ | ✔ | L3 由"插件可达性"检查项承担（§8.7） |

**L3 探测实现是 TCP connect，不是 HTTP 请求**：探活只需知道端口有人在听，`socket.create_connection(host, port, timeout=1.0)` 即可。原因有二——其一，插件可能只接受 POST 且要求 `op` 参数，发 GET 会拿到 405/422 这类"服务正常但语义不符"的噪声；其二，绝不给插件增加一次真实业务调用（§11.2 要求插件 handler 绝不做慢速外部调用）。

同理 `httpPlugins[].addr` 是 URL 形式（如 `http://127.0.0.1:8080`），解析时要取 `hostname` / `port`，缺省端口按 scheme 补（http→80、https→443）。

---

## 4. 设计决策（ADR）

### ADR-1：实例所有权显式化

**问题**：同一个 frps 进程可能由 systemd 托管，也可能由 frpsctl 直接托管。若两套机制并存且互不知情，会出现"CLI 说已停止、systemd 立刻又拉起来"以及状态判定自相矛盾。

**决策**：一个实例的进程**任何时刻只有一个所有者**，运行时探测，绝不混用。

```
resolve_owner(instance):
    存在同名 unit（frps@<name>.service / frps-<name>.service） → SYSTEMD
    否则 state.json 存在                                    → DIRECT
    否则                                                     → NONE
```

- `SYSTEMD`：`start/stop/restart` 全部委托 `systemctl`，`status` 读 `systemctl show`，**pid 文件不参与任何判定**。
- `DIRECT`：以 `state.json` 为权威，做 pid + 启动时刻 + 命令行三重校验。
- `status` 输出**必须**包含 `owner=` 字段，让"谁在管这个进程"永远无歧义。
- DIRECT 模式下若探测到同一配置正被 systemd 托管 → 拒绝 `start`，退出码 11，并给出处置指引。

### ADR-2：配置文件是唯一真相，编辑走无损补丁

**问题**：若把配置读进内存模型、改一个字段、再整体序列化写回，会丢两样东西：

1. **注释与排版**——frp 官方示例配置本身就是大段带注释的文档，用户通常直接改写它；
2. **模型未覆盖的键**——`allowPorts`、`maxPortsPerClient`、`transport.*`、`httpPlugins` 等一旦被丢弃，等于**一次 `config set` 悄悄关掉了端口白名单**。

**决策**：

| 职责 | 技术选择 |
|------|---------|
| 读 | `tomllib`（标准库，只读） |
| 写 | `tomlkit`（保留注释、键序、缩进、空行） |
| 校验 | `pydantic` v2 —— **只校验候选值，不承担序列化** |
| 权威判定 | 官方 `frps verify -c` |

**落地方式**：`config set` 只对目标键做定点赋值，其余字节原样保留（§8.4 的 `plan_set`）。

### ADR-3：只用 v2 Admin API，不做运行时能力探测

**决策**：`core/admin.py` **只实现 `/api/v2/*`**。版本门槛（§3.6）保证目标二进制 ≥ 0.70.0，因此不存在"端点可能缺失"的情形。

**理由**：

- 原设计是"首次调用探一次，404 则降级 v1"。既然版本门槛已经把 `< 0.70.0` 拒绝在安装之外，这个探测就是**永远返回 200 的死代码**——留着只会多一套解析分支、多一类信封格式、多一倍的契约断言（§13.1 的 C2 原本就是为它写的）。
- 但**保留一次启动时的显式校验**：`start` 后第一次拉 `server_info` 若拿到 404，说明二进制与预期不符（例如用户用 `--binary` 指了一个自编译的怪版本）。此时**报错退出（退出码 7）并提示实际版本号**，而不是降级——ADR-7 的"不猜测"原则。

**与前一版设计的差异**：删掉了 `_v2` 缓存状态、`/api/serverinfo` 解析路径、`source` 字段，以及 §3.2 里 v1 端点的契约断言。这是一次纯减法。

### ADR-4：交付形态 = 纯 Python 包 + 按需下载二进制

**决策**：`pip install frpsctl` 只装 Python 侧；frps 二进制由 `frpsctl install` 按需下载并强校验。

**理由**：内嵌二进制（PyInstaller `--onefile`）会让每次运行都解包约 14 MB 到临时目录，还要额外处理 Apache-2.0 的 LICENSE 随包分发；而"把二进制塞进 wheel"在 Linux 上还会逼出 `manylinux` 平台标签的问题——同一个 wheel 无法同时覆盖 glibc/musl 与 x86_64/aarch64。把 frps 当**外部可替换依赖**，语义更干净，也允许用户使用发行版自带的 frps（§12.1 升级语义）。

### ADR-5：日志只有一个写入者

**决策**：

- **主日志由 frp 自己写**：`log.to` 指向实例目录，`log.maxDays` 控制轮转（frp 内部按天轮转）。
- **frpsctl 绝不把 stdout 重定向到主日志**——两个写入者抢同一文件会和 frp 的轮转互相破坏。
- frpsctl 只捕获**启动阶段**的 stdout/stderr 到 `startup-<ts>.log`（保留最近 3 份），用途单一：诊断"启动即退出"。

### ADR-6：变更即事务

**决策**：任何配置写入都视为一次事务，必须满足 **原子性 + 可验证 + 可回滚**：

```
候选生成 → 权威校验 → 原子替换 → 重启 → 健康检查 → 失败自动回滚
```

**理由**：frps 没有热重载，改配置与重启是同一件事，而重启失败等于服务中断。没有回滚的"改配置"命令，在服务器上是不负责任的。完整流程见 §9。

### ADR-7：不确定的一律拒绝，而不是猜测

**决策**：以下情形一律以"拒绝 + 明确指引"收场，而不是尝试自愈：

| 情形 | 行为 |
|------|------|
| 进程存活但身份校验不通过（pid 疑似被复用） | 拒绝停止，退出码 11 |
| 配置值含 `{{`（会被 frp 当模板求值） | 拒绝写入，退出码 3 |
| 拿不到官方校验和 | 拒绝安装二进制，退出码 4 |
| systemd 与 direct 所有权冲突 | 拒绝操作，退出码 11 |

**理由**：这四类误判的代价分别是"杀掉无关进程""配置被静默改写""执行来路不明的二进制""状态错乱"，都远高于"多敲一条命令"。

---

## 5. 架构

```
┌───────────────────────────────────────────────────────────────────┐
│ frpsctl (Python 3.11+)                                            │
│                                                                   │
│  cli/            Typer 命令层（薄：只做参数解析与输出渲染）          │
│   ├─ lifecycle_cmd.py   init / start / stop / restart / status / log
│   ├─ config_cmd.py      config get|set|edit|diff|rollback         │
│   ├─ service_cmd.py     install / service / kick                  │
│   └─ doctor_cmd.py      doctor                                    │
│                                                                   │
│  core/                                                            │
│   ├─ instance.py   实例布局与全局选项（--instance / --root）        │
│   ├─ platform.py   pid_alive / proc_start_time / proc_cmdline      │
│   ├─ lock.py       flock 实例级互斥                                │
│   ├─ lifecycle.py  所有权探测 + 状态机 + 启动早退检测               │
│   ├─ config.py     tomlkit 无损补丁 + 原子写 + 备份历史             │
│   ├─ admin.py      Admin API 客户端（只走 v2，§ADR-3）             │
│   ├─ release.py    二进制下载 + sha256 强校验                      │
│   ├─ systemd.py    unit 渲染与 systemctl 委托                      │
│   └─ doctor.py     体检与安全 lint                                 │
│                                                                   │
│  errors.py  统一异常 → 退出码映射（§7.3）                          │
└──────────┬──────────────────────────────────┬─────────────────────┘
           │ subprocess / systemctl           │ httpx (Basic Auth)
           ▼                                  ▼
┌──────────────────────────────┐   ┌─────────────────────────────────┐
│ frps 官方二进制 ≥ 0.70.0      │   │ dashboard Admin API             │
│  -c <实例>/frps.toml         │   │  /healthz        （免认证）      │
│  verify / -v                 │   │  /api/v2/...     （唯一路径）    │
└──────────────────────────────┘   └─────────────────────────────────┘
```

**分层约束**：`cli/` 不直接调用 `subprocess` 或 `httpx`，只调用 `core/`；`core/` 不打印任何东西，只返回结构化结果或抛 `errors.py` 定义的异常。这条约束让"机器可读输出（`--json`）"与"人读输出"共享同一份逻辑。

依赖清单：

| 用途 | 选型 | 说明 |
|------|------|------|
| CLI 框架 | `typer` | 类型注解自动生成参数与帮助 |
| 校验模型 | `pydantic` v2 | 字段类型、范围、默认值、嵌套模型 |
| TOML 读 | `tomllib` | Python 3.11+ 标准库 |
| TOML 写 | `tomlkit` | 注释与格式保真 |
| HTTP | `httpx` | 同步客户端，Basic Auth |
| 进程 / 锁 | `subprocess`、`os`、`fcntl` | 标准库，零额外依赖 |

---

## 6. 实例模型与目录布局

**实例**是配置、状态、日志、锁的最小隔离单位，支持一台机器跑多个 frps。

```python
@dataclass(frozen=True)
class Instance:
    name: str
    root: Path            # 默认 ~/.local/share/frpsctl/instances/<name>
    config: Path          # <root>/frps.toml
    state: Path           # <root>/state.json      ← 所有权判定的权威来源
    pidfile: Path         # <root>/frps.pid        ← 人类可读副本
    lock: Path            # <root>/.lock
    history: Path         # <root>/config-history/
    startup_dir: Path     # <root>/startup/

    @property
    def bin_link(self) -> Path:
        """共享的版本软链：~/.local/share/frpsctl/bin/frps（全局，非实例级）。"""
        return self.root.parent.parent / "bin" / "frps"

    def active_binary(self) -> Path:
        """每次启动现解软链 → 真实版本文件路径（§8.6 升级语义）。

        必须 resolve()：写入 state.json 的是**真实路径**而非软链路径，
        否则软链一换，旧进程的 cmdline 校验就会失配（§8.3 三重校验）。
        """
        if self.bin_link.exists():
            return self.bin_link.resolve(strict=True)
        raise BinaryNotFound(self.bin_link)      # 退出码 4，提示先跑 install
```

```
~/.local/share/frpsctl/
├── bin/
│   ├── frps-0.71.0            # 已校验的二进制，按版本并存
│   ├── frps-0.70.0            # 旧版本保留，便于回退
│   └── frps -> frps-0.71.0    # 当前版本软链，唯一被 install --switch 改动的对象
└── instances/
    └── default/
        ├── frps.toml          # 0600
        ├── state.json         # {"pid":…,"start_time":…,"binary":"<真实路径 frps-0.71.0>",
        │                      #  "config":…,"version":…,"started_at":…,"owner":"direct"}
        ├── frps.pid           # 兼容性副本：仅 pid，方便人工排查
        ├── frps.log           # 由 frp 自己写并轮转（ADR-5）
        ├── .lock
        ├── config-history/    # 最近 10 份配置快照 + meta.json
        └── startup/           # 最近 3 份启动日志
```

**`bin/` 是全局共享的，`instances/` 是实例私有的**——这条边界决定了升级的粒度：换版本影响所有实例，但**已运行的进程不受影响**（§8.6）。

服务端场景可用 `--root /etc/frps` 配合 `--instance`；systemd 模式下实例名映射为 `frps@<name>.service` 的 `%i`，与目录布局天然对齐。

---

## 7. 命令行契约

### 7.1 全局选项

```
--instance, -i NAME   实例名（默认 default，可用 FRPSCTL_INSTANCE 覆盖）
--root PATH           实例根目录（默认 ~/.local/share/frpsctl/instances）
--config PATH         直接指定配置文件（覆盖实例默认）
--binary PATH         直接指定 frps 二进制
--json                机器可读输出（所有查询类命令支持）
--yes                 跳过交互确认
-v, --verbose
```

### 7.2 命令表

| 命令 | 语义 | 关键行为 |
|------|------|---------|
| `frpsctl install [--version 0.71.0] [--force] [--only-download]` | 获取 frps 二进制 | 下载 → **强校验 sha256** → 落盘为 `frps-<version>` → `-v` 复验 → 换软链（§8.6.1）。`< 0.70.0` 直接拒绝 |
| `frpsctl init` | 交互式生成配置 | 强制随机口令、`tls.force=true`、引导设置 `allowPorts`；已存在则要求 `--force` 并先备份 |
| `frpsctl verify [--file P]` | 双保险校验 | pydantic 语义校验 + `frps verify`；用临时副本，不动线上文件 |
| `frpsctl start [--foreground]` | 启动 | verify → 加锁 → 派生进程 → **早退检测** → 写 state → 健康检查 |
| `frpsctl stop [--force] [--timeout 10]` | 停止 | SIGTERM → 轮询确认退出 → 超时 SIGKILL；身份不符则拒绝 |
| `frpsctl restart [--health-timeout 10] [--timeout 10]` | 重启 | stop → start → 健康检查。**没有 `--no-rollback`**：重启不读配置，也就没有"新旧版本"可比，自动回滚只属于配置变更路径（`config set` / `edit` / `rollback`） |
| `frpsctl status [--watch]` | 状态聚合 | `owner / 状态 / pid / 版本 / 运行时长 / 客户端 / 各类型代理 / 今日流量 / 健康` |
| `frpsctl config get <key>` | 读单键 | 点分路径，如 `transport.tls.force` |
| `frpsctl config set <key> <value> [--no-restart]` | 写单键 | 走 §9 事务闭环 |
| `frpsctl config edit` | `$EDITOR` 编辑 | 保存后走同一闭环 |
| `frpsctl config diff` | 当前 vs 上一份快照 | unified diff |
| `frpsctl config rollback [N]` | 回滚到 N 份之前 | 同样走闭环 |
| `frpsctl log [-f] [-n 100]` | 看日志 | tail `log.to`；缺失时回退到 startup 日志 |
| `frpsctl service install\|uninstall\|status` | systemd 集成 | 渲染 unit + `daemon-reload` + `enable`（需 root，明确提示） |
| `frpsctl doctor [--json]` | 体检 | §8.7 检查项，按 severity 输出 |
| `frpsctl kick <proxy-name>` | 下线指定代理 | `DELETE /api/proxies` |

### 7.3 退出码

脚本化契约，`errors.py` 统一映射：

| 码 | 含义 | 典型触发 |
|----|------|---------|
| 0 | 成功 | |
| 1 | 未分类错误 | 意外异常 |
| 2 | 用法 / 参数错误 | Typer 参数校验失败 |
| 3 | 配置非法 | pydantic 或 `frps verify` 拒绝 |
| 4 | 二进制缺失 / 不可执行 / 版本不受支持 | 未 install，或版本 `< 0.70.0`（§3.6） |
| 5 | 实例未运行 | `stop` 时无进程 |
| 6 | 实例已在运行 | 重复 `start` |
| 7 | dashboard 不可达 | 网络不通 / 未启用 / 鉴权失败 |
| 8 | 权限不足 | 需要 root 的操作 |
| 9 | 变更已自动回滚 | 配置写入后启动失败，已恢复上一版 |
| 10 | 启动即失败 | 附 frp 原始错误输出 |
| 11 | 进程所有权冲突 | 身份校验不通过 / systemd 与 direct 混用 |

### 7.4 输出示例

```
$ frpsctl status
instance : default            owner : direct
state    : RUNNING (pid 12345, up 2h13m)
binary   : frps 0.71.0        (/home/u/.local/share/frpsctl/bin/frps-0.71.0)
config   : /home/u/.local/share/frpsctl/instances/default/frps.toml (0600)
listen   : 0.0.0.0:7000       dashboard: 127.0.0.1:7500 (auth: on)
health   : L1 process ok  L2 control ok (/healthz 200, 3ms)  L3 plugin ok (1 addr)
clients  : 4 online
proxies  : tcp=7  http=3  udp=1        total 11
traffic  : today in 1.2 GiB / out 3.4 GiB  (conns now 12)
```

未配置的层显示为 `skipped`，插件挂掉时**不改变退出码**（进程与配置都没问题），但必须显著提示：

```
health   : L1 process ok  L2 control ok (/healthz 200, 3ms)  L3 plugin FAIL (127.0.0.1:8080)
           ⚠ 插件不可达：客户端将无法登录（fail-closed），请先恢复插件服务（§11.2）
```

---

## 8. 核心模块设计

### 8.1 `core/platform.py` —— Linux 进程原语

本模块是**唯一**允许直接触碰 `os.kill` / `/proc` / `signal` 的地方，其余模块一律通过它访问。

```python
from __future__ import annotations

import errno
import os
import signal
from pathlib import Path

REQUIRED_PROC = Path("/proc/self/stat")


class UnsupportedPlatform(RuntimeError):
    """非 Linux 内核：/proc 不可用则进程身份校验无从谈起（ADR-7，拒绝而非降级）。"""


def assert_supported() -> None:
    """在最前端做一次硬检查，让"跑错平台"变成一条清晰报错，而不是诡异的行为差异。"""
    if not REQUIRED_PROC.exists():
        raise UnsupportedPlatform(
            "frpsctl 仅支持 Linux：/proc 不可用，无法做进程身份校验。"
        )


def pid_alive(pid: int) -> bool:
    """存活探测。绝不发送真实信号，绝不误杀。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                        # 存在但不属于当前用户 → 存活（§3.4）
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def proc_start_time(pid: int) -> int | None:
    """进程启动时刻（时钟滴答），用于识别 pid 复用 —— 安全停止的前提。

    /proc/<pid>/stat 的 comm 字段可能含空格与右括号，必须从最后一个 ')' 处切分。
    全局第 22 个字段是 starttime，切分后位于索引 19。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    rparen = raw.rfind(b")")
    if rparen < 0:
        return None
    fields = raw[rparen + 2:].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def proc_cmdline(pid: int) -> list[str]:
    """读 /proc/<pid>/cmdline（NUL 分隔）。比 ps 可靠，且无需 fork。"""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def terminate(pid: int, *, force: bool = False) -> None:
    """发送停止信号。注意 frps 没有信号处理器，SIGTERM 即进程终止（§3.5）。"""
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
```

> `proc_cmdline` 是 §8.3 三重校验的第三个维度，用 `/proc` 直读而非 `subprocess(ps)`——省一次 fork，也不受 `ps` 是否安装影响。

### 8.2 `core/lock.py` —— 实例级互斥

```python
import contextlib
import fcntl
import os
import time
from pathlib import Path


class LockBusy(RuntimeError):
    pass


@contextlib.contextmanager
def instance_lock(path: Path, timeout: float = 5.0):
    """串行化实例级变更操作：start / stop / 配置写入 互斥。

    ⚠️ 实现比这里的示意版**多一层进程内可重入**（按 (路径, 线程) 计数）：
    配置变更事务自己持锁，内部又会调用 `restart()`，而 restart 也要取同一把锁。
    `flock` 按 fd 计，同进程另开 fd 再锁同一文件会**阻塞自己**，表现为
    "变更后启动失败"这种完全不指向真因的错误。详见 §17.3 第 1 条。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy(f"另一个 frpsctl 进程正持有实例锁：{path}")
                time.sleep(0.1)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
```

### 8.3 `core/lifecycle.py` —— 所有权与状态机

```python
class Owner(enum.Enum):
    NONE = "none"
    DIRECT = "direct"
    SYSTEMD = "systemd"


@dataclass(frozen=True)
class ProcessRef:
    pid: int
    start_time: int | None
    binary: str
    config: str

    def is_ours(self) -> bool:
        """三重校验：pid 存活 + 启动时刻一致 + 命令行匹配。"""
        if not pid_alive(self.pid):
            return False
        if self.start_time is not None:
            current = proc_start_time(self.pid)
            if current is not None and current != self.start_time:
                return False          # pid 被复用 → 不是我们的进程
        return _cmdline_contains(self.pid, self.binary)
```

**状态判定表**（`status` 的唯一真相来源）：

| 状态 | 判定条件 | CLI 行为 |
|------|---------|---------|
| `SYSTEMD_ACTIVE` | unit 存在且 `systemctl is-active` | 显示 unit 名与 `ExecMainPID` |
| `RUNNING` | `state.json` 存在 + `is_ours()` | 正常 |
| `FOREIGN` | `state.json` 存在 + pid 存活 + 身份不符 | **拒绝** stop/start，退出码 11 |
| `STALE` | `state.json` 存在 + pid 不存在 | 提示可直接 `start`（自动清理） |
| `STOPPED` | 无 state、无 unit | 退出码 5 |

**`start()` 完整流程**：

```python
def start(self, *, health_timeout: float = 10.0) -> StartReport:
    with instance_lock(self.inst.lock):
        owner = self.resolve_owner()

        if owner is Owner.SYSTEMD:
            return self.systemd.start()               # 委托，不碰 pid 文件

        ref = self.read_state()
        if ref and ref.is_ours():
            raise AlreadyRunning(ref.pid)             # 退出码 6
        if ref and pid_alive(ref.pid):
            raise OwnershipConflict(ref.pid)          # 退出码 11：存活但不是我们的，绝不 kill
        self.inst.state.unlink(missing_ok=True)       # 陈旧状态，安全清理

        if self.systemd.same_config_is_active():
            raise OwnershipConflict("同配置正被 systemd 托管，请用 service 子命令")

        self.verify()                                 # pydantic + frps verify，失败退出码 3

        proc = self._spawn()                          # start_new_session，脱离 SSH 会话
        if not self._await_alive(proc.pid, 1.5):
            # 启动即退出：端口被占、证书缺失、配置语义错误都会走这里
            raise StartupFailed(self._startup_tail())  # 退出码 10，附 frp 原始报错
        self._write_state(proc.pid)                   # pid + start_time + binary + config

        health = self._await_health(health_timeout)   # L1 ∧ L2；port=0 时 L2 记 SKIPPED（§3.7）
        return StartReport(pid=proc.pid, health=health, healthy=health.gate)
```

**派生进程**（fd 生命周期必须正确）：

```python
def _spawn(self) -> subprocess.Popen:
    handle = open(self.inst.new_startup_log(), "ab", buffering=0)
    try:
        return subprocess.Popen(
            [str(self.binary), "-c", str(self.inst.config)],
            cwd=self.inst.root,                   # 让配置里的相对路径可预期
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,               # 脱离 SSH 会话，终端关闭不被 SIGHUP 带走
        )
    finally:
        handle.close()                            # 子进程已持有独立 fd，父进程必须关
```

**`stop()` 完整流程**：

```python
def stop(self, *, timeout: float = 10.0, force: bool = False) -> StopReport:
    with instance_lock(self.inst.lock):
        owner = self.resolve_owner()
        if owner is Owner.SYSTEMD:
            return self.systemd.stop()

        ref = self.read_state()
        if ref is None or not ref.is_ours():
            self.inst.state.unlink(missing_ok=True)
            return StopReport(stopped=False, reason="not-running")   # 退出码 5

        if force:
            terminate(ref.pid, force=True)
        else:
            with contextlib.suppress(ProcessLookupError):
                terminate(ref.pid)                 # SIGTERM：进程立即终止（§3.5，非 graceful）
            if not self._wait_gone(ref.pid, timeout):
                terminate(ref.pid, force=True)     # 兜底：应对卡死或无响应
                self._wait_gone(ref.pid, 5.0)

        self.inst.state.unlink(missing_ok=True)
        return StopReport(stopped=True)
```

### 8.4 `core/config.py` —— 无损配置补丁

```python
_MISSING = object()


def _ensure_table(doc, *path):
    """tomlkit 对不存在的键赋值会抛 NonExistentKey，这里逐级补建表。

    is_super_table=(不是最后一级) 用于生成隐式父表，避免产出多余的空表头。
    """
    node = doc
    for i, key in enumerate(path):
        current = node.get(key)
        if current is None:
            current = tomlkit.table(is_super_table=(i < len(path) - 1))
            node[key] = current
        node = current
    return node


def atomic_write(path: Path, text: str, *, mode: int = 0o600) -> None:
    """临时文件 + fsync + os.replace：磁盘上的目标文件任何时刻都是完整的。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)                        # 先定权限再写内容，避免权限窗口（Linux 必有）
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)                       # 保证目录项也落盘
        finally:
            os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def verify_flags(binary_version: tuple[int, int, int]) -> list[str]:
    """按 §3.6 构造标志，必须放在子命令之前（Cobra 持久标志）。

    `>= 0.70.0` 上 `--strict_config` 默认已是 true，这里仍**显式传**：
    默认值在 v0.53→v0.66 之间变过一次，依赖默认值是脆弱的，显式传值只花一个字节。
    `--allow-unsafe` 仅在配置使用 auth.tokenSource 的 exec 源时追加。
    """
    flags: list[str] = ["--strict_config=true"]        # 显式开启，不依赖版本默认值
    if _uses_exec_token_source():
        flags += ["--allow-unsafe", "TokenSourceExec"]  # StringSlice，不是布尔开关
    return flags


def _verify_argv(self, config_file: Path) -> list[str]:
    """verify 与 start 共用——避免只有一条路径被修好。"""
    return [str(self.binary), *verify_flags(self.binary_version()),
            "verify", "-c", str(config_file)]


class ConfigService:
    def load(self) -> tomlkit.TOMLDocument:
        return tomlkit.parse(self.path.read_text("utf-8"))

    def get(self, dotted: str):
        node = self.load()
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ConfigKeyMissing(dotted)
            node = node[part]
        return node

    def plan_set(self, dotted: str, raw: str) -> ChangePlan:
        """只做内存补丁，不落盘。"""
        doc = self.load()
        parts = dotted.split(".")
        value = self.schema.coerce(dotted, raw)     # "8000"→8000；非法值在此抛错（退出码 3）
        _reject_template_syntax(value)              # frp 会做 text/template 渲染（§3.1 推论 2）
        table = _ensure_table(doc, *parts[:-1])
        before = table.get(parts[-1], _MISSING)
        table[parts[-1]] = value
        return ChangePlan(dotted=dotted, before=before, after=value,
                          text=tomlkit.dumps(doc), diff=unified_diff(self.path, doc))

    def validate_text(self, text: str) -> None:
        """双保险：pydantic 语义校验 + 官方 frps verify 真机校验。"""
        self.schema.validate_document(tomllib.loads(text))
        with tempfile.NamedTemporaryFile(
            "w", suffix=".toml", dir=self.path.parent, delete=False, encoding="utf-8"
        ) as handle:
            handle.write(text)
            candidate = Path(handle.name)
        try:
            proc = subprocess.run(
                self._verify_argv(candidate),
                cwd=self.path.parent, capture_output=True, text=True,
            )
        finally:
            candidate.unlink(missing_ok=True)
        if proc.returncode != 0:
            raise ConfigRejected(proc.stdout.strip() or proc.stderr.strip())
```

**关键性质**：`plan_set` 只改目标键，其余字节原样保留。因此注释、键序、行内注释、空行在 `config set` 之后完全不变。

**版本前置条件**：`install` 阶段已拒绝 `< 0.70.0`（§3.6），因此 `verify_flags()` 不需要任何版本分支——`--strict_config` 与 `--allow-unsafe` 在目标区间内必然可用。这条前置条件必须在 `install` 与 `--binary` 两条入口上**都**做检查：用户用 `--binary` 指向自编译版本时同样要过版本门槛，否则 §3.6 的保证会被绕过。

### 8.5 `core/admin.py` —— Admin API 客户端

```python
class AdminClient:
    """只走 v2 的 dashboard 客户端（ADR-3，版本门槛保证 ≥ 0.70.0）。

    v2 信封：{"code":200,"msg":"success","data":{...}} —— 所有业务字段在 data 下（ADR-3）
    """

    def __init__(self, base_url: str, user: str = "", password: str = "",
                 timeout: float = 3.0) -> None:
        auth = (user, password) if (user or password) else None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout), auth=auth,
        )

    def healthz(self) -> tuple[bool, float]:
        """/healthz 免认证，是最可靠的控制面探针。返回 (是否健康, 耗时毫秒)。"""
        started = time.monotonic()
        try:
            ok = self._client.get("/healthz", timeout=1.5).status_code == 200
        except httpx.HTTPError:
            ok = False
        return ok, (time.monotonic() - started) * 1000

    def server_info(self) -> ServerInfo:
        """只有 v2 一条路径。404 不降级，而是报错并附实际版本（ADR-3）。"""
        resp = self._client.get("/api/v2/system/info")
        if resp.status_code == 404:
            raise ApiVersionMismatch(                 # 退出码 7
                f"该 frps 不提供 /api/v2/system/info（实测版本 {self.reported_version()}），"
                f"最低支持 0.70.0（§3.6）"
            )
        resp.raise_for_status()
        data = resp.json()["data"]
        status = data["status"]
        return ServerInfo(
            version=data["version"],
            client_counts=status["clientCounts"],
            proxy_type_counts=status["proxyCount"],       # map，不是总数
            cur_conns=status["curConns"],
            total_traffic_in=status["totalTrafficIn"],
            total_traffic_out=status["totalTrafficOut"],
            tls_force=data["config"]["tlsForce"],
        )

    def proxies(self, ptype: str) -> list[ProxyStat]:
        resp = self._client.get(f"/api/proxy/{ptype}")
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return [ProxyStat(**item) for item in resp.json().get("proxies", [])]
```

**三层健康判定**（§3.7）：`AdminClient` 只提供 L2 的原始探针，L1 属于 `lifecycle`，L3 属于 `plugin`（TCP 探测，与 HTTP 客户端无关，因此独立成型）：

```python
class Layer(enum.Enum):
    OK = "ok"
    FAIL = "fail"
    SKIPPED = "skipped"      # 未配置该层（如 webServer.port = 0 / 无 httpPlugins）
    UNKNOWN = "unknown"      # 未运行 / 拿不到判断依据


@dataclass(frozen=True)
class HealthReport:
    """三层健康报告。回滚判据只看 L1 与 L2（§3.7）。"""
    l1_process: Layer
    l2_control: Layer
    l3_plugin: Layer
    detail: str = ""
    ms: float = 0.0

    @property
    def gate(self) -> bool:
        """回滚/启动成功判据：L1 ∧ L2，SKIPPED 视为通过。"""
        return self.l1_process is not Layer.FAIL and self.l2_control is not Layer.FAIL


def probe_plugins(addrs: list[str], *, timeout: float = 1.0) -> Layer:
    """L3：只做 TCP connect，不发 HTTP 请求（§3.7 说明原因）。

    addr 形如 http://127.0.0.1:8080，取 hostname/port，缺省端口按 scheme 补。
    """
    if not addrs:
        return Layer.SKIPPED
    for addr in addrs:
        parsed = urllib.parse.urlparse(addr)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((parsed.hostname, port), timeout=timeout):
                continue
        except OSError:
            return Layer.FAIL
    return Layer.OK
```

凭据解析优先级：`--admin-password` / `FRPSCTL_ADMIN_PASSWORD` > 配置文件里的 `webServer.user` / `webServer.password`。

**硬约束**：口令与 token 绝不出现在日志、`--json` 输出、异常消息中。`config get webServer.password` 默认返回 `***`，需 `--reveal` 显式索取。

### 8.6 `core/release.py` —— 二进制获取与校验

```python
CHECKSUM_ASSET = "frp_sha256_checksums.txt"   # 官方校验和资产名


def install(version: str, dest: Path, mirrors: list[str], *, insecure: bool = False) -> Path:
    asset = f"frp_{version}_{_os_tag()}_{_arch_tag()}.tar.gz"
    expected = _expected_sha(version, mirrors)      # 解析 CHECKSUM_ASSET
    if expected is None and not insecure:
        raise ChecksumUnavailable(                  # fail-closed：拿不到就不装
            "拿不到官方校验和，拒绝安装（可用 --insecure 显式跳过，风险自负）"
        )
    blob = _download_with_mirror_fallback(asset, version, mirrors)
    actual = hashlib.sha256(blob).hexdigest()
    if expected and actual != expected:
        raise ChecksumMismatch(asset, expected, actual)
    # 解包 frps → chmod 0755 → 运行 -v 复验 → 落盘为 frps-<version> → （仅 --switch 时）换软链
```

**信任模型**：镜像只影响可用性，不影响信任。信任锚是**官方校验和文件**——校验不通过绝不落盘（ADR-7）。

#### 8.6.1 升级语义（`install` 的两个阶段）

`install` 拆成**下载**与**切换**两件事，默认合并执行，可分开：

| 阶段 | 行为 | 是否影响运行中的实例 |
|------|------|-------------------|
| **落盘** | 写入 `bin/frps-<version>`（0755），**不碰软链**；同名文件已存在则要求 `--force` | 否 |
| **切换** | 原子替换软链 `bin/frps` → `frps-<version>` | **否**，见下 |

三条必须写进帮助文本与 `doctor` 的语义：

1. **运行中的进程不受换链影响**。Linux 上进程的可执行映像在 `execve` 后已绑定到 inode，换软链（甚至删除旧文件）都不会改变正在运行的进程；它只影响**下一次** `start`。
2. **因此升级不是"立即全局生效"，而是"下次启动生效"**。`install --switch` 后 `status` 必须能同时显示两个版本：`state.json.version`（正在跑的）与 `frps -v`（软链指向的）。两者不一致时明确输出 `binary : frps 0.70.0 (running) → 0.71.0 (on disk, restart to apply)`。
3. **`state.json.binary` 存真实路径而非软链路径**（`active_binary()` 做 `resolve()`）。否则软链一换，`is_ours()` 的命令行比对就会失配，把自家进程判成 `FOREIGN`（退出码 11）——**这是升级功能最容易埋进去的自伤 bug**。

配套约定：

- `install --only-download`：只落盘不换链，用于先在多台机器备好二进制、再统一切换的运维节奏。
- `install --version X` 时若 `X < 0.70.0`，**拒绝**（退出码 4，§3.6），而不是"装了但用不了"。
- 旧版本文件**不自动删除**：`bin/frps-<old>` 是回退的唯一抓手。换回旧版只需 `ln -sfn` 或重跑 `install --version <old>`。
- 所有实例共享 `bin/`，所以换链是**全局动作**。多实例场景下 `doctor` 会列出"哪些实例仍在跑旧版"，让升级可被逐步推进。
- systemd 托管的实例（ADR-1）不受软链影响：unit 里 `ExecStart` 写的是具体路径（§12.2），换链后需 `systemctl restart`。这条差异要在 `install` 输出里显式提醒。

### 8.7 `core/doctor.py` —— 体检项

| 检查 | 严重度 | 说明 |
|------|-------|------|
| 二进制存在 / 可执行 / 版本 | ERROR | `< 0.70.0` **拒绝**（退出码 4，§3.6）；`0.70.x` 提示已知远程 DoS 修复缺失、建议升到 `>= 0.71.0` |
| 运行版本 vs 磁盘版本不一致 | WARN | `state.json.version` ≠ 软链指向版本 → 提示"重启后生效"（§8.6.1）。多实例时列出仍在跑旧版的实例 |
| 配置可解析 + `frps verify` | ERROR | 双保险 |
| 配置文件权限 | ERROR | 含 `auth.token` 却非 `0600`（组或其他可读） |
| dashboard 暴露面 | ERROR | `webServer.addr` 非回环 **且** user/password 全空 = 完全无鉴权 |
| dashboard 弱口令 | WARN | user/password 为空，或等于 `admin` |
| `transport.tls.force` | WARN | false 时提示可接受明文 frpc 连接 |
| `allowPorts` / `maxPortsPerClient` | WARN | 未设置时提示端口可被任意申请 |
| 端口可绑定性 | ERROR | 对 `bindPort` / `kcpBindPort` / `quicBindPort` / `vhostHTTPPort` / `vhostHTTPSPort` / `webServer.port` 做 bind 探测 |
| `< 1024` 端口 | INFO | 提示 systemd 需要 `CAP_NET_BIND_SERVICE` |
| systemd 与 direct 冲突 | ERROR | 两种所有权同时成立 → 歧义 |
| 插件可达性 | WARN | 配置了 `httpPlugins` 时逐 addr 做 **TCP 探测**；失败提示"客户端将无法登录"（§3.7、§11.2）。**仅告警，不影响退出码** |

---

## 9. 配置变更闭环

frps 没有热重载，所以"改配置"和"重启"是同一件事。frpsctl 把它实现为一次**带回滚的事务**（ADR-6）：

```
frpsctl config set bindPort 8000
  │
  ├─ 1. 取实例锁                              阻止并发改写
  ├─ 2. tomlkit 读原文件 → 内存定点补丁         线上文件此刻未被触碰
  ├─ 3. pydantic 校验候选值                   ── 失败 → 退出 3，线上配置零影响
  ├─ 4. 候选文本写临时文件（同目录，0600）
  ├─ 5. frps verify -c <临时文件>             ── 失败 → 删临时文件 → 退出 3
  ├─ 6. 备份当前配置到 config-history/（保留 10 份）
  ├─ 7. os.replace 原子替换                    任何时刻文件都完整
  ├─ 8. 若实例在运行：restart
  │       stop → start → 早退检测(L1) → /healthz(L2)（默认 10s）
  │       └─ 失败 → 恢复最近备份 → 再次 start → 退出 9（明确告知"已回滚"）
  │       注意：回滚判据只有 L1 ∧ L2；L3 插件不可达不触发回滚（§3.7）
  └─ 9. 输出 unified diff + 结果
```

设计要点：

| 步骤 | 为什么必须有 |
|------|-------------|
| 3 | pydantic 提供**更快、更好读**的错误信息（"bindPort 必须是 1..65535 的整数"），但不下最终结论 |
| 5 | **唯一权威判定**。在动线上文件之前就把 `frps verify` 请出来，是整条链路上性价比最高的一步。标志由 `verify_flags()` 按版本构造（§3.6、§8.4） |
| 6 | 回滚要有东西可回滚；快照附 `meta.json`（时间、操作者、diff、结果） |
| 7 | 原子替换保证"断电/被杀也不会留下半截配置" |
| 8 | 针对"verify 通过但真跑起来失败"的场景：端口被占、证书文件不存在、权限不足 |

配套约定：

- `--no-restart`：只做 1–7，输出里**显式提示"变更尚未生效"**（用于停机维护窗口）。
- `config edit` 走完全相同的闭环：先展示 diff 让人确认，再落盘重启。
- `config rollback [N]` 复用同一闭环，而不是简单 `cp` 覆盖。
- 实例当前未运行时不做重启与回滚，但 3/5 两步照做——保证**写进去的一定是合法的**。
- 第 8 步的判据是 `HealthReport.gate`（L1 ∧ L2）。**L3 永不参与回滚**：插件抖动不该被误判成"这份配置有问题"，否则会回滚掉恰好用于修复问题的那份配置。

---

## 10. 安全基线

`init` 生成的默认配置必须直接站在安全侧。每条都对应一个已确认的事实：

| 项 | 默认值 | 理由 |
|----|-------|------|
| `webServer.addr` | `127.0.0.1` | 与 frp 默认一致；dashboard 只有 Basic Auth 一层防护 |
| `webServer.user` / `password` | user 固定 `admin`，password 随机 24 字符 | **两者全空 = 完全不鉴权**（§3.3），不是"要求登录"。user 不随机是刻意的：它只是个标识符，真正起作用的是随机口令；随机化 user 只会让人无法从记忆/文档中复现登录名，而 dashboard 本就只监听回环 |
| `transport.tls.force` | `true` | 拒绝明文 frpc 连接（frp 默认 false） |
| `allowPorts` | 交互引导填写 | 否则任何持有 token 的客户端都能申请任意端口 |
| `maxPortsPerClient` | `20` | frp 默认为 0（不限），单客户端可耗尽端口 |
| `auth.token` | 随机 32 字符 | 无 token 等于无客户端鉴权 |
| 配置文件权限 | `0600` | 内含 token 与 dashboard 口令；`os.replace` 落地，无权限窗口 |
| `auth.method` | `token` | 亦支持 `oidc`；v1 不实现，但 `doctor` 会识别并提示 |
| `auth.tokenSource` | 不使用 | 若用户手工使用，`verify` / `start` 必须自动追加 `--allow-unsafe TokenSourceExec` |

其他硬约束：

1. **拒绝危险组合**：`config set` 与 `doctor` 双重拦截"dashboard 绑非回环 + 口令为空"。
   判据是 `schema.check_dangerous_combination()`，**针对合并后的完整配置**判断——
   单看被改的那一个键永远看不出问题（把 `user` 改成空串本身无害，配上
   `addr = "0.0.0.0"` 才是缺口）。
2. **不回显机密**：口令与 token 不进日志、不进 `--json`、不进异常消息。
3. **拒绝模板语法**：写入含 `{{` 的字符串会拒绝（§3.1 推论 2）。
4. **拒绝来路不明的二进制**：校验和不匹配即拒绝安装（§8.6）。
5. **生成加固的 unit**：专用用户 + `NoNewPrivileges` + `ProtectSystem=strict`（§12.2）。

---

## 11. 服务端插件

frp 的服务端插件机制是官方预留的 HTTP 回调协议：frps 在 `Login` / `NewProxy` 等事件发生时 POST 一段 JSON 给指定 HTTP 服务，由它返回操作决策。用 Python 实现该回调是本工具最有价值的扩展方向。

### 11.1 协议事实

| 项 | 事实 |
|----|------|
| 请求 | `POST <addr><path>?version=0.1.0&op=<Op>` —— **op 在 URL query，不在请求头** |
| 请求头 | `X-Frp-Reqid`（链路追踪）、`Content-Type: application/json` |
| 请求体 | `{"version":"0.1.0","op":"Login","content":{...}}` —— 业务字段**嵌在 `content` 下** |
| op 全集 | `Login` / `NewProxy` / `CloseProxy` / `Ping` / `NewWorkConn` / `NewUserConn` |
| `content.user` 类型 | **Login 时是字符串**；**NewProxy / Ping / NewWorkConn / NewUserConn 时是对象** `{"user","metas","run_id"}` |
| 响应语义 1 | `{"reject":true,"reject_reason":"..."}` → 拒绝该操作 |
| 响应语义 2 | `{"reject":false,"unchange":true}` → 放行，**保持原内容** |
| 响应语义 3 | `unchange:false` + `content` → 放行，并**替换**原内容 |
| 非 200 响应 | 直接判定该操作失败 |

> ⚠️ **最容易踩的坑**：只返回 `{"reject": false}` 而不带 `unchange`，会被解析为 `unchange:false` 且 `content` 是零值指针（`pkg/plugin/server/manager.go:99`：`if !res.Unchange { content = retContent.(*T) }`），**等于把 Login/NewProxy 的内容清空**——`user` 变空串、`proxy_name` 变空。**必须显式返回 `"unchange": true`**。

### 11.2 可用性约束

这两条决定了插件方案的部署形态，必须在设计阶段就承认：

1. **fail-closed**：插件报错 → Login / NewProxy **直接失败**（`manager.go:92-97`）。插件服务是全部客户端登录的**单点**。
2. **frp 侧没有超时**：插件 HTTP 客户端是 `&http.Client{}`，未设置任何 `Timeout`。插件一 hang，登录链路跟着 hang。

因此插件的部署要求是硬性的：

- 必须绑 `127.0.0.1`；
- 必须由 systemd 守护并 `Restart=always`；
- handler 内部自行计时，绝不做慢速外部调用；
- `doctor` 增加插件可达性探测。

### 11.2.1 ⚠️ 协议没有任何认证（实现期发现，比"绑回环"更根本）

上面那条"必须绑回环"在原文里是个**建议**。M5 实现时核对源码后确认，它是
**唯一的安全边界**，必须升级为硬约束：

```go
// pkg/config/v1/common.go:125-131 —— 插件配置的**全部**字段
type HTTPPluginOptions struct {
    Name      string   `json:"name"`
    Addr      string   `json:"addr"`
    Path      string   `json:"path"`
    Ops       []string `json:"ops"`
    TLSVerify bool     `json:"tlsVerify,omitempty"`
}
```

没有 token、没有 secret、没有 mTLS。而 frps 发请求时只带两个头
（`http.go:104-105`）：

```go
req.Header.Set("X-Frp-Reqid", GetReqidFromContext(ctx))
req.Header.Set("Content-Type", "application/json")
```

**推论**：任何能连到插件端口的进程都能伪造 `Login` / `NewProxy` 事件，从而
自封身份、自选端口。因此：

| 要求 | 落地方式 |
|------|---------|
| 插件**必须**绑回环 | `PluginPolicy.validate(bind=...)` 在**构造期**拒绝非回环地址（`UsageError`），不是靠文档提醒 |
| `doctor` 把"回调指向非回环"报为 **ERROR** | 与"dashboard 绑非回环且无口令"同量级（§8.7） |
| 客户端自报身份交叉校验 | `require_client_id` 默认开启：`metadatas.client_id` 必须与 `user` 一致 |

`require_client_id` **挡不住**恶意本地进程（它同样能编造两个字段）。它的价值
是把"配置抄错导致互相冒用身份"这类真实事故变成显式拒绝。真正解决需要
`auth.tokenSource` 的 exec 源（§3.1），那是 frp 侧的能力，不在插件层。

### 11.2.2 实现选型：标准库而非 ASGI

原文 §11.3 的参考实现用 FastAPI，那是**示意代码**。实现采用标准库
`http.server.ThreadingHTTPServer`，理由不是省事：

1. 插件是**单点**且 frp 侧**无超时**——依赖越少，启动越快、异常路径越少；
2. 协议只有"一个 POST、收 JSON、回 JSON"，不需要路由、模板、WebSocket；
3. 零新增依赖：任何有 Python 3.11 的机器可直接运行，不必先装 ASGI 栈。

线程模型是刻意的：一请求一线程。插件被调用时客户端线程正在等服务端回包，
串行处理会让一个慢请求阻塞所有人。

### 11.2.3 审计与"不拖慢登录"的冲突解法

审计必须"每次裁决都留痕"，而 §11.2 要求 handler 内绝不做慢速调用——两者直接
冲突。解法是**把写盘从请求路径上摘掉**：

| 环节 | 做法 |
|------|------|
| `record()` | 只做一次入队，O(1) 且无 I/O。实测单次裁决耗时 0.02–0.03 ms |
| 刷盘 | 守护线程按 `flush_every`(32) / `flush_interval`(2s) 落盘 |
| 磁盘不可用 | 记录退回缓冲区，**绝不让登录链路失败**——服务可用性优先于审计完整性 |
| 缓冲上限 | 超过 `max_buffer` 丢**最旧**的（最近的记录排查时最有用） |
| 格式 | JSONL：追加写、可 `tail`/`grep`/`jq` 直接消费，被 kill 最多丢最后一行 |

`audit.path` 默认为 `./plugin-audit.jsonl`。**这个默认值是刻意的**：审计默认
开启却有一半概率没地方落盘，会让"我开了审计啊"变成空话（记录只在内存里，
进程一退就没了）。要"仅内存"必须显式写 `"path": null`。

### 11.2.4 配额：能做什么，以及**为什么不做并发连接上限**

配额只在 `NewProxy` 上实施。这是核对源码后的结论，不是取舍偏好：

| op | 触发频率 | 能否用于配额 | 原因 |
|----|---------|------------|------|
| `Login` | 每客户端一次 | ❌ | 太早——此时还不知道要建几个代理 |
| `NewProxy` | **低频**（每代理一次） | ✅ | 唯一合适的位置 |
| `NewUserConn` | **每一次用户 TCP 连接**（`server/proxy/proxy.go:273-283`） | ❌ | 见下 |

`NewUserConn` 看似能做"并发连接上限"，但会同时踩三条线：

1. 它落在**每个用户连接的关键路径**上，加一次 HTTP 往返直接抬高所有连接延迟，
   违反 §11.2 的"handler 内绝不做慢速外部调用"；
2. 它的错误只以 **info 级**记录（`manager.go:167-170`），被拒连接不留显式痕迹，
   运维无从发现；
3. `NewUserConnContent` 只有 `{user, proxy_name, proxy_type, remote_addr}`——
   **没有连接 id**，插件无法知道连接何时关闭，"当前并发数"只能靠估算。

因此**本工具不实现并发连接上限**。需要真并发限制应在代理类型层面解决（frpc 侧
连接池与限流），而不是在插件回调里做。这是能力边界，不是遗漏。

**`max_proxies` 的计数来源**（`plugin/quota.py`）：

| 模式 | 触发条件 | 准确性 |
|------|---------|-------|
| `dashboard` | 策略里配了 `admin_url` | **权威**——用 `/api/v2/proxies` 的 `data.total`（`V2UserResp.ProxyCount` 亦提供按用户聚合） |
| `local` | 未配 `admin_url` | 仅本进程观测：重启归零、多实例各算各的 |

退化模式**仍然工作**（不能因为查不到计数就让所有人建不了代理），但审计记录里带
`quota_source=local`，`plugin check` 也会明确提示"配额计数不准确 ⚠"——**降级可以，
但必须让人看出来**。

> 顺带修掉一处 `AdminClient` 的隐患：v2 列表端点统一返回
> `{total, page, pageSize, items}`，只读 `items` 会在超过 `pageSize`（默认 50）
> 时**静默截断**。已新增 `page_total()` 并在文档中标注 `clients()` 是分页结果。

### 11.3 参考实现

```python
# plugin.py —— 多用户鉴权 + 端口白名单
# frps 侧配置：
#   [[httpPlugins]]
#   name = "auth"
#   addr = "http://127.0.0.1:8080"
#   path = "/handler"
#   ops  = ["Login", "NewProxy"]
from fastapi import FastAPI, Query, Request

app = FastAPI()
USERS = {"alice": {"allowed_ports": {6000, 6001}}}

@app.post("/handler")
async def handler(
    request: Request,
    op: str = Query(...),                # ← op 来自 URL query
    version: str = Query("0.1.0"),
):
    body = await request.json()
    content = body.get("content") or {}  # ← 业务字段在 content 下

    if op == "Login":
        user = content.get("user", "")   # Login 时 user 是字符串
        if user and user not in USERS:
            return {"reject": True, "reject_reason": f"unknown user: {user}"}

    elif op == "NewProxy":
        user = (content.get("user") or {}).get("user", "")   # NewProxy 时 user 是对象
        if content.get("proxy_type") == "tcp":
            port = content.get("remote_port", 0)
            allowed = USERS.get(user, {}).get("allowed_ports", set())
            if port not in allowed:
                return {"reject": True, "reject_reason": f"port {port} not allowed"}

    # 必须显式 unchange=True，否则 frps 会用零值覆盖原内容
    return {"reject": False, "unchange": True}
```

基于同一协议可以渐进扩展出：按用户分配可用端口段、域名配额、并发连接上限、完整审计日志。全部逻辑留在 Python 生态内。

---

## 12. 分发与部署

### 12.1 交付形态

| 方案 | 结论 |
|------|------|
| PyInstaller 内嵌 frps | ❌ 不用：约 14 MB 二进制每次运行都要解包到临时目录；还需处理 Apache-2.0 的 LICENSE 随包分发，并被迫给每个 glibc/musl × x86_64/aarch64 组合单独出包 |
| PyInstaller 只打 Python 侧 | ⚠️ 可选，收益有限（依赖全是纯 Python） |
| **pip / pipx + 按需下载 frps** | ✅ **采用** |

```toml
# pyproject.toml（要点）
[project]
name = "frpsctl"
requires-python = ">=3.11"
dependencies = ["typer>=0.12", "pydantic>=2.7", "tomlkit>=0.12", "httpx>=0.27"]

[project.scripts]
frpsctl = "frpsctl.cli:main"
```

### 12.2 systemd unit（`frpsctl service install` 渲染）

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

以 `frps@.service` 模板形式安装，多实例即多 unit，与 §6 的实例模型天然对齐。安装后 `owner` 变为 `systemd`，CLI 的 `start/stop/restart/status` 全部委托 `systemctl`（ADR-1）。

---

## 13. 测试策略

这类工具最容易出错的不是命令逻辑，而是**边界**：进程身份、并发、配置往返、失败回滚。因此测试分四层：

| 层 | 目标 | 手段 |
|----|------|------|
| **单元** | 进程原语、锁、无损补丁、原子写、标志构造 | `pid_alive` 的 `PermissionError` 分支；`proc_start_time` 解析（含 comm 字段含括号的用例）；`_ensure_table` 幂等；`atomic_write` 在写入中途抛异常时验证原文件未被破坏；**`verify_flags()` 的版本矩阵**（见下）；`probe_plugins` 的 URL 解析（无端口 / https 缺省端口 / 多地址有一不可达） |
| **契约** | **防止 frp 升级后字段漂移** | 用真实 frps 二进制在高位端口起服务，断言 §13.1 的**全部强制项**。**这是本文档所有"事实"的自动化守卫** |
| **集成** | 生命周期与回滚 | 临时实例目录：init → start → status → config set（成功）→ config set 非法值（断言线上文件未变 + 退出码 3）→ config set 导致启动失败（断言自动回滚 + 退出码 9）→ stop；**外加升级语义**：install 新版 → 断言运行中进程未受影响且 `state.json.version` 未变 → restart → 断言版本已切换且 `is_ours()` 仍成立（§8.6.1 第 3 条的自伤 bug 守卫） |
| **故障注入** | **异常路径的不变量** | monkeypatch 真实系统调用（`os.replace`/`fsync`/`mkdir`、`subprocess.*`、`httpx`、`platform.terminate`）使其单次失败，断言：契约内异常、无遗留进程、无半截/含机密的文件、锁已释放、机密不外泄。见 §13.2 |
| **手工冒烟** | SSH 断开存活、systemd 委托、非 root 权限 | 脚本化 checklist，CI 不覆盖 |

### 13.1 契约层强制断言（`tests/test_facts.py`）

每条都对应文档里一个会**静默出错**的事实——这类错误不会让测试变红，只会让输出悄悄错掉，因此必须显式断言：

| # | 断言 | 守住的事实 |
|---|------|-----------|
| C1 | `/api/v2/system/info` 的 `data.status.proxyCount` **是 `dict[str, int]` 而非 `int`**；`sum(...)` 等于 `GET /api/proxy/tcp` + `/api/proxy/http` + `/api/proxy/udp` 各列表长度之和 | §3.2 的字段陷阱（**问题 4 的核心**）。用 `isinstance(payload["proxyCount"], dict)` 直接断言，并做一次"求和 == 逐类型列表长度"的交叉验证——**光断言类型不够，还要证明求和口径正确** |
| C2 | `GET /api/serverinfo` 与 `GET /api/clients` 在目标版本上**仍存在但本工具不调用**：`AdminClient` 的源码扫描断言只出现 `/api/v2/` 前缀（防止有人把 v1 降级路径加回来） | ADR-3 的"只走 v2"决策。这是**反向断言**：不是测 frp，而是测我们没有偷偷用回 v1 |
| C3 | `GET /healthz` **不带 Authorization 头**返回 200 | §3.2 免认证；也防止未来被挪进鉴权中间件后 `start` 的健康检查静默失效 |
| C4 | v2 响应信封形状为 `{"code","msg","data"}`，业务字段在 `data` 下 | §8.5 解析路径 |
| C5 | 在**未设置** `webServer.user`/`password` 时，`GET /api/v2/clients` 返回 200（而非 401） | §3.3 "双空 = 完全不鉴权"。**这是安全基线的事实依据**，必须自动化确认 |
| C6 | `frps verify -c` 对非法配置退出码为 1，对合法配置打印 `syntax is ok` 且退出码 0 | §3.1 唯一权威判定的契约 |
| C7 | 真实二进制满足 §3.6：`frps -v` 输出无 `v` 前缀；`frps --help` 含 `--strict_config` 与 `--allow-unsafe`；`/api/v2/system/info` 返回 200。**并断言 0.69.1 及更早版本被 `install` 拒绝** | §3.6 版本门槛的自动化守卫。做法：`frps --help` 抓标志集合 + 起服务探 v2；低版本断言用 `install --version 0.69.1` 的退出码，**不下载二进制时可 `pytest.mark.skip`，但保留为可执行断言** |

C7 是"问题 2 的长期解药"：版本矩阵一旦被 frp 改动，CI 先于用户发现，而不是等某个用户拿着 0.60 报告"启动失败但配置明明合法"。

### 13.2 故障注入层：为什么必须有

M5 后的全量 review（§15.5）发现 4 处高危缺陷，**全部位于异常路径**——正常路径
完全正常，只有刻意让某个系统调用失败才会暴露。前三层测的是正常路径与少数手写
的异常分支，覆盖不到这个盲区。

这一层的做法是 monkeypatch **真实调用点**（而不是造一个假世界）：被测代码原样
运行，只有那一次系统调用被迫失败。这样测到的就是它真实的错误处理路径。

每条场景断言的不是功能，而是**五项不变量**：

| # | 不变量 | 为什么是它 |
|---|--------|-----------|
| 1 | 抛出**契约内**的异常（不是裸 OSError/AttributeError/卡死） | 用户看到的是可据以行动的报错 |
| 2 | **无遗留进程** | 一旦"无人认领"，工具再也管不到它——本工具存在的意义就是管住这个进程 |
| 3 | 无半截文件、**无含机密的残留**（校验用的候选文件、快照、草稿） | 故障路径最容易漏掉清理 |
| 4 | **锁已释放** | 否则后续所有操作都被 LockBusy 挡住 |
| 5 | 异常消息与输出**不含机密** | §10 硬约束 2 在异常路径上同样成立 |

**三条实现纪律**（都是踩过才总结出来的）：

1. **诊断工具本身不能被污染**。`assert_no_orphan_frps` 用 `pgrep` 找残留进程，
   而注入器一开始把 `pgrep` 也拦了 → 故障假报成"测试基建崩了"，掩盖真实结论。
   因此 `pgrep`/`ps`/`tail`/`systemctl`/`curl` 一律豁免。
2. **注入要模拟真实的失败模式**。`proc_start_time` 的真实失败是"读不到返回
   `None`"（它内部已吞掉 OSError），不是抛异常——注入异常会绕过 fail-closed
   那条路径，测到的是另一件事。
3. **断言范围要对**。核心层在故障下抛出裸 `OSError` 是**可以接受的**（退出码映射
   是 CLI 边界的职责），断言应覆盖 `FrpsctlError | OSError` 这个契约范围，而不是
   逼核心层吞掉所有异常。

这一层立刻兑现了价值：写完第一次运行就抓到 `stop()` 在 `PermissionError` 下
**信号没发出去却清掉了 state.json**（留下无人认领的进程），以及 `validate_text`
的 `subprocess.TimeoutExpired` 无人接管（裸异常冒到 CLI）。

CI 矩阵：Linux（Python 3.11 / 3.12 / 3.13），容器内跑全部四层；**不设其他操作系统的 job**——平台范围由 §3.4 决定，CI 与之一致。

**特别建议**：把附录 B 的核对命令打包成 `tests/test_facts.py`，C1–C7 全部落在这个文件里。这样"事实基线"就具备了自动保鲜能力。

---

## 14. 里程碑与工作量

| 阶段 | 范围 | 估算 |
|------|------|------|
| **M0** 骨架与事实冻结 | 包结构、错误与退出码映射、`tests/test_facts.py`、CI | 0.5 天 |
| **M1** 生命周期 | `install` / `start` / `stop` / `restart` / `status` / `log`，含锁、身份校验、启动早退检测 | 2 天 |
| **M2** 配置闭环 | tomlkit 无损补丁、verify 预检、原子写、备份历史、`config` 子命令、自动回滚 | 1.5 天 |
| **M3** 运维面 | `doctor`（含安全 lint）、`service install`、systemd 委托、`kick` | 1 天 |
| **M4** 硬化与文档 | 契约测试、集成测试、SSH 断开冒烟、README 与手册 | 1 天 |
| **合计** | 可上生产的 MVP | **约 6 天** |
| **M5** 插件（独立阶段） | FastAPI 插件服务 + 多用户 / 配额 / 审计 + 进程守护 | 视需求 |

工作量分布说明：命令骨架本身只占约 1 天，**其余成本在进程生命周期边界、配置无损往返、失败回滚与真机验证**——这些正是决定"能不能上生产"的部分。M5 与 MVP 解耦，因为它是独立进程、独立风险面（§11.2）。

---

## 15. 风险登记表

| # | 风险 | 影响 | 缓解 |
|---|------|------|------|
| R1 | frp 升级后 API 字段改名 | `status` 静默出错 | 契约测试作为升级门禁，**§13.1 C1/C2 专门断言 `proxyCount` 是 map 且求和口径正确**；`AdminClient` 对缺字段抛明确异常而非裸 `KeyError` |
| R2 | pid 复用导致误杀 | 杀掉无关进程 | 三重身份校验（pid + start_time + cmdline），不符即拒绝（退出码 11） |
| R3 | systemd 与 direct 所有权混用 | 状态错乱、双实例 | ADR-1 显式化所有权 + `doctor` 冲突检测 |
| R4 | 用户手写配置被改写 | 丢注释、丢安全设置 | ADR-2 无损补丁；首次接管前强制备份 |
| R5 | 重启失败导致服务中断 | 可用性事故 | ADR-6 事务闭环，第 8 步自动回滚 |
| R6 | 插件服务挂掉 | **全部客户端无法登录**（fail-closed，且 frp 侧无超时） | 绑回环 + systemd 守护 + `doctor` 探活；插件独立成 M5，不阻塞 MVP |
| R7 | 非 Linux 内核上运行（或容器未挂载 `/proc`） | 身份校验静默失效 → 误杀无关进程 | `assert_supported()` 硬拒绝启动（§3.4、§8.1）；不提供任何降级路径 |
| R8 | 下载源被投毒 | 执行恶意二进制 | 强制 sha256 校验官方 `frp_sha256_checksums.txt`；拿不到校验和即拒绝安装 |
| R9 | 配置含 `{{` 被 frp 当模板渲染 | 配置语义被意外改写 | `plan_set` 拒绝含模板语法的字符串值 |
| R10 | 运行 `<0.71` 版本 | 存在已知远程 DoS | 版本门槛 + `start` / `doctor` 输出 WARNING（§3.6） |
| R11 | dashboard 暴露公网且无口令 | 状态泄露、代理被任意下线 | `init` 强制随机口令；`config set` 与 `doctor` 双重拦截危险组合 |
| R12 | **无条件传 `--strict_config` / `--allow-unsafe` 到旧版本** | 未知标志 → frps 直接退出，表现为"配置合法却启动失败" | §3.6 标志兼容矩阵 + 单一构造点 `verify_flags()`（§8.4）+ §13.1 C7 断言 |
| R13 | **换软链后 `state.json.binary` 失配** | `is_ours()` 误判为 `FOREIGN` → 拒绝停止自家进程（退出码 11） | `active_binary()` 强制 `resolve()` 存真实路径；集成测试覆盖（§8.6.1、§13） |
| R14 | 插件抖动被误判为配置故障 | 回滚掉恰好用于修复问题的配置 | 回滚判据固定为 L1 ∧ L2，L3 只告警不回滚（§3.7、§9） |

---

## 15.5 全量回归 review（M0–M5 完成后）

M5 落地后做了一次**全量回归 review**：4 路独立审查（资源生命周期、并发竞态、
异常路径、文档-代码交叉核对）＋静态分析（ruff 全规则）＋真实二进制端到端。

结论：**发现 30 余处真实缺陷**，其中 4 处高危。全部已修复并补了回归测试。
这一节记录**值得后人记住的几条**，详细清单不再逐条罗列（代码与测试即证据）。

### 15.5.1 四处高危（都是"照着读代码看不出来"的类型）

| # | 缺陷 | 后果 | 为什么危险 |
|---|------|------|-----------|
| 1 | `ProcessRef.is_ours()` 在 `start_time` 缺失/读失败时**静默跳过**复用校验 | 三重校验退化为"pid 存活 + 命令行匹配"；实测一个无关的 `/bin/sleep` 被判成"我们的"，随后被 stop 杀掉 | 这正是 R2 要防的事故，而且**降级是静默的**——没有任何日志 |
| 2 | `stop()` 等待退出期间只校验一次身份 | pid 被复用后 `process_gone` 一直为 False → 超时 → 对**无关进程**发 SIGKILL | 误杀窗口真实存在；修法是"升 SIGKILL 前重验身份" |
| 3 | `start()` 在 `_spawn()` 之后抛异常，进程无人认领 | frps 继续跑、继续占端口，而 state.json 没写成 → `stop` 报 NotRunning、`status` 显示 STOPPED，工具彻底失去追踪 | 触发面很实在：写 state 遇 ENOSPC、`frps -v` 超时、`webServer.addr` 写成带端口的字符串 |
| 4 | 回滚是"**假回滚**"：磁盘回滚了、服务没有 | `config set` 报退出码 9"已自动回滚"，实际实例留在 DOWN；另一条路径更隐蔽——磁盘是旧配置、跑着的是新配置（frps 无热重载），回滚等于没做 | 用户会据此认为"问题已自动解决"，从而不去人工介入 |

**共同点**：都是**安全/可用性语义在异常路径上悄悄失效**，正常路径完全正常。
这解释了为什么"跑通主流程"的自测永远发现不了它们——必须刻意构造异常。

### 15.5.2 一条反复出现的模式：**降级必须可见**

review 发现好几处"遇到问题就悄悄降低保证"：

| 位置 | 曾经的降级 | 现在的做法 |
|------|-----------|-----------|
| `is_ours()` | 读不到启动时刻 → 跳过复用校验 | **fail-closed**（返回 False） |
| 审计写盘失败 | 记录回灌缓冲，静默 | 保留 + `dropped` 计数 + `describe()` 可见 |
| 配额计数查不到 | 退化为本地计数，静默 | 审计记 `quota_source=local`，`plugin check` 明确告警 |
| `prune_history` | `ignore_errors=True`，快照无限增长 | 失败时打印警告 |
| systemd 托管 | `start/stop` 直接抛 11 | **委托 systemctl**（文档承诺的行为） |

写进设计原则：**降级可以，但必须在输出、审计或退出码里留下痕迹**。一个
"看起来正常工作"的降级，比一个明确的失败更有害。

### 15.5.2b 补第五层测试：故障注入

review 的结论是"异常路径是盲区"，因此补了**故障注入层**（§13.2）：注入真实的
系统调用失败，验证五项不变量。

第一次运行就抓到两个此前所有测试都没发现的问题：

- `stop()` 遇到 `PermissionError` 时，信号没发出去（被 `suppress` 吞了），
  而 state.json 被清掉 → **留下无人认领的进程**。已改为报冲突(11)并保留状态。
- `validate_text` 的 `subprocess.TimeoutExpired` **无人接管** → 裸异常冒到 CLI
  变成"未分类错误(1)"。已改为配置类错误(3)并带可行动提示。

另外抓到两个**我自己在 review 修复过程中引入**的错误（说明回归测试在起作用）：

- SIGKILL 升级逻辑被误缩进进 `except OSError` 块 → **不可达代码**，卡死的 frps
  再也停不掉。集成测试抓住。
- 停止失败复用了 `StartupFailed`，消息是"frps 启动后立即退出"——**与事实相反**。
  已新增 `StopFailed`（消息说"无法停止 pid N"）。

### 15.5.3 测试基础设施本身也有缺陷

契约层的端口分配用"绑定→取号→关闭"连续调用两次，取号与真正绑定之间有竞态
窗口，且内核可能重复分配刚释放的号 → 契约测试**间歇性失败**，而重跑就好。

已改为"同时持有多个 socket 直到全部取号完毕再一起关闭"，并抽成
`conftest.free_ports(n)`。教训：**flaky 测试比没有测试更糟**——它会训练人忽略红灯。

### 15.5.4 文档与实现的 drift（已按"以文档为准"修正）

交叉核对查出 11 处行为级不一致，其中**代码确实没做到文档承诺**的有 6 处：

| 文档承诺 | 修正 |
|---------|------|
| ADR-1/§12.2 systemd 所有权下全部委托 systemctl | 已实现委托（此前直接抛 11，`systemd.py` 的三个方法零调用点） |
| §8.3 SYSTEMD_ACTIVE 显示 unit 名与 ExecMainPID | 已显示 |
| §10 硬约束 1 要求 `config set` 与 `doctor` 双重拦截 | 已补 `config set` 一侧 |
| §8.5 凭据优先级 `--admin-password` > 环境变量 > 配置 | 已实现该选项与环境变量 |
| ADR-3 启动后拉 `server_info`，404 → 退出 7 并附实测版本 | 已实现（并区分 404 与瞬时连接失败） |
| §7.3 退出码 7 = dashboard 不可达/未启用 | `kick` 在未启用时改为 7（原为 3） |

**反向修正 3 处**（文档写错了，以代码为准）：`restart --no-rollback` 收回
（重启不读配置，不存在回滚语义）；`webServer.user` 明确为固定 `admin`（见 §10）；
§8.2 `instance_lock` 补记"实现为可重入"。

### 15.5.5 一处刻意保留的不一致

`config get` 的打码形式是"保留首尾各两字符"（`SU***56`）而非全 `***`：既防止
泄露，又让人能核对"改的是不是同一个值"。`config get <表>` 与所有 diff 输出
（`config set` / `diff` / `rollback` / `edit`）都走同一套递归打码。

---

## 16. 设计评审变更记录（问题 1–5 闭环）

本章记录一次设计评审后的修订，**目的是让"为什么这么改"可追溯**，避免后续实现时把结论当成凭空规定。

### 16.1 平台范围：改为 Linux-only（问题 1）

| 项 | 修订前 | 修订后 |
|----|-------|-------|
| 平台定位 | Linux 一级 + macOS 二级 + Windows 尽力而为 | **仅 Linux**，非 Linux 直接拒绝启动 |
| 进程原语 | `pid_alive` 分平台分支 + `_win_pid_alive`（ctypes/`OpenProcess`）+ `taskkill` 停止 | 单一实现：`os.kill(pid, 0)` + `/proc/<pid>/stat` + POSIX 信号 |
| 派生进程 | `os.name` 分支（`start_new_session` / `DETACHED_PROCESS`） | 固定 `start_new_session=True` |
| 原子写 | `if hasattr(os, "fchmod")` 的兼容判断 | 直接 `os.fchmod`（Linux 必有） |
| 新增 | — | `assert_supported()` + `UnsupportedPlatform`：`/proc` 不可用即拒绝 |

**理由**：Windows 分支与"不承诺生产可用"自相矛盾——写了一套永远不会被验证的代码，却要为它的正确性背书。删掉后 §3.4 从"三个平台的矩阵"变成"一组必须满足的内核依赖"，可测试、可断言。同时新增 `proc_cmdline()`（读 `/proc/<pid>/cmdline`），让 §8.3 里 `_cmdline_contains` 这个引用有了确定的实现。

### 16.2 版本门槛重写（问题 2）

**修订前的错误**：文档写"`0.52 – 0.70` 允许 + WARNING"，但这区间里藏着两个硬失败点。逐版本源码对比后的真值：

| 版本 | `--strict_config` | 其默认值 | `--allow-unsafe` | v2 API | 结论 |
|------|------------------|---------|-----------------|--------|------|
| `< 0.52` | 不存在 | — | 无 | 无 | 拒绝（配置体系换代） |
| `0.52.x` | **不存在** | — | 无 | 无 | **拒绝**（无键名护栏） |
| `0.53.0 – 0.65.x` | 有 | **false** | 无 | 无 | 允许 + WARNING |
| `0.66.0 – 0.69.x` | 有 | true | 有 | 无 | 允许 + WARNING |
| `>= 0.70.0` | 有 | true | 有 | **有** | 完全支持 |

**三个直接后果**（均已写入设计）：

1. `--strict_config=true` **必须显式传**——`0.53–0.65` 默认 false，不传等于护栏消失；
2. 标志构造集中到 `verify_flags()`，`verify` 与 `start` 共用（"两处调用、一处判定"）；
3. `auth.tokenSource` 的 exec 源在 `< 0.66.0` 上**拒绝**（退出码 3），而不是硬传一个未知标志。

#### 16.2.1 追加决策：门槛上收到 0.70.0，删除 v1 降级（评审确认）

上表查清后，评审拍板把门槛从"`0.53+` 允许"进一步上收到 **`>= 0.70.0` 才支持，其余一律拒绝**。这个决定让设计发生了一次**净减法**：

| 被删掉的东西 | 原因 |
|-------------|------|
| `AdminClient` 的 `_v2` 能力探测缓存 | 目标是 `>= 0.70.0`，探测永远返回 200，是死代码 |
| `/api/serverinfo` 解析路径 + `ServerInfo.source` 字段 | v1 裸模型不再需要支持 |
| ADR-3 的"404 → 记 v1"语义 | 改为"404 → 报错退出（退出码 7）并附实测版本"——**不降级，不猜测**（ADR-7） |
| §13.1 原 C2（v1 字段同陷阱断言） | 改为**反向断言**：源码扫描确保 `admin.py` 只出现 `/api/v2/` 前缀 |
| `0.53–0.65` / `0.66–0.69` 两个"允许 + WARNING"区间 | 合并为单一拒绝分支 |

**收益**：`admin.py` 从"两套解析 + 一个状态机"变成"一条路径"；契约断言对象从两套模型减为一套；`doctor` 少了两个版本区间分支。**新增的唯一约束**：`--binary` 入口也必须过版本门槛——否则用户用自编译二进制可绕过 §3.6 的保证（已写入 §8.4）。

**代价**：不能用发行版自带的旧 frps。这个代价被 §8.6 的按需下载直接抵消——`frpsctl install` 会装一个达标版本。

### 16.3 健康语义分层（问题 3）

新增 §3.7，把"健康"从一个布尔值拆成 L1 进程 / L2 控制面 / L3 插件三层，并定死两条边界：

- **回滚判据只有 L1 ∧ L2**——插件抖动不能触发配置回滚（否则会回滚掉恰好用于修复问题的那份配置）；
- **L3 用 TCP connect 而非 HTTP 请求**——不给插件增加真实业务调用（§11.2 要求插件 handler 轻量），也避开"服务正常但语义不符"的 405/422 噪声。

`status` 输出改为三层同行展示；插件失败不影响退出码，但必须显著告警（fail-closed 意味着客户端全线无法登录）。

### 16.4 契约测试显式化（问题 4）

§13.1 新增 C1–C7 七条**强制断言**，其中 C1 专门守 `proxyTypeCount` 陷阱：

- 断言它是 `dict[str, int]`（用 `isinstance` 直接验类型）；
- **再做一次交叉验证**：其求和值 == `/api/proxy/{tcp,http,udp}` 各列表长度之和。

只断言类型不够——**求和口径错误（例如漏掉 udp）同样是静默 bug**，必须用第二条独立路径交叉验证。C2 是反向断言（守住"只用 v2"），C7 把 §3.6 的版本门槛也变成可执行断言。

### 16.5 升级语义补全（问题 5）

新增 §8.6.1，把 `install` 拆为**落盘**与**切换**两阶段，并定死三条语义：

1. 换软链**不影响运行中的进程**（Linux 上可执行映像已绑定 inode），只影响下一次 `start`；
2. 因此 `status` 要能同时显示"正在跑的版本"与"软链指向的版本"，不一致时提示 restart 生效；
3. **`state.json.binary` 必须存 `resolve()` 后的真实路径**——否则换链后 `is_ours()` 的 cmdline 校验失配，自家进程会被判为 `FOREIGN`（退出码 11）。这是升级功能最容易埋进去的自伤 bug，已登记为 R13 并由集成测试覆盖。

配套：`--only-download` 分离下载与切换；旧版本文件保留作为回退抓手；`doctor` 新增"运行版本 vs 磁盘版本"检查项。

### 16.6 本次修订新增的风险条目

R12（旧版本传未知标志）、R13（换链后身份校验失配）、R14（插件抖动误触发回滚）——均已在 §15 登记，且各自都有对应的测试或断言。

### 16.7 本次修订的总体效果：三处净减法

五次修订中有三次是**删代码**，而不是加代码——这是设计收敛的信号：

| 减法 | 删掉了什么 | 换来什么 |
|------|-----------|---------|
| Linux-only（§16.1） | Windows 分支：`_win_pid_alive`、`taskkill`、`DETACHED_PROCESS`、`hasattr(os,"fchmod")` 兼容判断 | §3.4 从"三平台行为差异"变成"五条内核依赖"，可测试、可断言 |
| 门槛 ≥ 0.70.0（§16.2.1） | v1 降级路径：能力探测缓存、`/api/serverinfo` 解析、`source` 字段、两个 WARNING 区间 | `admin.py` 一条路径；契约断言对象减半 |
| 三层健康（§16.3） | "健康 = 一个布尔值"的模糊语义 | 回滚判据显式化，插件故障不再能误伤配置 |

**唯一没有减法的两处**（版本门槛的查证、升级语义的补全）都是**发现并堵住了静默 bug**：前者是"旧版本上传未知标志导致启动失败 / 不传标志导致护栏消失"，后者是"换软链后身份校验失配导致拒绝停止自家进程"。这两个都不是理论风险，而是照原文档实现就必然出现的问题。

---

## 17. 实现验证记录（M0–M5 已落地）

> 本节记录 M5 之前的实现验证；M5 之后的全量回归 review 见 §15.5。

本章记录实现过程中**文档被现实修正**的地方，以及只有真机测试才能照出来的问题。
它的用途是：下次改这块代码的人，不必重新踩一遍。

### 17.1 状态

| 项 | 状态 |
|----|------|
| 代码 | `src/frpsctl/`（core 15 模块 + `plugin/` 6 模块 + CLI 3 模块） |
| 测试 | **164 个用例**；单元 / 集成 / CLI / 契约 / **故障注入** 五层 |
| 真机验证（M0–M4） | frps **0.71.0** 全链路冒烟通过：`init → verify → start → status → config set（自动重启）→ doctor → config rollback → stop` |
| 真机验证（M5） | **真 frpc → 真 frps → 我们的插件**：授权用户建代理成功、未授权用户登录被拒、白名单外端口被拒且理由回到客户端 |
| 契约层 | C1–C7 全绿（真二进制），CI 里作为升级门禁 |

### 17.2 实现期发现并修正的文档错误

实现与真机测试推翻了设计文档的 **3 处**描述，均已回写：

| # | 文档原说法 | 实测真值 | 影响 |
|---|-----------|---------|------|
| 1 | `--strict_config` 是"标志名" | 源码里是 `strict_config`（下划线），`--help` 渲染成 `--strict-config`（连字符）；**两种写法都被 pflag 接受**。另确认 0.71.0 上**不传标志时严格模式也已生效** | 契约断言原写死 `--strict_config in --help`，会因纯展示层差异误报。已改为"断言存在 + 断言两种拼写都可用" |
| 2 | v2 字段名 `proxyCount` | 真名是 **`proxyTypeCount`**；且 **`clientCounts` 是 `int` 而不是 dict** | 实现按文档写了 `proxyCount`，解析永远得到空 dict、客户端数永远是 0——**不报错，只是数字全错** |
| 3 | 附录 A 未提 TOML 位置语义 | 顶层键写在 `[table]` 之后会变成那张表的子键，frps 报 `unknown field "allowPorts"` | `init` 生成的配置把 `allowPorts` 排在 `[log]` 之后 → 生成的配置**开箱即被 frps 拒绝** |

> 第 2 条特别值得记住：**字段名写错的代价是"静默给出错误数字"**，而 `dataclass`
> 的默认值恰好让解析"看起来成功了"。这正是 §13.1 坚持"既断言字段名、又交叉验证
> 求和口径"的原因——只写单元测试永远发现不了它。

### 17.2.1 M5（服务端插件）的三个关键结论

| # | 结论 | 依据 |
|---|------|------|
| 1 | **插件协议没有任何认证**，因此"绑回环"是唯一安全边界，已升级为构造期硬拒绝 + `doctor` ERROR | `HTTPPluginOptions` 只有 5 个字段；请求只带 `X-Frp-Reqid`/`Content-Type` |
| 2 | **放行必须显式带 `unchange: true`**，且这条被做成了类型约束 | `manager.go:99` 的 `content = retContent.(*T)`；Go 零值是 `false` |
| 3 | **审计必须异步**，否则与 §11.2"handler 内不做慢速调用"直接冲突 | 实测裁决耗时 0.02–0.03 ms（写盘已移出请求路径） |

第 2 条的实现方式值得记下来：响应对象**只能**由 `pass_through()` / `reject_op()` /
`replace()` 三个工厂方法构造，从类型上就不给你"忘记写 unchange"的机会——
这个坑一旦踩中，表现是"登录莫名失败"，而排查方向会完全跑偏。

**M5 的真机验证方式**：插件唯一的真实调用方是 frpc，因此从官方 tar 包中取出
frpc（`install --with-frpc`，与 frps 同一个资产、不额外下载），用真 frpc 发起
登录来断言：

| 场景 | 期望 | 实测 |
|------|------|------|
| 授权用户 + 白名单内端口 | 登录成功、建代理成功 | ✅ |
| 未授权用户 | 登录失败（fail-closed 生效） | ✅ |
| 授权用户 + 白名单外端口 | 登录成功但建代理失败，**拒绝理由回到客户端** | ✅（理由含 `6000-6010`） |

最后一条尤其重要：它同时证明了"拒绝理由真的传到了客户端"，而不只是插件自己
以为拒绝了。

### 17.3 实现期发现并修正的代码缺陷

以下 7 个问题都由**测试**（而非代码审查）抓出，说明 §13 那四层测试的划分是有效的：

| # | 缺陷 | 若上线的后果 | 抓住它的测试 |
|---|------|------------|------------|
| 1 | `apply_change` 持实例锁后内部 `restart()` 又取同一把锁 → **自我死锁** | **每一次 `config set` 都失败并回滚**（报"变更后启动失败"，指向完全错误的真因） | 集成层 `test_full_chain` |
| 2 | 早退检测用 `pid_alive`，而僵尸进程的 `kill(pid,0)` 返回成功 | 崩掉的 frps 被判为"启动成功"，服务实际不在 | 集成层 `test_startup_failure_reports_frps_output` |
| 3 | 停止流程用 `not pid_alive` 判"已退出" | 进程其实已死，却空等超时并误报"SIGKILL 后仍未退出" | 集成层 `test_sigterm_terminates_immediately` |
| 4 | `plan_set` 未做语义校验，非法值要等到 `frps verify` 才被拒 | 违反 §9 第 3 步"候选值先过语义校验"，错误信息变差、失败点后移 | 集成层 `test_full_chain` |
| 5 | `prune_history` 只 unlink 配置再 `rmdir`，而目录里还有 `meta.json` | "保留 10 份"从未生效，快照无限增长 | 集成层 `test_history_keeps_only_ten` |
| 6 | 身份校验只比 `argv[0]` | 经解释器/包装脚本启动时 `argv[0]` 是解释器，自家进程被判 `FOREIGN`（退出码 11，拒绝停止） | 集成层夹具（假 frps 走 shebang） |
| 7 | 健康探针在**回环地址**上也读取环境代理变量 | `NO_PROXY` 里写了 `[::1]` 这类不规范值时，httpx 构造客户端即抛 `InvalidURL`，每一次健康检查都变成"dashboard 不可达" | 集成层（宿主环境恰好如此） |

### 17.4 实现层面的设计确认

三条设计决策在实现中被证明"正是关键"：

1. **`verify_flags()` 单一构造点**（§8.4）：`verify` 与 `start` 共用，因此"标志是否正确"只需要在一处验证（§13.1 的 `test_our_flag_construction_is_accepted` 用的是真 frps）。
2. **回滚判据只有 L1 ∧ L2**（§3.7）：集成测试 `test_plugin_failure_does_not_break_gate` 直接断言"插件不可达时 `gate` 仍为真"，把这条边界钉成了可执行事实。
3. **`state.json` 存 `resolve()` 后的真实路径**（§8.6.1）：升级语义测试在换软链后仍能通过三重校验——R13 那个"自伤 bug"被测试永久守住。

### 17.5 与设计的一处偏离（有意为之）

命令表（§7.2）里 `--json` 写在子命令后面（如 `doctor [--json]`），而实现把 `--json`
做成了**全局选项 + 每个子命令的选项**，两种位置都可用。原因：只提供全局选项时
`frpsctl status --json` 会报 `No such option`，与文档写法及用户直觉都不符。
行为对脚本完全兼容（`--json` 在前后都能用），因此不视为破坏性偏离。

---

## 附录 A：frps 配置键速查表

> 全部取自 `v0.71.0` 源码；「默认值」栏是 `Complete()` 之后的**生效值**。合法取值来自校验器。
> 表中打 ★ 的项是 `init` 会主动设置的安全相关键。

### 顶层

> ⚠️ **TOML 位置纪律（实现期踩到的坑）**：TOML 里**任何写在 `[table]` 之后的键都属于那张表**。
> 若把 `allowPorts`、`maxPortsPerClient` 这类顶层键误写在 `[log]` 之后，它就变成
> `log.allowPorts`，而 frps 的报错是极具误导性的
> `json: unknown field "allowPorts"`——看起来像键名写错，实际是**位置错了**。
> 因此 `init` 生成配置时必须把所有顶层键排在任何表头之前（已由
> `tests/test_cli.py::TestInitConfig` 守住）。

| 键 | 类型 | 默认值 | 说明 |
|----|------|-------|------|
| `bindAddr` | string | `0.0.0.0` | 控制连接监听地址 |
| `bindPort` | int | `7000` | 控制端口 |
| `kcpBindPort` | int | `0`（禁用） | KCP 端口 |
| `quicBindPort` | int | `0`（禁用） | QUIC 端口 |
| `proxyBindAddr` | string | 同 `bindAddr` | 代理实际监听地址 |
| `vhostHTTPPort` | int | `0`（禁用） | HTTP 虚拟主机端口 |
| `vhostHTTPTimeout` | int64 | `60` | vhost HTTP 响应头超时（秒） |
| `vhostHTTPSPort` | int | `0`（禁用） | HTTPS 虚拟主机端口 |
| `tcpmuxHTTPConnectPort` | int | `0`（禁用） | TCPMux HTTP CONNECT 端口 |
| `tcpmuxPassthrough` | bool | `false` | 透传模式，frps 不修改流量 |
| `subDomainHost` | string | `""` | 子域名后缀 |
| `custom404Page` | string | `""` | 自定义 404 页面路径 |
| `enablePrometheus` | bool | `false` | 在 dashboard 上暴露 `/metrics` |
| ★ `maxPortsPerClient` | int64 | `0`（不限） | 单客户端可申请的端口数上限 |
| `userConnTimeout` | int64 | `10` | 等待工作连接的超时（秒） |
| `udpPacketSize` | int64 | `1500` | UDP 包大小 |
| `natholeAnalysisDataReserveHours` | int64 | `168` | NAT 打洞分析数据保留时长 |
| `detailedErrorsToClient` | bool | `true` | 是否把详细错误回给 frpc（建议生产关掉） |
| ★ `allowPorts` | 数组 | 不限 | 元素形如 `{start=…,end=…}` 或 `{single=…}` |

### `auth`

| 键 | 合法值 | 默认值 | 说明 |
|----|-------|-------|------|
| `auth.method` | `token` / `oidc` | `token` | 鉴权方式 |
| ★ `auth.token` | string | `""` | 为空表示不校验客户端 token |
| `auth.additionalScopes` | `HeartBeats` / `NewWorkConns` | `[]` | **合法值只有这两个** |
| `auth.tokenSource` | `{type,exec.command,…}` | — | 使用 exec 源时**必须**加 `--allow-unsafe TokenSourceExec` |
| `auth.oidc.*` | — | — | 仅 `method = "oidc"` 时生效 |

### `webServer`

| 键 | 类型 | 默认值 | 说明 |
|----|------|-------|------|
| ★ `webServer.addr` | string | `127.0.0.1` | 绑定地址；改成 `0.0.0.0` 前必须确认口令已设 |
| `webServer.port` | int | `0`（**不启动 dashboard**） | >0 才启用 dashboard 与 Admin API |
| ★ `webServer.user` | string | `""` | 与 password **同时为空 = 完全不鉴权** |
| ★ `webServer.password` | string | `""` | 同上 |
| `webServer.assetsDir` | string | `""` | 为空时用内嵌 dashboard 资源 |
| `webServer.pprofEnable` | bool | `false` | 开放 pprof |
| `webServer.tls.*` | `{certFile,keyFile}` | — | 启用时必须两项都给 |

### `log`

| 键 | 合法值 | 默认值 | 说明 |
|----|-------|-------|------|
| `log.to` | `console` / 文件路径 | `console` | 非 `console` 时按天轮转 |
| `log.level` | `trace`/`debug`/`info`/`warn`/`error` | `info` | |
| `log.maxDays` | int64 | `3` | 日志保留天数 |
| `log.disablePrintColor` | bool | `false` | |

### `transport`

| 键 | 默认值 | 说明 |
|----|-------|------|
| `transport.tcpMux` | `true` | TCP 流多路复用 |
| `transport.tcpMuxKeepaliveInterval` | `30` | |
| `transport.tcpKeepalive` | `7200` | 负值禁用 |
| `transport.maxPoolCount` | `5` | **负值非法**（历史版本曾因此 panic） |
| `transport.heartbeatTimeout` | tcpMux 开 → `-1`（禁用）；关 → `90` | 不建议修改 |
| `transport.quic.keepalivePeriod` | `10` | |
| `transport.quic.maxIdleTimeout` | `30` | |
| `transport.quic.maxIncomingStreams` | `100000` | |
| ★ `transport.tls.force` | `false` | 只接受 TLS 加密连接；**设了 `trustedCaFile` 会被自动置为 true** |
| `transport.tls.certFile` / `keyFile` / `trustedCaFile` | — | 双向认证需给全 |

### `httpPlugins`（数组）

| 键 | 说明 |
|----|------|
| `name` | 插件名 |
| `addr` | 形如 `http://127.0.0.1:8080` |
| `path` | 形如 `/handler` |
| `ops` | 合法值：`Login` / `NewProxy` / `CloseProxy` / `Ping` / `NewWorkConn` / `NewUserConn` |
| `tlsVerify` | HTTPS 时是否校验证书 |

---

## 附录 B：事实复核清单

本文档的每条事实都可以用下面几条命令复现。建议转成 CI 断言（§13 契约层），让事实基线具备自动保鲜能力。

```bash
# 1) 拉取基线源码
git clone --depth 1 --branch v0.71.0 https://github.com/fatedier/frp /tmp/frp

# 2) proxyTypeCount 是 map 而非整数
grep -n 'ProxyTypeCounts' /tmp/frp/server/http/model/types.go

# 3) /healthz 注册在鉴权中间件之外
sed -n '25,50p' /tmp/frp/server/api_router.go

# 4) user 与 password 双空 = 完全不鉴权
sed -n '45,60p' /tmp/frp/pkg/util/net/http.go

# 5) frps 没有任何信号处理器（只有 frpc 有）
grep -rn 'signal.Notify' /tmp/frp --include=*.go

# 6) 插件协议：op 在 query，请求头是 X-Frp-Reqid
sed -n '80,110p' /tmp/frp/pkg/plugin/server/http.go
sed -n '1,60p' /tmp/frp/doc/server_plugin.md

# 7) unchange 语义（只返回 {"reject":false} 会清空 content）
sed -n '86,100p' /tmp/frp/pkg/plugin/server/manager.go

# 8) 官方校验和资产名（第一条非 200，第二条 200）
curl -sIL -o /dev/null -w '%{http_code}\n' \
  https://github.com/fatedier/frp/releases/download/v0.71.0/frp_0.71.0_checksums.txt
curl -sIL -o /dev/null -w '%{http_code}\n' \
  https://github.com/fatedier/frp/releases/download/v0.71.0/frp_sha256_checksums.txt

# 9) 版本输出格式与默认严格模式
sed -n '1,40p' /tmp/frp/cmd/frps/root.go

# 10) 配置默认值（Complete 系列）
grep -n 'func (c \*ServerConfig) Complete' -A 28 /tmp/frp/pkg/config/v1/server.go
grep -n 'func (c \*LogConfig) Complete' -A 5 /tmp/frp/pkg/config/v1/common.go
```

### 附录 B-2：§3.6 版本矩阵的复核命令

§3.6 那张表是逐版本对比源码得出的，不是推断。复现方式（需要完整 tag 历史，故不用 `--depth 1`）：

```bash
# 11) 版本矩阵：标志引入点与默认值变迁
git clone https://github.com/fatedier/frp /tmp/frp-all && cd /tmp/frp-all

# 11a) --strict_config 首次出现（v0.52.3 无，v0.53.0 有）
for t in v0.52.0 v0.52.3 v0.53.0; do
  echo -n "$t strict_config="; git show "$t:cmd/frps/root.go" | grep -c strict_config
done

# 11b) --strict_config 默认值由 false 翻为 true（v0.65.1 false，v0.66.0 true）
for t in v0.53.0 v0.65.1 v0.66.0 v0.71.0; do
  echo -n "$t "; git show "$t:cmd/frps/root.go" | grep 'BoolVarP.*strict_config' | head -1
done

# 11c) --allow-unsafe 首次出现（v0.65.1 无，v0.66.0 有）
for t in v0.65.0 v0.65.1 v0.66.0; do
  echo -n "$t allow_unsafe="; git show "$t:cmd/frps/root.go" | grep -c 'allow-unsafe'
done

# 11d) v2 Admin API 首次出现（v0.69.1 无，v0.70.0 有）
for t in v0.69.0 v0.69.1 v0.70.0; do
  echo -n "$t v2.go="; git ls-tree -r --name-only "$t" | grep -c 'server/http/model/v2.go'
done

# 11e) 配置体系换代点：v0.51.3 是 INI 解析器，v0.52.0 才有 load.go + frps.toml
git ls-tree -r --name-only v0.51.3 | grep 'pkg/config/'      # 只有 parse.go/server.go
git ls-tree -r --name-only v0.52.0 | grep -E 'pkg/config/load.go|conf/frps.toml'
```

三处**必须注意的复核纪律**：

1. `-v` / `--version` 是**持久标志**（`PersistentFlags`），`verify` 子命令同样认识它们——所以 §8.4 的 `verify_flags()` 把标志放在子命令**之前**，两种位置虽然都能工作，但只沿用一种写法以免未来踩 Cobra 的解析顺序坑。
2. `--allow-unsafe` 是 `StringSlice`，值为 `TokenSourceExec`（对应 `security.ServerUnsafeFeatures`），**不是布尔开关**。
3. 版本矩阵会随 frp 发版变化，因此**它是 CI 断言（§13.1 C7），不是一次性结论**。本文档记录的是 v0.71.0 时点的观测值。
