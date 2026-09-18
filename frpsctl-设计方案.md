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
   - [17.6 第二轮全量 review](#176-第二轮全量-reviewm0m5--文档定稿--真-frpc-契约复核后)
18. [Web 管理台](#18-web-管理台)
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
├── 二进制与配置                  install / init / verify
├── 生命周期                      start / stop / restart / status / log
├── 配置子命令                    config get|set|unset|edit|list|diff|rollback
│                                 （set 支持 --dry-run / --stdin / --prompt）
├── 观测与运维                    clients / proxies / traffic / instances / doctor / prune
├── systemd 集成                  service install|uninstall|status|logs
├── 服务端插件                    plugin init|check|serve
│                                 plugin user set|remove|list（策略结构化编辑）
│                                 plugin service install|uninstall|status
└── Web 管理台                    web serve / web service install|uninstall|status
                                  web password show

└── 工具管理                      install / uninstall（完整卸载，§21）

可选：插件服务与 Web 管理台都是独立进程，由各自的 systemd unit 守护。
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
- **Web UI**：~~不做~~——该非目标在 v0.2.2 被推翻：官方 dashboard 只有观测、没有控制面，而"改配置 / 停服务 / 回滚"恰恰是运维最需要的（§18）。此处保留原判断以记录决策变更。
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
| `POST /api/v2/system/prune` | Basic | ✘ 未使用——`prune` 走的是下面那条 v1 遗留端点（v2 没有等价的清离线接口） |
| `DELETE /api/proxies` | Basic | ✔ `prune` 用——**实际语义是"清理离线代理记录"**（`?status=offline`，源码 `controller.go` 的 `ClearOfflineProxies()`；真机实测无参数返回 400）。frp **没有**强制下线在线代理的 API；旧 `kick` 命令基于误读，从未工作（§18.6） |
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
- **鉴权开关的判据是"任一非空"**：只要 `user` 或 `password` 有一个非空，Basic Auth 中间件就生效；而 **Basic Auth 里的空口令是"合法口令"**。实现期在真 frps 0.71.0 上逐项实测（复现命令见附录 B 第 12 条）：

  | `webServer.user` | `webServer.password` | 无 `Authorization` 头 | `user:(空口令)` |
  |---|---|---|---|
  | `"admin"` | 未设/空 | **401** | **200** |
  | 未设 | 未设 | **200** | 200（任意凭据均可） |
  | `"admin"` | `"secret"` | 401 | 401（须 `admin:secret`） |
  | 未设 | `"secret"` | 401 | 401（须 `:secret`） |

  > **推论（本工具据此划边界）**：`check_dangerous_combination()` 只拒绝"**两者全空 + 绑非回环**"（那是真·无鉴权）；而"有 user、口令为空"属于**强度不足**——它毕竟启用了鉴权，属于用户的显式选择，因此由 `doctor` 以 **WARN** 告警（"等于只用用户名保护 dashboard"），不在 `config set` 层否决。
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

`>= 0.70.0` 上 `--strict_config` 默认已是 `true`，但 `config_flags()` **仍然显式传 `--strict_config=true`**。原因是这条护栏值一个字节的成本，而依赖"某个版本的默认值"是脆弱的——默认值在 v0.53 到 v0.66 之间就变过一次。同理 `--allow-unsafe TokenSourceExec` 按配置内容决定是否追加。

**版本解析**：正则提取 `(\d+)\.(\d+)\.(\d+)` 后转三元组，而不是对 `split(".")` 做 `int()`——后者对 `0.71.0-rc1`、`0.71.0+dev` 这类合法后缀不够稳健。解析失败按"不支持"处理（ADR-7）。

**标志构造**集中在 `core/config.py` 的 `config_flags()`（§8.4，**刻意不接收版本号**：门槛上收后版本分支已成死代码），`verify` 与 `start` 共用同一份逻辑——"两处调用、一处判定"，避免未来只修好其中一条路径。

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
| `start` / `restart` 的健康等待 | ✔（早退检测） | ✔（`--health-timeout`，默认 10s） | ✘ | `webServer.port = 0` 时 L2 自动跳过，退化为 L1。**gate 未过 → 退出码 12**，进程保留（`status`/`stop` 可用）——v0.2.0 起生效，此前该路径完全静默 |
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

> ⚠️ 历史注记："允许发行版自带的 frps"在 §16.2.1 门槛上收（只接受 ≥ 0.70.0）后**不再成立**——发行版通常落后于该门槛。

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
│   ├─ __init__.py   全部命令（按 install/init、config、plugin、     │
│   │                web、观测类命令分组；实现即文档 §7.2 的命令表）  │
│   ├─ context.py    全局选项 → AppContext、异常 → 退出码映射        │
│   └─ ui.py         人读/JSON 渲染（--verbose 委托 core.diagnostics）│
│                                                                   │
│  web/            浏览器界面（与 cli/ 平级的又一个前端，§18）       │
│   ├─ server.py     ThreadingHTTPServer：路由/安全头/CSRF           │
│   ├─ api.py        JSON API（只调 core）+ 错误 → HTTP 映射         │
│   ├─ auth.py       会话 / CSRF / 失败限速                          │
│   └─ static/index.html   单文件前端（零外部资源）                  │
│                                                                   │
│  plugin/         服务端插件（§11）：协议解析 / 裁决 / 配额 / 审计   │
│                                                                   │
│  core/                                                            │
│   ├─ instance.py   实例布局与全局选项（--instance / --root）        │
│   ├─ platform.py   pid_alive / proc_start_time / proc_cmdline      │
│   ├─ lock.py       flock 实例级互斥（同线程可重入）                 │
│   ├─ lifecycle.py  所有权探测 + 状态机 + 启动早退检测               │
│   ├─ config.py     tomlkit 无损补丁 + 原子写 + 备份历史             │
│   ├─ transaction.py 变更事务（锁内读-改-写、CAS、自动回滚）         │
│   ├─ admin.py      Admin API 客户端（只走 v2，§ADR-3）             │
│   ├─ release.py    二进制下载 + sha256 强校验                      │
│   ├─ systemd.py    unit 渲染与 systemctl 委托（frps/插件/web）      │
│   ├─ doctor.py     体检与安全 lint                                 │
│   ├─ logs.py       日志路径解析与 tail（CLI 与 web 共用）           │
│   └─ diagnostics.py 进程级诊断开关（--verbose，core 不依赖 cli）    │
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
│   └── frps -> frps-0.71.0    # 当前版本软链，唯一被 install（默认换链行为）改动的对象
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
--admin-password       dashboard 口令（优先于配置文件；也可用 FRPSCTL_ADMIN_PASSWORD）
--yes, -y             跳过交互确认
--verbose, -v         详细输出（诊断只进 stderr）
--version             frpsctl 版本
```

**全局选项可写在子命令之后**（`frpsctl status --json` / `frpsctl verify --verbose`）。
落地方式是 `cli._AnywhereGroup.parse_args` 在**解析之前**把散落在子命令后面的全局
选项整体前移，而不是给每条子命令手工加同名选项——后者补了 19 处仍然漏掉了
`--yes` / `--verbose`（详见 §17.6.6）。短名 `-v` **不参与**前移：它与 `log -n` /
`status --interval` 之类的局部短选项挤在一起，盲目前移会把子命令自己的参数搬到
错误位置。

### 7.2 命令表

| 命令 | 语义 | 关键行为 |
|------|------|---------|
| `frpsctl install [--version V] [--force] [--only-download] [--mirror U（可重复）] [--insecure] [--with-frpc]` | 获取 frps 二进制 | 下载 → **强校验 sha256** → 落盘为 `frps-<version>` → `-v` 复验 → 换软链（§8.6.1）。`< 0.70.0` 直接拒绝 |
| `frpsctl init [--no-input] [--force] [--bind-port P] [--dashboard-port P] [--allow-ports S]` | 交互式生成配置 | 强制随机口令、`tls.force=true`、引导设置 `allowPorts`；已存在则要求 `--force` 并先备份 |
| `frpsctl verify [--file P]` | 双保险校验 | pydantic 语义校验 + `frps verify`；用临时副本，不动线上文件 |
| `frpsctl start [--foreground] [--health-timeout S]` | 启动 | verify → 加锁 → 派生进程 → **早退检测** → 写 state → 健康检查 |
| `frpsctl stop [--force] [--timeout S]` | 停止 | SIGTERM → 轮询确认退出 → 超时 SIGKILL；身份不符则拒绝 |
| `frpsctl restart [--health-timeout S] [--timeout S]` | 重启 | stop → start → 健康检查。**没有 `--no-rollback`**：重启不读配置，也就没有"新旧版本"可比，自动回滚只属于配置变更路径 |
| `frpsctl status [--watch] [--interval S]` | 状态聚合 | `owner / 状态 / pid / 版本 / 运行时长 / listen / dashboard / 健康 / 客户端 / 各类型代理 / 今日流量`；`--watch --json` 为 NDJSON |
| `frpsctl log [-f] [-n N]` | 看日志 | 纯 Python tail（**从文件尾反向读取**，大文件不再全量扫描）；缺失时回退到 startup 日志（轮转后自动重开） |
| `frpsctl config get <key> [--reveal]` | 读单键 | 点分路径；敏感值默认打码（`--reveal` 显式取明文） |
| `frpsctl config set <key> <value> [--no-restart] [--dry-run] [--stdin] [--prompt]` | 写单键 | 走 §9 事务闭环；`--dry-run` 只校验并展示 diff；`--stdin`/`--prompt` 让敏感值不进 argv；`--health-timeout S` |
| `frpsctl config unset <key> [--no-restart] [--dry-run]` | 删键回落默认 | 与 set 同一闭环；键不存在报配置错误(3) |
| `frpsctl config list [--prefix P] [--tree]` | 列出全部键 | 值自动打码；`--tree` 按表分组缩进 |
| `frpsctl config edit [--yes]` | `$EDITOR` 编辑 | 保存后走同一闭环（锁内 CAS：编辑期间被并发修改则拒绝草稿） |
| `frpsctl config diff [--steps N]` | 当前 vs 第 N 新快照 | unified diff（打码） |
| `frpsctl config apply --set K=V --unset K [--dry-run]` | **多键**一次事务 | 一份快照、一次重启（复用 `apply_sets`）；与 Web 配置表单同语义 |
| `frpsctl config rollback [N]` | 回滚到 N 份之前 | 同样走闭环；`N ≥ 1` |
| `frpsctl service install\|uninstall\|status` | frps 的 systemd 集成 | 渲染 `frps@.service` + `daemon-reload` + `enable`（需 root；安装前四项部署体检）；`status` 同时报告 active 与 **enabled**（开机自启） |
| `frpsctl service logs [-f] [-n N]` | journald 集成 | unit 级日志（启动失败 / OOM / 权限拒绝），与 `log` 互补 |
| `frpsctl doctor [--json]` | 体检 | §8.7 检查项（含 Web 口令文件权限），按 severity 输出；`--json` 带 `counts`；有 ERROR 时退出码 1 |
| `frpsctl clients [--json]` | 在线客户端列表 | v2 `/api/v2/clients`，**自动翻页取全量**；人读尾行与 `--json` 均带总数，翻页上限截断时告警 |
| `frpsctl proxies [--type T] [--json]` | 代理列表 | v2 `/api/v2/proxies`（嵌套 `spec/status` 形状），自动翻页；`--type` 非法值报用法错误(2)（不再静默空表）；人读含启用时长（`lastStartAt`） |
| `frpsctl traffic [name] [--json]` | 近 7 天流量历史 | 无参 = 全部代理逐日汇总（**并发查询**）；单代理失败记空不拖垮整体（离线 = 404 无数据） |
| `frpsctl instances [--health] [--json]` | 多实例一行式概览 | 默认只读本地状态；`--health` 额外做三层探测（多实例**并发**，输出顺序稳定） |
| `frpsctl capabilities [--json]` | 能力清单 | 命令树 / 退出码 / 环境变量 / 版本门槛，**从代码派生**（`frpsctl/capabilities.py`）——脚本与文档生成消费同一份数据 |
| `frpsctl prune` | 清理离线代理记录 | `DELETE /api/proxies?status=offline`；清理前后各数一次离线记录，**如实返回清理条数**；**不存在**强制下线在线代理的 API（§18.6） |
| `frpsctl plugin init [--force]` | 生成策略模板 | fail-closed 默认；0600 原子写 |
| `frpsctl plugin check [--bind B]` | 离线校验策略 | 载入 + 回环校验 + 典型裁决试算 |
| `frpsctl plugin serve [--bind B] [--path P]` | 插件服务（前台） | 只允许绑回环；SIGTERM 优雅退出并刷审计；生产用 `plugin service install` 守护 |
| `frpsctl plugin user set\|remove\|list` | 策略用户的结构化编辑 | 只改显式给出的字段；写入前同 `plugin check` 判据复验；未知键保留 |
| `frpsctl plugin audit tail [-n N] [-f] [--json]` | 审计尾部（只读） | 复用日志的反向读取；`-f` 跟随新记录（轮转自动重开）；`--json` 是一次性导出（与 `-f` 互斥） |
| `frpsctl plugin audit stats [--since W] [--json]` | 审计统计（只读） | 流式扫描：总量/允许/拒绝/按用户/按操作/限速抑制/裁决耗时；`--since` 支持 `24h` / `7d` / ISO / unix；**跨轮转文件合并** |
| `frpsctl plugin config list\|set` | 策略级设置的结构化编辑 | `allow_unknown_user` / `require_client_id` / `reject_log_burst` / `reject_log_window` / `admin_*` / `audit.*`；写入前同 `plugin check` 判据复验；未知字段拒绝 |
| `frpsctl plugin service install\|uninstall\|start\|stop\|restart\|status` | 插件的 systemd 集成 | `Restart=always`（登录单点）+ 四项体检；`status` 同时报告 **enabled**；`install --access-log` 把逐请求日志写进 journald |
| `frpsctl web serve [--bind B] [--password P] [--password-file F] [--allow-non-loopback] [--trusted-proxy] [--access-log] [--metrics]` | Web 管理台（前台，§18） | 默认只绑回环、不允许空口令；`--trusted-proxy` 支持反代后的按来源限速；`--access-log` 打开逐请求日志；`--metrics` 暴露 Prometheus 文本（Basic auth，§23.2） |
| `frpsctl web audit tail\|stats [--since W]` | Web 操作审计（只读） | 登录与变更动作的留痕（来源 / 会话指纹 / 结果）；`--since` 与插件审计同语义 |
| `frpsctl web service install\|uninstall\|start\|stop\|restart\|status` | 管理台的 systemd 集成 | 0600 口令文件（明文不进 unit）+ 四项体检；`--trusted-proxy` / `--access-log` 可写入 unit；`status` 同时报告 **enabled** |
| `frpsctl web password show [--json]` | 读回管理台口令 | 显式索取明文；权限过宽时向 stderr 告警 |
| `frpsctl web password set [--stdin] [--prompt]` | 设置（轮换）管理台口令 | 不给输入通道时生成随机口令并只显示一次；0600 原子写；systemd 托管需重启生效 |
| `frpsctl uninstall [--all] [--keep-data] [--keep-bin] [--force] [--yes]` | 完整卸载 | 默认只卸当前实例并要求确认（`--json` 必须显式 `--yes`）；删共享二进制要求覆盖全部实例（或 `--keep-bin`）；运行中默认拒绝（`--force` 先停止）；unit 清理需 root，权限不足汇总为"未清理项" |

### 7.3 退出码

脚本化契约，`errors.py` 统一映射：

| 码 | 含义 | 典型触发 |
|----|------|---------|
| 0 | 成功 | |
| 1 | 未分类错误 | 意外异常 |
| 2 | 用法 / 参数错误 | Typer 参数校验失败 |
| 3 | 配置非法 | pydantic 或 `frps verify` 拒绝；**state.json 损坏**也归此码（`status` 仍可用，`stop`/`start` 会拒绝） |
| 4 | 二进制缺失 / 不可执行 / 版本不受支持 | 未 install，或版本 `< 0.70.0`（§3.6） |
| 5 | 实例未运行 | `stop` 时无进程 |
| 6 | 实例已在运行 | 重复 `start` |
| 7 | dashboard 不可达 | 网络不通 / 未启用 / 鉴权失败 |
| 8 | 权限不足 | 需要 root 的操作 |
| 9 | 变更已自动回滚 | 配置写入后启动失败，已恢复上一版 |
| 10 | 启动即失败 / 停止失败 | 附 frp 原始错误输出；`StopFailed`（SIGKILL 后仍未退出）复用此码，仅文案区分 |
| 11 | 进程所有权冲突 | 身份校验不通过 / systemd 与 direct 混用 |
| 12 | 已启动但健康检查未通过 | L1 进程在、L2 控制面不可达（v0.2.0 新增；进程保留，`status`/`stop` 可用，见 §3.7） |

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
        """三重校验：pid 存活 + 启动时刻一致 + 命令行匹配。

        ⚠️ 设计初稿在这里写的是"`start_time` 为 None 就跳过这一维"——那是**错的**：
        它让三重校验静默退化成两维，而 pid 复用恰恰靠启动时刻识别（§15.5.1 第 1 条，
        R2 的真实事故）。现在**三个维度缺一即 False**（fail-closed）。
        """
        if not pid_alive(self.pid):
            return False
        if self.start_time is None:
            return False                  # 拿不到依据 → 按"不是我们的"处理（ADR-7）
        if not same_process(self.pid, self.start_time):
            return False                  # 要么已退出，要么 pid 被复用
        return _cmdline_matches(proc_cmdline(self.pid), self.binary)
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


def config_flags(*, uses_exec_token_source: bool = False) -> list[str]:
    """构造 frps 标志，**必须放在子命令之前**（Cobra 持久标志）。

    **刻意不接收版本号**：门槛（§3.6）在 `install` 阶段就把 `< 0.70.0` 拒之门外，
    运行时不存在"这个版本认不认这个标志"的分支。早先的设计里确有版本矩阵
    （0.52–0.65 无 `--allow-unsafe`、strict 默认 false），但门槛上收之后那段
    分支已成死代码——留一个被忽略的 `version` 参数只会让人以为这里还在按版本判断。

    `--strict_config=true` 仍**显式传**：0.70+ 上它默认已是 true，但这个默认值在
    v0.53→v0.66 之间变过一次（false→true）。依赖"某个版本的默认值"是脆弱的，
    而显式传值只花一个字节。
    `--allow-unsafe` 仅在配置使用 auth.tokenSource 的 exec 源时追加。
    """
    flags: list[str] = ["--strict_config=true"]        # 显式开启，不依赖版本默认值
    if uses_exec_token_source:
        flags += ["--allow-unsafe", "TokenSourceExec"]  # StringSlice，不是布尔开关
    return flags


def validate_text(text: str, *, binary: Path, workdir: Path, uses_unsafe: bool = False) -> None:
    """verify 与 start 共用同一份标志构造与调用——避免只有一条路径被修好。"""
    validate_semantics(text)                            # 第一层：pydantic（快速失败、信息友好）
    ...                                                 # 第二层：写临时副本 → frps verify（唯一权威）


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

**版本前置条件**：`install` 阶段已拒绝 `< 0.70.0`（§3.6），因此 `config_flags()` 不需要任何版本分支——`--strict_config` 与 `--allow-unsafe` 在目标区间内必然可用。这条前置条件必须在 `install` 与 `--binary` 两条入口上**都**做检查：用户用 `--binary` 指向自编译版本时同样要过版本门槛，否则 §3.6 的保证会被绕过。

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
            proxy_type_counts=status["proxyTypeCount"],    # map，不是总数
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
    # 解包 frps → chmod 0755 → 运行 -v 复验 → 落盘为 frps-<version> → 换软链（--only-download 时跳过）
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
2. **因此升级不是"立即全局生效"，而是"下次启动生效"**。`install`（默认换链）后 `status` 必须能同时显示两个版本：`state.json.version`（正在跑的）与 `frps -v`（软链指向的）。两者不一致时明确输出 `binary : frps 0.70.0 (running) → 0.71.0 (on disk, restart to apply)`。
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
| dashboard 弱口令 | WARN | user/password 为空；**`password` 为空而 user 非空**（frp 把空口令当合法口令，等于只用用户名保护，§3.3）；或 user/password 等于 `admin`/`admin` |
| `transport.tls.force` | WARN | false 时提示可接受明文 frpc 连接 |
| `allowPorts` / `maxPortsPerClient` | WARN | 未设置时提示端口可被任意申请 |
| 端口可绑定性 | ERROR | 对 `bindPort` / `kcpBindPort` / `quicBindPort` / `vhostHTTPPort` / `vhostHTTPSPort` / `webServer.port` 做 bind 探测 |
| `< 1024` 端口 | INFO | 提示 systemd 需要 `CAP_NET_BIND_SERVICE` |
| systemd 与 direct 冲突 | ERROR | 两种所有权同时成立 → 歧义 |
| 插件可达性 | WARN | 配置了 `httpPlugins` 时逐 addr 做 **TCP 探测**；失败提示"客户端将无法登录"（§3.7、§11.2）。**仅告警，不影响退出码** |

`doctor` **只报告，不修复**：每条发现都带"哪个事实导致这条检查存在"，因为一个
说不出理由的检查项，用户只会选择忽略它。

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
| 5 | **唯一权威判定**。在动线上文件之前就把 `frps verify` 请出来，是整条链路上性价比最高的一步。标志由 `config_flags()` 单一构造（§3.6、§8.4） |
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

基于同一协议可以渐进扩展出：按用户分配可用端口段、域名配额、完整审计日志。全部逻辑留在 Python 生态内。

> ⚠️ 历史注记：本小节原列的"**并发连接上限**"与 §11.2.4 的结论**直接冲突**——该能力明确不做（`NewUserConn` 在关键路径、错误只 info 级、无连接 id），以 §11.2.4 为准。

---

## 12. 分发与部署

### 12.1 交付形态

| 方案 | 结论 |
|------|------|
| PyInstaller 内嵌 frps | ❌ 不用：约 14 MB 二进制每次运行都要解包到临时目录；还需处理 Apache-2.0 的 LICENSE 随包分发，并被迫给每个 glibc/musl × x86_64/aarch64 组合单独出包 |
| PyInstaller 只打 Python 侧 | ⚠️ 可选，收益有限（依赖全是纯 Python）。"没有 Python 的服务器"由 uv 路线覆盖（uv 自带 Python），见 §21.4 |
| **pip / pipx + 按需下载 frps** | ✅ **采用** |
| **uv 一键 + install.sh 管道直跑** | ✅ v0.2.5 起支持（§21.4）：`curl … \| bash` 自动下载源码；无 Python 时打印 uv 指引 |

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
ReadWritePaths=/var/log/frps /etc/frps/instances/%i

[Install]
WantedBy=multi-user.target
```

以 `frps@.service` 模板形式安装，多实例即多 unit，与 §6 的实例模型天然对齐。安装后 `owner` 变为 `systemd`，CLI 的 `start/stop/restart/status` 全部委托 `systemctl`（ADR-1）。

**部署前置（v0.2.0 起在安装前强制体检）**：unit 能起来取决于三项环境事实，
任何一项不满足 `service install` 会当场拒绝——它们过去都只在 `systemctl start`
时才暴露，错误现场与安装动作相隔很远：

| 事实 | 失败表现 | 实现 |
|------|---------|------|
| 服务账户存在（默认 `frps`，可 `--user`/`--group`） | `Failed to determine user credentials` | `_account_ids()` |
| 二进制对服务用户可执行 | `Permission denied`（典型：`sudo frpsctl install` 落在 0700 的 `/root` 下） | `_access_problem()` 逐级检查 x 位 |
| 日志目录存在且可写 | `Failed to set up mount namespacing`（`ProtectSystem=strict` 下 ReadWritePaths 必须存在） | `_ensure_log_dir()` 创建并 chown |
| 二进制与实例目录不在家目录下 | unit 看不到路径（`ProtectHome=true` 是挂载隔离，权限位检查看不出） | `_protect_home_conflict()` 识别 `/home`、`/root`、`/run/user` |

另外 unit 的 `ReadWritePaths` 同时包含日志目录与**实例目录**：`ProtectSystem=strict`
下其余路径只读，而 frp 默认要往实例目录写 `./frps.log`（v0.2.0 review 发现的
原设计遗留缺陷）。

安装时还会把实例目录（0700）的属主**移交**给服务用户：unit 以它运行，必须读得到
`frps.toml`（内含 token）。安全性不降级——同机其他用户依然读不到；root 不受权限位
限制，后续运维照常。因此 systemd 模式下应统一以 root 执行 frpsctl。

---

## 13. 测试策略

这类工具最容易出错的不是命令逻辑，而是**边界**：进程身份、并发、配置往返、失败回滚。因此测试分**七层**（单元 / 集成 / CLI / 契约 / 故障注入 / 插件 / 前端与文档，完整定义见 README §开发；下表列出其中六类并补手工冒烟，插件层与前端/文档层见 §11/§18 与 README）：

| 层 | 目标 | 手段 |
|----|------|------|
| **单元** | 进程原语、锁、无损补丁、原子写、标志构造 | `pid_alive` 的 `PermissionError` 分支；`proc_start_time` 解析（含 comm 字段含括号的用例）；`_ensure_table` 幂等；`atomic_write` 在写入中途抛异常时验证原文件未被破坏；**`config_flags()` 的构造**（含 `auth.tokenSource` 的 exec 源分支）；`probe_plugins` 的 URL 解析（无端口 / https 缺省端口 / 多地址有一不可达） |
| **契约** | **防止 frp 升级后字段漂移** | 用真实 frps 二进制在高位端口起服务，断言 §13.1 的**全部强制项**。**这是本文档所有"事实"的自动化守卫** |
| **集成** | 生命周期与回滚 | 临时实例目录：init → start → status → config set（成功）→ config set 非法值（断言线上文件未变 + 退出码 3）→ config set 导致启动失败（断言自动回滚 + 退出码 9）→ stop；**外加升级语义**：install 新版 → 断言运行中进程未受影响且 `state.json.version` 未变 → restart → 断言版本已切换且 `is_ours()` 仍成立（§8.6.1 第 3 条的自伤 bug 守卫） |
| **CLI** | 命令行契约 | `typer.testing` 之外自建 `_Cli` 复用 `map_exceptions()`，使测试断言的就是真实退出码；覆盖退出码表、`--json` 形态、机密不外泄、全局选项位置 |
| **故障注入** | **异常路径的不变量** | monkeypatch 真实系统调用（`os.replace`/`fsync`/`mkdir`、`subprocess.*`、`httpx`、`platform.terminate`）使其单次失败，断言：契约内异常、无遗留进程、无半截/含机密的文件、锁已释放、机密不外泄。见 §13.2 |
| **手工冒烟** | SSH 断开存活、systemd 委托、非 root 权限 | 脚本化 checklist，CI 不覆盖 |

### 13.1 契约层强制断言（`tests/test_facts.py`）

每条都对应文档里一个会**静默出错**的事实——这类错误不会让测试变红，只会让输出悄悄错掉，因此必须显式断言：

| # | 断言 | 守住的事实 |
|---|------|-----------|
| C1 | `/api/v2/system/info` 的 `data.status.proxyTypeCount` **是 `dict[str, int]` 而非 `int`**（真名不是 `proxyCount`）；`sum(...)` 等于 `GET /api/proxy/tcp` + `/api/proxy/http` + `/api/proxy/udp` 各列表长度之和 | §3.2 的字段陷阱（**问题 4 的核心**）。用 `isinstance(payload["proxyTypeCount"], dict)` 直接断言，并做一次"求和 == 逐类型列表长度"的交叉验证——**光断言类型不够，还要证明求和口径正确** |
| C2 | `GET /api/serverinfo` 与 `GET /api/clients` 在目标版本上**仍存在但本工具不调用**：`AdminClient` 的源码扫描断言只出现 `/api/v2/` 前缀（防止有人把 v1 降级路径加回来） | ADR-3 的"只走 v2"决策。这是**反向断言**：不是测 frp，而是测我们没有偷偷用回 v1 |
| C3 | `GET /healthz` **不带 Authorization 头**返回 200 | §3.2 免认证；也防止未来被挪进鉴权中间件后 `start` 的健康检查静默失效 |
| C4 | v2 响应信封形状为 `{"code","msg","data"}`，业务字段在 `data` 下 | §8.5 解析路径 |
| C5 | 在**未设置** `webServer.user`/`password` 时，`GET /api/v2/clients` 返回 200（而非 401） | §3.3 "双空 = 完全不鉴权"。**这是安全基线的事实依据**，必须自动化确认 |
| C6 | `frps verify -c` 对非法配置退出码为 1，对合法配置打印 `syntax is ok` 且退出码 0 | §3.1 唯一权威判定的契约 |
| C7 | 真实二进制满足 §3.6：`frps -v` 输出无 `v` 前缀；`frps --help` 含 `--strict_config` 与 `--allow-unsafe`；`/api/v2/system/info` 返回 200。**并断言 0.69.1 及更早版本被 `install` 拒绝** | §3.6 版本门槛的自动化守卫。做法：`frps --help` 抓标志集合 + 起服务探 v2；低版本断言用 `install --version 0.69.1` 的退出码，**不下载二进制时可 `pytest.mark.skip`，但保留为可执行断言** |
| C8 | 在 `user = "admin"` + 口令为空时，无凭据请求得 **401**，而 `admin:`+空口令得 **200**；只设 `password` 时须用 `:secret` 才能进 | §3.3 的实测边界。它守的是 `check_dangerous_combination()` **只拒绝两者全空**这个判据分工：若哪天 frp 改成"口令非空才启用鉴权"，"有 user + 空口令"会**静默退化成完全不鉴权**，而校验仍会放行——安全缺口就此产生 |
| C9 | `DELETE /api/proxies` 只接受 `?status=offline`（无参数返回 400），语义是 `ClearOfflineProxies()`——frp **没有**强制下线在线代理的 API | §18.6 的实现期发现。它守住"`prune` 不做 kick"这条纠正：旧 `kick` 命令基于对该端点的误读，从未工作过 |
| C10 | `GET /api/v2/proxies/{name}/traffic` 对离线/不存在的代理返回 **404（无数据）**，而非错误 | §18.9 新增。CLI `traffic` 与 Web 趋势图都依赖"一个离线代理不拖垮整体" |

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

CI 矩阵：Linux（Python 3.11 / 3.12 / 3.13 / 3.14），容器内跑全部七层；**不设其他操作系统的 job**——平台范围由 §3.4 决定，CI 与之一致。

**特别建议**：把附录 B 的核对命令打包成 `tests/test_facts.py`，C1–C7 全部落在这个文件里。这样"事实基线"就具备了自动保鲜能力。

---

## 14. 里程碑与工作量

| 阶段 | 范围 | 估算 |
|------|------|------|
| **M0** 骨架与事实冻结 | 包结构、错误与退出码映射、`tests/test_facts.py`、CI | 0.5 天 |
| **M1** 生命周期 | `install` / `start` / `stop` / `restart` / `status` / `log`，含锁、身份校验、启动早退检测 | 2 天 |
| **M2** 配置闭环 | tomlkit 无损补丁、verify 预检、原子写、备份历史、`config` 子命令、自动回滚 | 1.5 天 |
| **M3** 运维面 | `doctor`（含安全 lint）、`service install`、systemd 委托、`kick`（**历史规划**：`kick` 基于对 API 的误读已删除，见 §18.6；当前由 `prune` 取代） | 1 天 |
| **M4** 硬化与文档 | 契约测试、集成测试、SSH 断开冒烟、README 与手册 | 1 天 |
| **合计** | 可上生产的 MVP | **约 6 天** |
| **M5** 插件（独立阶段） | FastAPI 插件服务 + 多用户 / 配额 / 审计 + 进程守护（**历史规划**：实现改用标准库 `ThreadingHTTPServer`，FastAPI 只是 §11.3 的示意代码；见 §11.2.2） | 视需求 |

工作量分布说明：命令骨架本身只占约 1 天，**其余成本在进程生命周期边界、配置无损往返、失败回滚与真机验证**——这些正是决定"能不能上生产"的部分。M5 与 MVP 解耦，因为它是独立进程、独立风险面（§11.2）。

---

## 15. 风险登记表

| # | 风险 | 影响 | 缓解 |
|---|------|------|------|
| R1 | frp 升级后 API 字段改名 | `status` 静默出错 | 契约测试作为升级门禁，**§13.1 C1/C2 专门断言 `proxyTypeCount` 是 map 且求和口径正确**；`AdminClient` 对缺字段抛明确异常而非裸 `KeyError` |
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
| R12 | **无条件传 `--strict_config` / `--allow-unsafe` 到旧版本** | 未知标志 → frps 直接退出，表现为"配置合法却启动失败" | §3.6 标志兼容矩阵 + 单一构造点 `config_flags()`（§8.4）+ §13.1 C7 断言 |
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

**理由**：Windows 分支与"不承诺生产可用"自相矛盾——写了一套永远不会被验证的代码，却要为它的正确性背书。删掉后 §3.4 从"三个平台的矩阵"变成"一组必须满足的内核依赖"，可测试、可断言。同时新增 `proc_cmdline()`（读 `/proc/<pid>/cmdline`），让 §8.3 里 `_cmdline_matches` 这个引用有了确定的实现。

### 16.2 版本门槛重写（问题 2）

**修订前的错误**：文档写"`0.52 – 0.70` 允许 + WARNING"，但这区间里藏着两个硬失败点。逐版本源码对比后的真值：

| 版本 | `--strict_config` | 其默认值 | `--allow-unsafe` | v2 API | 结论 |
|------|------------------|---------|-----------------|--------|------|
| `< 0.52` | 不存在 | — | 无 | 无 | 拒绝（配置体系换代） |
| `0.52.x` | **不存在** | — | 无 | 无 | **拒绝**（无键名护栏） |
| `0.53.0 – 0.65.x` | 有 | **false** | 无 | 无 | 允许 + WARNING（**已被 §16.2.1 取代**：门槛上收后一律拒绝） |
| `0.66.0 – 0.69.x` | 有 | true | 有 | 无 | 允许 + WARNING（**已被 §16.2.1 取代**：门槛上收后一律拒绝） |
| `>= 0.70.0` | 有 | true | 有 | **有** | 完全支持 |

**三个直接后果**（均已写入设计）：

1. `--strict_config=true` **必须显式传**——`0.53–0.65` 默认 false，不传等于护栏消失；
2. 标志构造集中到 `config_flags()`，`verify` 与 `start` 共用（"两处调用、一处判定"）；
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
>
> ⚠️ 本章各小节里的用例数（226 / 277 / …）是**当时**的快照，用于记录每轮增量；
> 当前总数以 CHANGELOG 最新条目为准（v0.3.0 起为 735 条；非契约 705 条）。

本章记录实现过程中**文档被现实修正**的地方，以及只有真机测试才能照出来的问题。
它的用途是：下次改这块代码的人，不必重新踩一遍。

### 17.1 状态

| 项 | 状态 |
|----|------|
| 代码 | `src/frpsctl/`：core 19 模块（含 `web_audit.py`）+ `plugin/` 6 + `web/` 5（api/auth/cache/metrics/server）+ CLI（app/runtime/ui + `commands/` 7 个命令模块）+ 顶层共享（report/capabilities/env/docs）（v0.3.0 现状） |
| 测试 | **226 个用例**；单元 / 集成 / CLI / 契约 / **故障注入** 五层（§17.6 后，含 4 条真 frpc 契约）。最新一轮的数字（277 条）见 §17.7 |
| 覆盖率 | **81%**（`pytest --cov`；剩余未覆盖集中在渲染分支与需 root/网络的路径） |
| 真机验证（M0–M4） | frps **0.71.0** 全链路冒烟通过：`init → verify → start → status → config set（自动重启）→ doctor → config rollback → stop` |
| 真机验证（M5） | **真 frpc → 真 frps → 我们的插件**：授权用户建代理成功、未授权用户登录被拒、白名单外端口被拒且理由回到客户端 |
| 契约层 | C1–C8 全绿（真二进制），CI 里作为升级门禁 |
| 插件真机契约 | 真 frpc → 真 frps → 我们的插件：4 条全绿（授权建代理成功、未授权登录被拒、白名单外端口被拒且理由回到客户端、审计留痕） |

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

1. **`config_flags()` 单一构造点**（§8.4）：`verify` 与 `start` 共用，因此"标志是否正确"只需要在一处验证（§13.1 的 `test_our_flag_construction_is_accepted` 用的是真 frps）。
2. **回滚判据只有 L1 ∧ L2**（§3.7）：集成测试 `test_plugin_failure_does_not_break_gate` 直接断言"插件不可达时 `gate` 仍为真"，把这条边界钉成了可执行事实。
3. **`state.json` 存 `resolve()` 后的真实路径**（§8.6.1）：升级语义测试在换软链后仍能通过三重校验——R13 那个"自伤 bug"被测试永久守住。

### 17.5 与设计的一处偏离（有意为之）

命令表（§7.2）里 `--json` 写在子命令后面（如 `doctor [--json]`），而实现把 `--json`
做成了**全局选项 + 每个子命令的选项**，两种位置都可用。原因：只提供全局选项时
`frpsctl status --json` 会报 `No such option`，与文档写法及用户直觉都不符。
行为对脚本完全兼容（`--json` 在前后都能用），因此不视为破坏性偏离。

> **§17.6 已把这条偏离收回**：逐命令打补丁的做法被证明是治标的（见 §17.6.6），
> 现在由 `_AnywhereGroup` 在解析前统一前移全局选项。各子命令上保留的 `--json`
> 选项与 `with_json()` 仍在，属于冗余而非缺陷。

---

## 17.6 第二轮全量 review（M0–M5 + 文档定稿 + 真 frpc 契约复核后）

第二轮 review 的方法与 §15.5 不同：**先把全部源码、测试与两篇文档读完，再逐条
实测**（不是只读代码）。结论是上一轮的修复质量可信，但**"文档承诺 vs 实现"的
缝隙仍在，且这一轮新发现的问题里有两条比第一轮更隐蔽**。

统计：修复 **13 条**问题（见 §17.6.8 的逐条清单），新增 **62 个用例**（164 → 226），覆盖率 76% → **81%**，
且盲区从"关键路径"退到"渲染与错误分支"：

| 模块 | 之前 | 之后 | 补的是什么 |
|------|-----|-----|-----------|
| `core/systemd.py` | 52% | **98%** | unit 渲染、systemctl 委托、所有权探测（`is_active` / `same_config_active`） |
| `core/release.py` | 58% | **83%** | 解包（含路径穿越）、`-v` 复验、原子就位、校验和顺序 |
| `core/instance.py` | 86% | **89%** | 快照序号分配与耗尽 |
| `core/version.py` | 95% | **96%** | 版本探测超时收口 |

### 17.6.1 最重要的一条：空口令 ≠ 没有口令

§3.3 原文只写了"两者全空 = 完全不鉴权"，实现也据此把
`check_dangerous_combination()` 写成"两者全空才拒绝"。**判据本身是对的**（实测
确认：`user="admin"` + 空口令时无凭据请求得 401，确实启用了鉴权），但**没人提示
"有 user 但口令为空"这个等价于免口令的情形**：

- `config set webServer.addr '"0.0.0.0"'` 在这个组合下**放行**（因为启用了鉴权）；
- 而 `doctor` 一条提示都没有——实测确认，`(user="admin", password="")` 下
  `doctor` 静默通过。

于是出现一个真实的缺口：用户可以把 dashboard 暴露到公网，而唯一的防护是一个
**已知用户名 + 空口令**。修复：

| 位置 | 修复 |
|------|------|
| §3.3 | 补上"鉴权开关是**任一非空**、空口令是合法口令"的实测表（4 行配置 × 2 种请求） |
| `healthcheck.DashboardInfo.auth_enabled` | docstring 写明它是"是否施加 Basic Auth"而非"口令是否够强" |
| `doctor._check_dashboard` | 新增分支：`password` 为空而 `user` 非空 → **WARN**（"等于只用用户名保护 dashboard"） |
| `test_facts.py::TestC8PasswordlessAuth` | 把这条事实做成契约断言（起真 frps 实测 401/200） |
| `test_plugin.py::TestDoctorDashboardCredentials` | 6 条用例覆盖新分支及其边界（含"非回环 + 空口令仍只是 WARN"） |

**判据边界是刻意保留的**：`config set` 仍只拒绝"两者全空 + 绑非回环"。"有 user、
口令为空"启用了鉴权，属于强度不足而非缺口，是用户的显式选择——由 `doctor` 告警
比否决写配置更合适。

### 17.6.2 异常契约在三处漏出裸异常

`release._verify_binary` 的 `subprocess.run(..., timeout=10)` **没有接住
`TimeoutExpired`**：它既不是 `FrpsctlError` 也不是 `OSError`，冒到 CLI 就是
"未分类错误(1)"——把"下载的二进制卡住"误报成"工具内部出错"，用户会去查 frpsctl
的 bug。同类问题还有 `instance.next_history_slot` 耗尽重试时的裸 `RuntimeError`。

两处都已收口为契约内异常（`BinaryError` / `FrpsctlError`），并补了断言退出码的用例。
**教训与 §15.5.2 一致**：异常路径上的"降级"必须可见，而"异常类型"本身就是可见性
的一部分。

### 17.6.3 供应链关键路径零覆盖

`release._place_binary`（解包 → `-v` 复验 → 原子就位）是"校验不通过绝不落盘"
这条承诺的**最后一道门**，而它此前**一个用例都没有**——因为要造真实 tar 字节流。
补上之后立刻证明了两条不变量：

- 资产里的 `frps` 版本低于门槛（§3.6）时，**目标文件不出现、临时文件不残留**；
- 复验超时 / `ENOEXEC` 都收口成退出码 4。

`render_unit` 同理：它是纯函数、零成本可测，却决定了 systemd 托管下的全部行为。
现在断言 `ExecStart` 是**具体版本路径而非软链**——这正是 §8.6.1 要求
`install` 输出里显式提醒用户的那个差异。

### 17.6.4 顺手修掉的一个实现缺陷

`Systemd.install_template` 直接 `write_text(template_path)`，**没有先建
`unit_dir`**。真实部署里 `/etc/systemd/system` 总是存在的，所以这不是线上 bug；
但它是"依赖环境恰好满足"的写法，且让"渲染出的 unit 到底长什么样"这件事无法在
测试里断言。测试逼出这个假设之后补上了 `mkdir(parents=True, exist_ok=True)`。

### 17.6.5 `--verbose` 是个空选项（比没有更糟）

`--verbose, -v` 出现在 README 的全局选项表与 §7.1 里，被 `build_context()` 解析进
`AppContext`，然后**全仓库没有任何地方读过 `app_ctx.verbose`**。用户加上它以为能
看到诊断，实际输出一字不差。

修复方式是把它做成**真功能**而不是删掉：`ui.set_verbose()` 设一个进程级开关
（`--verbose` 要影响的调用点在 `core/` 里，那些函数没有 CLI 上下文），
`ui.trace()` 把诊断写进 **stderr**：

```
[trace] 读取二进制版本：…/frps-0.71.0 -v
[trace] 执行权威校验：…/frps-0.71.0 --strict_config=true verify -c …/tmpXXXX.toml
[trace] 派生进程：…/frps-0.71.0 -c …/frps.toml（stdout → …/startup/startup-….log）
```

**必须走 stderr**：`--json` 的 stdout 是机器可读契约，一行 trace 就能让 `jq` 解析
失败。测试里专门有一条断言 `--verbose --json` 的 stdout 仍是纯 JSON。

### 17.6.6 一处"文档 vs 实现"的广泛不一致：全局选项位置

README 写着"全局选项（写在子命令前后都可以）"，§17.5 也把这当成已解决的偏离。
实际上只有 `--json` 被**逐命令**打了补丁（19 处 `with_json()`），而：

```console
$ frpsctl config edit --yes
Error: No such option: --yes
$ frpsctl verify --verbose
Error: No such option: --verbose
```

原因是 Typer/Click 原生只解析"子命令之前"的选项。**逐命令打补丁是治标的**：每加
一条子命令、每加一个全局选项，都可能再漏一次（这次就漏了两个）。

治本的修复是 `cli._AnywhereGroup.parse_args`：在解析**之前**把散落在子命令后面的
全局选项整体前移到前面。一条规则覆盖全部命令与全部变体。三条必须守住的边界：

| 边界 | 为什么 |
|------|-------|
| `--version` **不在**全局名单里 | 它是 `install` 的版本参数。误搬走会让 `install --version 0.69.1` 变成"打印 frpsctl 版本并退出 0"——一个**静默的错误成功** |
| 短名 `-v` 不参与前移 | 与 `log -n` / `status --interval` 等局部短选项挤在一起，盲目前移会把子命令自己的参数搬到错误位置 |
| 只搬"值紧随其后"或 `--name=value` | 避免把 `--config` 后面那个**位置参数**误当成它的值 |

`config edit` 另外补了一个局部的 `--yes`（`init` 早就有），使
`frpsctl config edit --yes` 与 `frpsctl --yes config edit` 都能用。

### 17.6.7 测试脚手架的一个自身缺陷

CLI 测试的 `_Cli.invoke` 把 stdout 与 stderr **合并**成一个字符串再断言。这在
`--verbose` 出现之前是无害的，但它使"诊断只能进 stderr"这条契约**无法被断言**——
第一版 `--verbose` 测试因此得到假结果。已改为同时保留 `stdout` / `stderr` 两个
独立字段（`output` 仍保留为合并视图，既有断言不受影响）。

**这与 §15.5.3 是同一类教训**：测试基础设施本身也会掩盖真相。合并输出流的脚手架
让"stdout 是否干净"这件事永远测不出来。

### 17.6.8 本轮修复清单（可逐条核对）

| # | 问题 | 类型 | 落地 |
|---|------|------|------|
| 1 | `transaction._restore` 有**两段相邻 docstring**，后者是被遮蔽的死字符串，且两段描述了不同实现 | 文档 | 合并为一段（保留"取自快照"+"曾假回滚"两条信息） |
| 2 | "有 user、口令为空" = 免口令 dashboard，`doctor` 完全静默 | **安全可见性** | `doctor` 新增 WARN 分支；§3.3 补实测表；新增 C8 契约断言 + 6 条 doctor 用例 |
| 3 | `status --watch` 的 Ctrl-C 会多刷一屏（`except: return` 让循环后的调用变成"退出前再打一次"） | 行为 | 改为"不 watch 就渲染一次并返回；watch 只在循环内渲染" |
| 4 | `_assert_v2_api` 末尾 `last` 变量赋值后从未被读 | 死代码 | 删除变量与末尾的 `_ = last` |
| 5 | `release.install` 的 `--insecure` 判断发生在**下载之后** | 可读性/效率 | 校验和判定整体前移到下载之前 |
| 6 | `release._verify_binary` 未接住 `TimeoutExpired`；`instance.next_history_slot` 耗尽时抛裸 `RuntimeError` | **异常契约** | 分别收口为 `BinaryError(4)` / `FrpsctlError(1)`，并加断言退出码的用例 |
| 7 | `--verbose` 被文档承诺、被解析、**从未被读过** | 空功能 | 做成真功能：`ui.set_verbose()` + `ui.trace()`，诊断只进 stderr，覆盖 `verify` / 版本探测 / 派生进程 / 下载 |
| 8 | 全局选项写在子命令之后报 `No such option`（README 却承诺两者皆可），`--yes`/`--verbose` 都中招 | **文档 vs 实现** | `_AnywhereGroup.parse_args` 统一前移；`config edit` 补局部 `--yes`；新增 5 条位置矩阵用例 |
| 9 | `Systemd.install_template` 未先建 `unit_dir` | 环境假设 | 补 `mkdir(parents=True, exist_ok=True)` |
| 10 | CLI 测试脚手架合并 stdout/stderr，使"诊断只进 stderr"无法被断言 | 测试基建 | 拆成独立字段（`output` 保留为合并视图） |
| 11 | 设计文档 §8.3 的 `is_ours()` 仍在演示**已被判定为高危**的旧写法；`verify_flags` / `proxyCount` / `_cmdline_contains` 等命名与实现漂移；§8.4 还写着"按版本构造标志" | 文档 drift | 逐处修正为当前实现（含 §13.1 C1 的字段名、附录 B 第 4b 条） |
| 12 | **`--with-frpc` 在"frps 已在盘上"时完全不可达**：`dest.exists()` 那条捷径直接 `return`，于是补装 frpc 永远装不上，而输出还报成功 | **功能失效（CI 掩盖）** | frps 与 frpc 的落盘判定解耦为 `place_frps` / `place_frpc`；新增 `TestWithFrpc` 4 条（含"已有 frps 时补装 frpc"这条回归） |
| 13 | `InstallResult.switched` 把"frps 是否换链"与"是否要求换链"混为一谈，导致两个错误输出：已有 frps 时打印"未切换软链"，以及把"同版本已在盘上"误报成"已按 `--only-download`" | 输出误导 | 语义拆清：`switched` = 本次是否真的建立了指向；新增 `switched_frpc`；`--only-download` 与"同版本已就位"两种原因分开渲染 |

### 17.6.9 装上真 frpc 之后的契约层复核（§17.6.8 第 12 条的由来）

为了跑通那 4 条被 skip 的插件契约用例，执行了 `frpsctl install --with-frpc` —— 结果
**frpc 没有出现**，而命令**报了成功**（`downloaded=False`、退出码 0）。这是一条真实
的功能失效，也顺带解释了它为什么能活到现在：

| 环节 | 为什么会漏 |
|------|-----------|
| `install` 的控制流 | `if dest.exists() and not force:` 里直接 `return`，**`with_frpc` 那段在它之后**，永远不可达 |
| CI | CI 每次都是全新数据目录 → `dest` 不存在 → 必然走完整路径 → `with_frpc` 生效。**CI 从来没有覆盖过"机器上已有 frps"这个状态** |
| 本地开发 | 本项目的开发机早就装过 frps，因此这条捷径**总是**被走到 —— 也就是说这个功能在本地从来就没成功过，只是没人注意（契约层只是 skip，不是 fail） |

修复方式是把两者的落盘判定**解耦**：

| 变量 | 含义 |
|------|------|
| `place_frps = force or not dest.exists()` | 本次是否需要写入 `frps-<version>` |
| `place_frpc = with_frpc and (force or not frpc_dest.exists())` | 本次是否需要写入 `frpc-<version>` |

两者都为假时才走"不碰网络"的捷径；只要有一个为真就下载**同一个资产**（frps 与 frpc
在同一 tar 包里，因此仍只下载一次），各自独立就位。

顺带暴露并修掉第二处**输出误导**：`InstallResult.switched` 原先同时承担"frps 软链
本次是否被切换"与"用户是否要求切换"两个含义，于是

- "已有 frps + 未加 `--only-download`" → 打印"已按 `--only-download` 落盘，未切换软链"（**用户根本没加那个参数**）；
- 而"已有 frps + 未加 `--only-download`"时**确实应该**动链（幂等，且能修回被手工改歪的链）。

现在语义拆清：`switched` = 本次是否真的建立了指向；新增 `switched_frpc`；CLI 把
"`--only-download`"与"同版本已在盘上"两种原因**分开渲染**。验证矩阵：

```console
$ frpsctl install --version 0.71.0 --with-frpc         # 全新目录
frps 0.71.0 → …/bin/frps-0.71.0
当前版本软链 → …/bin/frps
frpc 0.71.0 → …/bin/frpc-0.71.0
      （供插件契约测试使用；软链已指向它）

$ frpsctl install --version 0.71.0 --with-frpc         # 再跑一次（不碰网络）
frps 0.71.0 → …/bin/frps-0.71.0
当前版本软链 → …/bin/frps

$ ln -sfn /nonexistent …/bin/frps && frpsctl install   # 手工改歪的链会被修回来
$ frpsctl install --only-download                      # 显式不动链
已按 --only-download 落盘，未切换软链。
```

装上真 frpc 之后，**全量 226 个用例 0 skipped**：契约层 C1–C8 与
`TestRealFrpcContract` 的 4 条全部在真二进制上通过——包括最关键的那条
"白名单外端口被拒、且**拒绝理由真的回到了客户端**"（`6000-6010` 出现在 frpc 输出里）。

### 17.6.10 本轮**未**修的东西（明确记账）

| 项 | 为什么不修 |
|----|-----------|
| `plugin serve --json` 之后仍阻塞 | 这是**设计意图**（它是前台服务），已在 `--help` 与 README 里写明"一次性状态、随后前台运行"；改成流式 JSON 反而让它无法被 systemd 正常托管 |
| 各子命令上冗余的 `--json` 选项与 `with_json()` | 有了 `_AnywhereGroup` 之后确实冗余，但保留它们让**旧写法继续可用**，且删掉要动 19 处调用点——收益不足以承担回归风险 |
| ~~4 条真机契约用例需要 `frpc`~~ | **已解决**：`frpsctl install --with-frpc` 装上真 frpc 后，`tests/test_plugin.py::TestRealFrpcContract` 4 条全部通过（见 §17.6.9）。没有 frpc 时它们会 skip 而不是 fail |

---

## 17.7 第三轮全量迭代（v0.2.0：安全缺口、部署闭环与发布链路）

第三轮以**便捷性、易用性、稳定性、可靠性**为目标做全量复核，方法与历次一致：
先把全部源码、测试与两篇文档读完，再逐条实测（不只看代码）。统计：修复 **15 条**
问题（含 1 条安全缺口、3 条正确性缺陷），新增 **51 个用例**（226 → 277），
覆盖率维持 81%；CI 新增 ruff、覆盖率门禁与 frp **0.70.0 下界契约矩阵**。

### 17.7.1 安全缺口：内联表机密从 diff 泄露（实测复现）

`mask_diff` 只识别逐行赋值，而 TOML 的内联表会把机密藏进"值"里：

```console
+auth = { token = "SUPERSECRET123456" }     ← 修复前原样打印
```

修复：`_mask_inline_value` 把值解析出来后复用 `mask_tree` 的完整点分路径匹配
（任意深度都能覆盖），并对"解析失败但形似含机密"的行**保守整体打码**。同时修掉
`[[数组表]]` 的表名解析（旧正则把 `[` 混进表名，使该表下的敏感键路径失配）。

### 17.7.2 正确性：三条

| # | 问题 | 实测证据 | 修复 |
|---|------|---------|------|
| 1 | `start` 在 L2 失败时**退出码 0 且 stderr 静默** | 占住 dashboard 端口后 start：`L2 control fail`、退出码 0 | 新增退出码 **12**（§7.3），CLI 统一检查 `StartReport.healthy`；进程保留，`status`/`stop` 可用 |
| 2 | 一次 `config rollback` 产生**两份内容相同的快照** | 快照 0001 → 回滚后出现 0002/0003 | 快照只在 `apply_change` 的**实例锁内**创建一次；`snapshot_action` 保留 `rollback N` 记账 |
| 3 | 进程在早退窗口之后死亡会留下假 RUNNING | `exit_after` 假件（grace 后退出）实测 | `_await_health` 发现 L1 失败立即返回；`start` 清理 state 并按启动失败(10)收尾 |

### 17.7.3 稳定性：六条

| # | 问题 | 修复 |
|---|------|------|
| 1 | 配额预占在**全局锁内**做 dashboard 查询（timeout 2s），一个用户的慢查询挂住所有人（frp 侧对插件无超时） | 按用户拆锁；网络调用不持全局锁（新增事件驱动的并发回归测试） |
| 2 | `plugin serve` 的 SIGTERM handler 安装不在 `try` 内——安装瞬间收到信号会绕过审计刷盘 | 纳入 `try`；测试改用 `/proc/<pid>/status` 的 `SigCgt` 掩码做就绪判定，消除 flaky |
| 3 | `--with-frpc --only-download` 仍会切换 frpc 软链（同一参数两套语义） | 两条链统一遵守 `switch`；两边在盘上时幂等校正 frpc 链 |
| 4 | `log` 依赖外部 `tail`，缺失时报"未分类错误(1)" | 纯 Python tail（含轮转后按 inode 重开），零外部命令 |
| 5 | `status --watch --json` 输出多行缩进串，无法逐行消费 | NDJSON（单行） |
| 6 | `plugin init` 用 `write_text` 落盘策略文件（半截文件 + 权限窗口） | 改 `atomic_write` |

### 17.7.4 部署闭环：systemd 三查一移交

按既有文档部署 systemd **必然踩坑**（`sudo frpsctl install` 把二进制放进 0700 的
`/root`，unit 以 `frps` 运行读不到；模板硬编码 `User=frps` 却不检查账户存在），
且错误现场与操作动作相隔很远。现在 `service install` 在安装前完成三项体检
（账户存在、二进制逐级可达、日志目录可写），安装时把实例目录属主移交给服务用户。
详见 §12.2。

### 17.7.5 便捷性与发布

- `install --mirror`（可重复）/ `FRPSCTL_MIRROR`：镜像可配置（此前文档承诺
  "可被镜像替换"但实现硬编码）；默认版本改由 `RECKONED_VERSION` 单一来源生成。
- `status` 补 `listen` 行与 JSON 字段（对齐 §7.4）；`--watch --json` 为 NDJSON。
- `init` 生成后立即语义自检（非法端口不再落盘）；回环判断统一为
  `healthcheck.is_loopback`（修掉 `127.0.0.2` 上 doctor 与 schema 的判据分歧）。
- shell 补全启用（`--install-completion`）；`start --foreground` 在帮助里明确
  "不写 state、stop 管不到它"。
- 发布链路：版本号单一来源（hatchling 读 `__init__.py`）、CHANGELOG、release
  workflow（tag 与版本一致性校验 → GitHub Release → PyPI Trusted Publishing
  由仓库变量 `PYPI_PUBLISH` 开关）、`py.typed`。

### 17.7.6 未做与代价（明确记账）

| 项 | 结论 |
|----|------|
| `self update` | 不做：需要探测 pip / pipx / venv 安装形态，收益低而风险高；升级路径由重跑 `install.sh` 与 `pipx upgrade frpsctl` 覆盖 |
| `restart --no-rollback` | 维持原边界（§2.2、§7.2） |
| 契约层固定 `0.71.0` | 已扩展为 0.70.0（下界）+ 0.71.0（目标）双版本矩阵，下界本地实测 21/21 通过 |

---

## 17.8 第三轮回归 review（v0.2.0 发布前）

方法与前两轮一致：**完整 diff 逐行审查 + 对抗性实测 + 文档-代码交叉核对**。
本轮在 §17.7 的修复之上又发现 **5 个真实缺陷**——全部位于"异常/边缘路径"或
"部署链路的下游"，新增 **19 个回归用例**（277 → 296），覆盖率 81% → 82%。

### 17.8.1 机密打码的跨行盲区（实测复现）

§17.7 的打码修复只覆盖单行形态，review 用四组对抗用例证明多行结构仍会泄露：

```console
+token = """            ← 多行字符串：内容行原样打印
+SECRET
+"""

+auth = {                ← 内联表跨多行
+  token = "SECRET",
+}
```

修复：`mask_diff` 重写为**跨行状态机**——三引号 / 未闭合括号开启遮蔽状态，
逐行定点打码（`_mask_fragment` 处理内联片段与裸赋值续行），闭合后恢复常规处理；
非敏感多行数组（`allowPorts`）原样保留。另有两处加固：裸键名
（`token`/`password`/`clientSecret`）纳入敏感判定；非敏感键的**未闭合起始行**
也走定点打码（`foo = { token = "x"` 这种半行）。

### 17.8.2 systemd 部署链路的三个下游缺陷

| # | 缺陷 | 后果 | 修复 |
|---|------|------|------|
| 1 | `config set` 重建配置时**夺回属主**（root） | 上一轮刚移交给服务用户的配置，下一次改配置后 frps 就读不到了 | `atomic_write` 保留已存在文件的 uid/gid（fchown 失败不阻断写入） |
| 2 | `ReadWritePaths` 只放行日志目录 | `ProtectSystem=strict` 下实例目录只读，而 frp 默认要写 `./frps.log` → unit 启动即失败 | unit 的 ReadWritePaths 加上实例目录（`{config_dir}/%i`） |
| 3 | 家目录下的路径被 `ProtectHome=true` 挡住，且**权限位检查看不出来**（挂载隔离≠文件模式） | `~/.local` 默认路径部署装完后启动失败 | `_protect_home_conflict()` 识别 `/home`、`/root`、`/run/user` 并提前拒绝 |

另补：unit 需要的实例内 `frps.toml` 缺失时提前拒绝（`--config` 指向外部路径的
用户尤其容易踩——unit 不会使用那个文件）。

### 17.8.3 边缘路径的两处收口

| # | 问题 | 修复 |
|---|------|------|
| 1 | 负数数值选项行为诡异：`--interval -1` 产生"未分类错误(1)"；`--timeout -1` 跳过等待直接 SIGKILL（参数笔误造成不可逆动作） | 全部数值选项加 `min` 约束 → 用法错误(2) |
| 2 | `status --watch` 重定向到文件时写入 ANSI 清屏码污染输出 | 仅在 `stdout.isatty()` 时清屏 |

### 17.8.4 交叉核对与最终验收

- GitHub Actions 两份 workflow 经 YAML 解析验证；wheel 构建自检（含 `py.typed`）。
- 文档-代码交叉核对修 3 处 drift（CHANGELOG 用例数、README 版本示例、测试文件
  "五层/第五层"措辞）。
- 对抗性实测记录：`mask_diff` 16 组边界（含真实 `config edit` 端到端）、`--` 三组、
  tail 空文件/无尾换行/百万行（1.12s、47MB）、负数选项 4 组、systemd 体检 7 组。
- **最终验收：296 用例 0 skipped**（含真 frps 0.71.0 + 真 frpc 端到端契约），
  ruff 全绿，覆盖率 82%，非契约层 271 条。

---

## 17.9 第四轮全量迭代（v0.2.1：并发根治、策略 fail-open 与便捷性）

第四轮目标是"便捷性、易用性、稳定性、可靠性"四轴全覆盖。方法与前几轮一致：
**先把全部源码、测试与文档读完（18,028 行，无截断），再逐条实测**。统计：修复
**16 条**问题（含 2 条安全/正确性缺陷、1 条 P0 并发缺陷），新增 **47 个用例**
（296 → 343），覆盖率 82% → **83%**，并新增 6 个命令。

### 17.9.1 P0：并发配置变更静默丢失（前三轮的结构性盲区）

`config set` 是"读-改-写"，但**"读"（`plan_set`）在实例锁外**：两个并发变更都
基于同一份旧文本生成完整新文本，后写入者覆盖前者，**两条命令都报成功**。实测
复现（确定性，不依赖线程时序）：

```console
$ 两个 plan_set 均基于 bindPort=7000 的初始配置，先后 apply：
    A: config set bindPort 8000        → 成功
    B: config set maxPortsPerClient 30 → 成功
$ 最终配置：bindPort = 7000             ← A 的变更被静默覆盖
```

前三轮没发现它的原因很具体：**单线程逻辑完全正确**，代码读起来无懈可击；
只有"两个并发调用 + 特定时序"才触发。这是串行审查的结构性盲区。

修复是**锁边界的重新划分**（不是补丁）：`transaction.py` 只暴露三个入口，
每个都在**同一把锁内**完成"读取 → 生成候选 → 落盘"：

| 入口 | 场景 | 候选生成 |
|------|------|---------|
| `apply_set` | `config set` | 锁内 `plan_set`（noop 判定同样在锁内） |
| `apply_edit` | `config edit` | 锁内 **CAS**：编辑器交互在锁外，写回前确认文件未被并发修改 |
| `rollback_to` | `config rollback` | 锁内选快照并读取 |

原 `apply_change`（接受已在锁外生成的 `new_text`）收敛为内部函数 `_apply_locked`
——把"锁的边界"从调用方纪律变成 API 结构：CLI 不可能再写错。

新增回归用例三条，其中最关键的一条是**确定性不变量**（不依赖时序）：
`cfg.plan_set` 被调用时 `is_locked()` 必须为真。

### 17.9.2 安全：策略 JSON 的宽松转换是 fail-open

`bool(raw.get("allow_unknown_user", False))` —— JSON 里写 `"false"`（带引号的
字符串）时 Python 的 `bool("false")` 是 **True**。实测确认：

```console
$ 策略里 allow_unknown_user: "false"
→ allow_unknown_user = True     ← 用户以为关了鉴权后门，实际完全打开
→ allow_random_port  = True     ← 端口白名单同理形同虚设
```

修复：`policy.py` 引入 `_strict_bool` / `_strict_int` / `_strict_float` /
`_strict_list` 四个严格转换（`bool` 不是 `int`、字符串不是数组、类型错误报
退出码 3 并指出字段名），并修掉 `"audit": "false"` 走到 `"false".get(...)` 的
裸 `AttributeError`。负数的**钳制**语义（`flush_every` 最小 1）保持不变。

同时兑现一个**零引用的死配置**：`reject_log_burst` / `reject_log_window` 在
§11.3 承诺了"拒绝风暴限速"但实现里没有任何地方读过它。现在 `DecisionEngine`
按用户限速：拒绝风暴期间**请求仍被拒绝**（安全语义不变），审计停止逐条刷写，
被抑制的条数累计到下一条记录的 `suppressed` 字段上——降采样必须可见。

### 17.9.3 其余修复（14 条）

| # | 问题 | 修复 |
|---|------|------|
| 1 | `--health-timeout` 对 systemd 实例**静默无效**（`_start_via_systemd` 硬编码 10s） | 参数透传 + 回归用例 |
| 2 | `same_config_active` 只扫 `frps*`：自建 unit（`my-tunnel.service`）指向同一配置时漏检，双起防护有洞 | 改为全量 active service + 逐个 ExecStart 比对 |
| 3 | `status` 调 `resolve_owner()` 两次（systemd 下 4 个子进程 + TOCTOU） | 新增 `state_with_owner()` 单次探测；doctor 复用同一快照 |
| 4 | curl 存在但失败时**不回退 urllib**（兜底代码不可达） | `_fetch` 双通道：curl 失败保留错误并尝试 urllib |
| 5 | `config edit` 配置不存在 → 裸 `FileNotFoundError`（未分类 1） | 新增 `read_config_text()` 统一收口为配置错误(3)（verify/diff/start 同步） |
| 6 | `EDITOR="vim -u NONE"`（带参数）→ `FileNotFoundError` | `shlex.split`；引号不配对归用法错误(2) |
| 7 | 策略数值字段类型写错 → 裸 `ValueError` | 严格转换，指出字段名与实参 |
| 8 | `do_GET /healthz` 无 BrokenPipe 保护 → 探活断开的回溯污染插件 stderr | `_send_body` 统一捕获 |
| 9 | `write_state` 手写 tmp+replace（无 fsync/固定 tmp 名/夺回属主） | 统一到 `atomic_write` |
| 10 | "是否需要 `--allow-unsafe`"有 **4 份**实现 | 单点 `config.needs_unsafe_flag(text)`，四处调用统一 |
| 11 | `_place_binary` 临时名不带线程 id | 与 `switch_symlink` 对齐 |
| 12 | `restart` 的 `try/except AlreadyRunning: raise` 死代码 | 删除 |
| 13 | `doctor` 重复探 `frps -v`（2 次）与重复 `resolve_owner`（3 次） | run_doctor 单次探测后传参 |
| 14 | `parse_listen` 把 `bindPort <= 0` 当成"无监听" | **实测修正**：frp 回落默认 7000（启动日志 `listen on 127.0.0.1:7000`） |

### 17.9.4 便捷性与易用性：6 个新命令

| 命令 | 价值 | 实现基础 |
|------|------|---------|
| `frpsctl instances [--health]` | 多实例一行式概览（此前要手动 ls + 逐个 status） | 遍历 + `status()`，`--health` 才对运行中的实例做网络探测 |
| `frpsctl clients` | 在线客户端列表（`AdminClient.clients()` 早已实现却无 CLI 出口） | v2 `/api/v2/clients`，自动翻页取全量 |
| `frpsctl proxies [--type]` | 代理列表（name/user/port/phase/流量） | 新增 `V2Proxy`：嵌套 `spec/status` 形状在真机核对 |
| `frpsctl config list [--prefix] [--tree]` | 键名发现（此前只能翻文档或逐个 get 试） | `flatten_tree` + 同一套打码 |
| `frpsctl plugin service install` | 插件 systemd unit（README 要求 `Restart=always` 却让用户手写） | `frpsctl-plugin@.service` 模板 + 四项目体检 |
| `frpsctl service logs [-f]` | journald 集成（unit 级日志只有 journalctl 能看到） | `Systemd.journal_argv()` 纯函数 + CLI 终端接管 |

配套体验：`start`/`restart` 的等待期输出**逐轮进度**（终端原地刷新、非终端按行
限流；`--json` 不输出）；`install` 的 curl 不再捕获 stderr——终端上恢复下载
进度条（管道中自动静默）；`config set` 的 noop 在 `--json` 下也输出 JSON
（此前打印人读文本）。

### 17.9.5 端到端验收（真 frps 0.71.0 + 真 frpc）

```console
$ frpsctl init → start → instances → clients → proxies → config list → config set → stop
（全部通过；真实 frpc 连接后 clients/proxies 正确渲染出 runID/user/port/phase）
```

- 全量 **343 用例通过、0 skipped**（含 C1–C8 契约层与真 frpc 端到端）；
- ruff 全绿；覆盖率 **83%**；非契约层 318 条。

### 17.9.6 未做与边界（明确记账）

| 项 | 结论 |
|----|------|
| 策略 JSON 的 schema 校验（如 JSON Schema） | 不做：严格转换已覆盖"类型写错"这一真实高发面，上 schema 会引入新依赖与维护成本 |
| `clients`/`proxies` 的实时刷新（`--watch`） | 不做：`status --watch` 已覆盖"持续观察"的主场景；列表类命令的刷新留给 shell 循环 |
| `reject_log_burst` 的跨进程持久化 | 不做：限速是"保护审计文件不被刷爆"的进程内手段，重启后重置是合理的 |

---

## 17.10 第四轮回归 review（v0.2.1 发布前）

方法：**逐条审查本轮全部 diff + 对抗性实测**（多进程并发、真实编辑器 CAS、
真机分页与 unsafe 标志）。发现 **3 个真实缺陷**（全部是第四轮重构自己引入或
放大的），并在"方案完整性核对"中补齐 **2 处实施降级**（见 §17.10.5）——
新增 **11 条回归用例**（343 → 354），覆盖率维持 83%。

### 17.10.1 doctor 的版本诊断漂移（重构引入）

`_read_version` 改用带门槛的 `lc.binary_version()` 之后，`< 0.70.0` 抛出的
`UnsupportedVersion` 被吞成 None，最终报"无法读取版本 / 可能不是官方 frps"
——而真因是"版本太低"。实测（0.69.1 假件）复现。

修复：裸读版本（`read_binary_version`），门槛判断留在 `_check_binary` 的
展示逻辑；`_check_config` 对不达标版本跳过（不用低版本二进制做"权威校验"
得到误导性结论）。

### 17.10.2 unit 渲染的相对路径（既有缺陷，被 plugin service 放大）

两个安装器会把用户给的相对路径（`--root ./instances` / `--binary ./bin/frps`）
**原样写进 unit**：`ExecStart=bin/frps`、`WorkingDirectory=instances/t`、
`ReadWritePaths=instances/t`。systemd 要求绝对路径——安装成功，
`systemctl start` 才报 "Executable path is not absolute"。

实测复现（第五轮 review 的前置实验）后修复：渲染前统一 `.resolve()`
（`Systemd.install_template` 与 `PluginService.install_template`）。

### 17.10.3 对抗性实测清单（全部通过）

| 场景 | 方法 | 结果 |
|------|------|------|
| P0 并发（**多进程**，非线程） | 8 轮双进程并发 `config set` 不同键 | 两个键的改动全部保留（修复前必然丢失） |
| 编辑器 CAS | 慢编辑器（2s）+ 编辑期间并发 `config set` | set 成功保留；edit 退出码 3 拒绝草稿，文件无草稿痕迹 |
| exec tokenSource | 真 frps verify | trace 显示 `--allow-unsafe TokenSourceExec`，退出 0（单点判定无退化） |
| 分页上限 | 真 frp `page_size=200` | 服务端 **cap 到 50**；`_paged` 按 `total` 收敛（mock 测试锁定翻页行为） |
| `instances --health` | 真 frps 运行中 | 三层健康正确渲染 |
| 残留模式扫描 | grep（宽松转换 / 死代码 / 未 resolve 路径） | 无残留 |

### 17.10.4 未修（明确记账）

| 项 | 结论 |
|----|------|
| `_paged` 的 `max_pages=100`（2 万条上限） | 保留：服务端异常时的死循环防御；正常规模（代理数千）不会触及 |
| `clients` 的 `bool(item.get("online"))` | 保留：展示层字段来自 frp 的 JSON 布尔，非安全语义 |

### 17.10.5 方案完整性核对：补齐两处实施降级

对照第四轮方案逐项核对，发现两处**实施时被简化**、与方案原文有差距（不是
缺陷，但方案承诺未 100% 兑现），本轮补齐：

| 方案条目 | 实施时的降级 | 补齐方式 |
|---------|-------------|---------|
| P2-5 健康等待**进度** | 只有一行静态提示，没有逐轮进度 | `Lifecycle._await_health` 增加 `on_tick` 回调链（`start`/`restart`/systemd 路径透传）；`ui.progress` 终端原地刷新、非终端按秒限流按行；`--json` 不输出进度 |
| P2-4 `config list --tree` | 实现成了 `--prefix`（方案列的是 `--tree`） | 补 `--tree`（表头完整点分路径 + 叶子缩进，可与 `--prefix` 组合） |

核对结论：P0 全部、P1 全部 14 条 + 待核对项（`bindPort = 0` 实测修正）、
P2 全部 7 项、P3 全部 4 条——**至此 100% 落地**。

---

## 17.11 第五轮回归 review（v0.2.1 发布前，补齐项专项）

方法：对上一轮补齐的新代码（`on_tick` 回调链、`ui.progress`、`config list
--tree`）做**对抗性复现**（stderr 断开、回调抛异常、tty 残影），并复查全量
diff。发现 **3 个真实缺陷**——全部集中在"**展示层与业务层的边界**"——新增
**3 条回归用例**（354 → 357），覆盖率维持 83%。

### 17.11.1 展示层异常会拖垮启动（最严重）

`_await_health` 的进度回调没有异常隔离：tick 抛 `BrokenPipeError`（`start
2>&1 | head` 让 stderr 管道断开）时会冒泡进 `start()` 的 `except BaseException`，
触发 `_reap_after_failure`——**刚派生的 frps 被误杀**，而用户只看到 `head`
截断的一行输出。core 层与真实 CLI 端到端均复现。

修复：`_await_health` 用 `contextlib.suppress(Exception)` 包住 on_tick 调用
——进度回调是展示层，任何异常都不能影响启动。修复后同一场景实测：进程正常
托管（`state=RUNNING`）、退出码 12（健康未过的正确语义）。

### 17.11.2 stderr 辅助输出在管道断开时二次崩溃

`ui.note` / `ui.progress` / `ui.warn` / `ui.trace` 直接写 stderr：管道断开
时抛 `BrokenPipeError`。其中 `warn` 还挂在 `map_exceptions` 的**错误报告路径**
上——二次异常会变成 traceback。

修复：四个函数内部 `suppress(OSError)`——stderr 是辅助通道，写失败静默放弃。

### 17.11.3 tty 进度的残影

`ui.progress` 的终端分支只写 `\r` 不清行尾——文本从 "等待健康检查 10s" 变
"9s" 时留下残影。修复：补 `\033[K`（与 `status --watch` 的"仅 tty 写 ANSI"
原则一致）。

### 17.11.4 对抗性实测（复现 → 修复 → 验证）

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| tick 抛 BrokenPipe（core 层复现） | 异常冒泡 → `_reap_after_failure` | 被隔离，等待正常完成 |
| `start 2>&1 \| head -1`（真机 e2e，L2 fail） | 进程被误杀 | 进程 RUNNING、退出码 12 |
| stderr 断开时 note / progress / warn / trace | 全部抛 `BrokenPipeError` | 全部静默 |
| tty 进度文本变短 | 残影 | `\033[K` 清行尾 |

**共同模式**：这三个缺陷都不在业务逻辑里，而在"**展示层被授予了影响业务
流程的能力**"——进度、告警、诊断的失败都不能是启动失败。这与 §15.5.2 的
"降级必须可见"是一体两面：业务降级要可见，展示失败要无害。

---

## 17.12 发布前最终验收（v0.2.1）

最后一轮 review 的目标从"找缺陷"转为"证明可发布"：**复刻 CI 全套 + 验证
发布产物本身**（用户实际会安装的东西）。结论：**可发布**。

### 17.12.1 发布产物验证

| 项 | 方法 | 结果 |
|----|------|------|
| wheel 内容 | 解包核对模块清单 | 28 个 `.py` + `py.typed`，无缺失 |
| wheel 安装 | 干净 venv（Python 3.14）安装 | `frpsctl 0.2.1`；7 个新命令的 help 全部可用（无打包遗漏导致的 import 错误） |
| sdist 安装 | 干净 venv 安装源码包 | 同上 |
| 安装后冒烟 | `init` / `config list --tree` / `instances` | 通过 |
| 版本一致性 | 模拟 release workflow 的 tag 校验 | `v0.2.1` == `__version__`，一致 |
| wheel 自检 | 模拟 workflow 的 required 集合断言 | 通过（35 个条目） |

### 17.12.2 CI 全套本地复刻（与 ci.yml 逐步一致）

| 步骤 | 结果 |
|------|------|
| ruff | 全绿 |
| 非契约 + 覆盖率门禁（80%） | 332 passed，覆盖率 83.29% |
| 契约层（真 frps 0.71.0） | 21 passed |
| 故障注入层 | 23 passed |
| 插件契约层（真 frpc） | 4 passed |
| 端到端冒烟（12 步，含全部新命令） | 通过 |
| 无二进制降级路径 | 18 skipped + 3 passed（静态断言），无失败 |
| 0.70.0 下界契约矩阵 | 21 passed |

### 17.12.3 文档-代码交叉核对（自动脚本）

| 核对项 | 方法 | 结果 |
|--------|------|------|
| 命令 | 提取 README 全部 `frpsctl <cmd>` 引用 vs CLI 实际命令树（33 条路径） | 双向一致（无幽灵命令、无未文档化命令） |
| 环境变量 | 代码中 `FRPSCTL_*` 引用 vs README 环境变量表 | 双向一致 |
| 退出码 | README 退出码表 vs `errors.ExitCode` | 双向一致 |

### 17.12.4 工作区卫生

- 25 个修改文件全部在预期内；无 untracked 漏网（`.gitignore` 覆盖 `.coverage`、
  `dist/`、`.venv/` 等构建产物）；
- 版本引用零残留（`0.3.0` 字样已全部改为 `0.2.1`；历史段落里的 `v0.2.0`
  是真实发布记录，保留）。

---

## 18. Web 管理台

### 18.1 目标：控制面，而不只是仪表盘

frp 自带 dashboard 只有**数据面**（状态、客户端、代理、流量）且界面陈旧；
本工具的内置管理台补齐**控制面**——进程启停、配置编辑（预览 → 事务 → 自动
回滚）、日志——数据面则复用 v2 Admin API（§3.2）。

架构定位：`web/` 是与 `cli/` **平级的又一个前端**，只调 `core/`，不复制任何
业务逻辑；`core/` 不感知 HTTP。技术栈与插件服务同源：标准库
`ThreadingHTTPServer`，零新增依赖。

```
浏览器（单文件前端，内嵌 CSS/JS，零外部资源）
   │  JSON API + 会话 Cookie + CSRF 头
   ▼
frpsctl web serve（独立进程，默认只绑 127.0.0.1）
   ├── core.Lifecycle     → 状态 / start / stop / restart（含健康门控）
   ├── core.AdminClient   → clients / proxies / traffic（v2，自动翻页）
   ├── core.transaction   → 配置预览（plan_set_many + CAS）与应用（apply_sets）
   └── core.logs          → 日志 tail
```

### 18.2 API 契约

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` `/api/logout` | 口令登录（下发会话 Cookie + CSRF）/ 登出；**login 是唯一免认证入口** |
| GET | `/api/session` | 会话状态：归还 CSRF（刷新页面后内存丢失 → 用它恢复，否则所有变更都 403） |
| GET | `/api/status` | 进程状态 + 三层健康（含 L3 告警文本）+ dashboard 统计（统计不可得为 null） |
| GET | `/api/clients` `/api/proxies` | v2 数据（自动翻页；响应带 `total`，列表被翻页上限截断时 `truncated` 如实汇报） |
| GET | `/api/traffic` | 全部代理的**逐日汇总**（服务端聚合 + 并发查询 + 30s 缓存；超 50 个代理时带 `truncated` / `total`） |
| GET | `/api/traffic/{name}` | 单个代理的 7 天明细（前端展开某行时按需请求，不再随汇总全量下发） |
| GET | `/api/doctor` | 只读体检（与 CLI `doctor` 同一实现；以 Web 服务进程权限运行，结果可能与 root 不同） |
| GET | `/api/audit` | 插件审计视图：策略位置 + 统计 + 尾部记录；策略缺失/关闭时以 `available` / `enabled` / `reason` 如实说明 |
| GET | `/api/config` | 配置树（**打码值 + masked 标记**，原文永不下发） |
| GET | `/api/config/history` | 快照列表（只读 meta.json，**不读快照里的配置原文**） |
| GET | `/api/config/history/{steps}/diff` | 某快照 vs 当前配置的**打码 diff**（回滚前的"看差异"；与 `config diff --steps` 同一实现） |
| GET | `/api/logs?lines=` | 日志尾部（≤2000 行，路径解析复用 `core/logs`） |
| GET | `/favicon.ico` | 204（页面内嵌 data URI 图标；这条是给旧工具收尾的） |
| GET | `/metrics` | Prometheus 文本（v0.3.0；`--metrics` 开启后才存在）：实例状态 / 三层健康 / dashboard 统计；Basic auth（口令 = 管理台口令），5 秒服务端缓存 |
| GET | `/api/audit?scope=web` | Web 操作审计（v0.3.0）：登录与变更动作的来源 / 会话指纹 / 结果；`scope` 缺省为 `plugin`（兼容），非法值 400 |
| POST | `/api/config/preview` | 多键变更（`changes`）+ 删除键（`unsets`）→ 锁内取快照 + 打码 diff，登记 `preview_id`（TTL 10 分钟） |
| POST | `/api/config/apply` | 按 `preview_id` 应用（含删除）；**CAS**：预览后文件被改 → 400 拒绝而不是覆盖 |
| POST | `/api/actions/{start,stop,restart,rollback,prune}` | 与 CLI 同一套 core 入口（数值参数做范围校验，越界/布尔一律 400） |

错误映射：`FrpsctlError.exit_code` → HTTP（用法/配置 400、未运行/冲突 409、
权限 403、dashboard 不可达/健康未过 502）——响应只含 `message` 与 `hint`，
机密不进错误体（core 已保证）。

### 18.3 安全基线（比 frp 自带 dashboard 更严）

frp 的教训（user/password 双空 = 完全不鉴权，§3.3）是本项目全部安全决策的
来源；管理台能改配置、停服务，是比 dashboard 高得多的价值目标：

| 风险 | 对策 |
|------|------|
| 暴露到非回环 | 默认只绑 `127.0.0.1`；`--bind` 非回环必须显式 `--allow-non-loopback` |
| 无口令 | **不允许空口令**：自动生成（仅打印一次）或显式提供 |
| 会话劫持 | 256 位随机 token；Cookie `HttpOnly` + `SameSite=Strict`；TTL 8h（内存态） |
| CSRF | 一切变更请求要求 `X-CSRF-Token`（登录下发，仅存浏览器内存） |
| 口令爆破 | 来源级失败限速（60s/5 次）；冷却与错口令**响应完全一致** |
| 反代部署下的爆破误伤 | `--trusted-proxy`（默认**关**）：开启后按 `X-Forwarded-For` **最后一跳**限速；不开启时该头完全不被读取——伪造它既不能绕开限速、也不能制造新来源 |
| 时序侧信道 | `hmac.compare_digest` |
| 内存放大（失败来源 / 会话表） | 两张输入驱动的表都**有上限**：失败来源 1024（驱逐最早失败者）、会话 32（驱逐最早到期者） |
| 配置泄露 | 界面/API 只出打码值；欲看明文用 CLI `--reveal` |
| 前端供应链 | 单文件、零外部资源；CSP `default-src 'none'` + `connect-src 'self'`；`tests/test_web_frontend.py` 静态守卫（禁 innerHTML 家族、禁外部引用、JS 语法 `node --check`） |

### 18.4 systemd 托管

`frpsctl web service install` 渲染 `frpsctl-web@.service`：`Restart=on-failure`
（交互工具，正常停止不自启）、四项体检与插件一致、安装时生成 0600 口令文件
并移交服务用户——**口令明文绝不进 unit**（unit 是 0644，只引用文件路径）。

### 18.5 实现记录：下沉与复用

为 web 与 CLI 共用，三处逻辑下沉到 core（消除重复实现）：`parse_bind`
（bind 解析，插件服务同时受益）、`mask_value`（打码单点，`cli.ui.mask_secret`
委托）、`core/logs.py`（日志路径解析与 tail，`frpsctl log` 改用）。

### 18.6 实现期发现：`kick` 是一个从未工作过的功能

Web 端到端测试（真 frps + frpc）第一次调用"下线代理"就暴露：frp 的
`DELETE /api/proxies` 实现是 `ClearOfflineProxies()`，**只接受
`?status=offline`**（源码 `server/http/controller.go`），路由表里也没有任何
强制下线在线代理的 API。旧 `kick` 按"按 name 下线"实现该端点——真机**永远**
返回 400，而它从未被真机验证过（契约层是盲区）。

修正：`kick` 移除，`prune` 取代（正确语义：清理离线记录）；契约层新增 **C9**
锁定该事实（无参数 400 / `?status=offline` 200 / 客户端源码反向断言）。

### 18.7 未做与边界

| 项 | 结论 |
|----|------|
| WebSocket/SSE 实时推送 | 不做：5 秒轮询足够，且省掉长连接的生命周期管理 |
| 多实例切换 | 不做：一个 web 进程服务一个实例（`--instance`），多实例起多个进程 |
| HTTPS | 不做：默认回环明文即可；远程访问建议反向代理终结 TLS（文档已说明） |
| 配置的"全文编辑器" | 不做：表单式逐键编辑 + 预览 diff 更安全（原文不回传浏览器） |
| 强制下线在线代理 | 做不到：frp 没有该 API（§18.6）；停掉对端 frpc 是唯一途径 |
| 前端行为测试（DOM 级） | 不做**测试框架**：引入 jsdom/构建链会破坏"单文件零依赖"这个安全资产。语法与静态纪律由 `tests/test_web_frontend.py` 守卫（`node --check` + 禁 innerHTML/外部资源 + CSS 变量对齐），行为正确性由 HTTP 层全路由测试 + 真机冒烟覆盖；已知残余风险是"UI 逻辑分支只有人工点得到" |
| CSP 去 `'unsafe-inline'`（nonce 化） | 暂不做：单文件内联脚本需要服务端渲染时注入 nonce，收益是纵深防御（当前无任何注入点），成本是前端从"静态文件直发"变成"每请求改写"。已记账，等有真实动机再动 |
| 配置表单的结构化数组编辑器 | 暂不做：`allowPorts` 这类数组目前按 JSON 文本编辑（有 `parse_scalar` 兜底与预览 diff 兜底）；等实际使用中确认痛点再设计 |

### 18.8 发布前回归 review（v0.2.2；"第八轮"是当时的序号，与 §21 的第八轮迭代同名——以版本号为准）

对抗性复现（浏览器刷新场景、TOML datetime、全路由认证扫描、单代理故障注入）
发现 **4 个真实缺陷**，全部修复并补 **21 条测试**（410 → 431）：

| # | 缺陷 | 复现方式 | 修复 |
|---|------|---------|------|
| 1 | 配置含 TOML datetime 时 `json.dumps` 抛 `TypeError`——traceback 后连接被断开，整个配置页不可用 | 配置写 `expires_at = 2026-12-31T23:59:59` 后 `GET /api/config` | `_send_json` 统一 `default=str` 兜底（与 `cli.ui.emit_json` 一致） |
| 2 | 浏览器**刷新页面**后 Cookie 还在但前端内存的 CSRF 丢失——所有变更操作 403 且无恢复路径 | 有 Cookie 无 CSRF 的 POST | 新增 `GET /api/session`（有效会话归还 CSRF）；前端 boot 时恢复 |
| 3 | 趋势接口中单个代理查询失败会拖垮整张图（dashboard 半死不活时全 502） | mock 单代理抛 `AdminUnreachable` | 逐代理容错（记空 history），其余照常返回 |
| 4 | 预览条目无上限（已登录用户可持续 preview 堆内存）；前端 `localStorage` 在隐私模式下抛异常会中断流量采样 | 代码审查 + 存储注入 | 预览上限 32 条（丢最旧）；采样读/写 try 包裹 |

另新增**全路由认证扫描**（16 条参数化用例）：除 `POST /api/login` 外的每条 API
在未登录时必须 401——**未知路由同样返回 401**（不泄露路由存在性）。前端 425 行
JS 经 `node --check` 语法验证通过。

### 18.9 发布前最终验收（v0.2.2）

方法同 §17.12：**验证发布产物本身 + 复刻 CI 全套**。本轮补齐了 CI 对 Web 的
覆盖缺口（此前 CI 从不触碰 web），结论：**可发布**。

#### 发布产物验证

| 项 | 结果 |
|----|------|
| wheel / sdist | `frpsctl-0.2.2`（41 条目，含 web 模块与 26KB 单文件前端） |
| 干净安装（独立 venv） | `frpsctl 0.2.2`；**11/11 命令 help 全部可用**（含 `web`/`prune`） |
| 前端资源 | 安装后 `STATIC_INDEX` 可读（26389 字符） |
| tag 一致性（模拟 release workflow） | `v0.2.2` == `__version__` |
| wheel 自检 | 含 `py.typed` / `web/static/index.html` / 全部模块 |

#### CI 全套本地复刻

| 步骤 | 结果 |
|------|------|
| ruff / 覆盖率门禁（80%） | 全绿 / 82.44% |
| 非契约 | 405 passed |
| 契约层（0.71.0，含新增 C9/C10） | 26 passed |
| 故障注入层 | 23 passed |
| 插件契约层（真 frpc） | 4 passed |
| 端到端冒烟（CLI 12 步） | 通过 |
| **Web 冒烟（本轮新增的 CI 步骤）** | 通过（login / session / status / config / 静态页） |
| 无二进制降级路径 | 3 passed / 23 skipped |
| 0.70.0 下界契约矩阵 | 26 passed（C9/C10 在 0.70 上同样成立） |

#### 本轮 review 的修补

- **CI 覆盖缺口**：此前 CI 从不触碰 Web——新增 web 冒烟步骤（已在本地复刻验证
  可上 CI）；
- **契约补充**：C10 锁定 v2 traffic 端点的"无数据 = 404"语义（Web 前端容错的
  前提），0.70.0 上同样成立；`AdminClient.proxy_traffic` 的解析与名称转义补单测；
- 交叉核对：命令（39 条路径）/ 环境变量 / 退出码三向一致，无幽灵命令。

---

## 19. 第六轮全量迭代（v0.2.3：边界根治与六项新能力）

本轮以"全量通读 + 疑点实测"的方式复核全部源码、测试与两篇文档，聚焦便捷性、
易用性、稳定性、可靠性、功能性五轴。统计：修复 **5 条**真实缺陷（含 1 条可
造成不可逆动作），新增 **6 项能力**、**75 条测试**（435 → 510），覆盖率
82% → **85%**。

### 19.1 修复（根因清单）

| # | 问题 | 根因 | 根治方式 |
|---|------|------|---------|
| 1 | Web `stop` 负 timeout **立即 SIGKILL** | `_float` 宽松解析——CLI 侧 v0.2.0 已修，Web 是同一缺陷的镜像 | `_bounded_float` / `_bounded_int`：越界与非数值一律 400；回归断言"进程必须仍存活" |
| 2 | systemd + 损坏 state.json 误报 STOPPED | 损坏分支在所有权探测**之前**直接返回 | 状态判定以 owner 为先（systemd 下 state.json 本不参与判定）；并收口"检查之后才损坏"的 TOCTOU |
| 3 | 失败来源表无上限 + 反代误伤 | 来源固定为对端地址（反代下全部同源）；表只按窗口清理 | 1024 上限（驱逐最早失败者）+ `--trusted-proxy`（只信 XFF 最后一跳，默认关） |
| 4 | `rollback -1` 静默归一成"回滚 1 步" | `max(0, steps-1)` 吞掉非法输入 | Click `min=1` → 用法错误(2) |
| 5 | L2 失败详情被 L3 覆盖 | `detail` 单变量被后写的层覆盖 | 各层详情合并；L2 失败也渲染原因（控制面是恢复顺序上的第一层） |

### 19.2 新增能力

| 能力 | 动机 | 落点 |
|------|------|------|
| `config unset <key>` | 配置闭环缺"删除"（恢复默认只能手编） | `plan_unset` + `apply_unset`（同一锁与闭环） |
| `config set --dry-run` | CLI 无预览；Web 已有两段式 | `_dry_run_check`：跑全量校验（含 verify 与危险组合），零落盘零快照 |
| `--stdin` / `--prompt` | 敏感值进 argv = shell 历史 + `/proc/<pid>/cmdline` 泄露 | `_resolve_value_input`（三源互斥，空值拒绝） |
| `traffic [name]` | `proxy_traffic` 早已实现却没有 CLI 出口 | 逐日汇总 / 单代理明细；404=无数据（C10 语义精确化到客户端层） |
| `plugin user set/remove/list` | 策略是安全单点，手写 JSON 易错 | raw dict 编辑 + 同 `plugin check` 判据复验 + 未知键保留 |
| `web password show` + 历史回滚 UI | 口令遗忘无出路；Web 只能回滚一步 | `GET /api/config/history`（只读 meta.json）+ 前端卡片按 steps 回滚 |

### 19.3 工程

- `tests/test_docs.py`：README 命令 / 环境变量 / 退出码 ↔ 代码 ↔ 设计文档
  §7.2 的**双向一致性守卫**——把发布前的一次性核对变成 CI 常态；
- `core/` 反向依赖 `cli/` 的 8 处全部清除（`core/diagnostics.py` 承接诊断开关，
  打码统一 `config.mask_value`）；
- 死代码清理（零调用即删）、CI 加入 Python 3.14、Web/插件监听 backlog 64。

### 19.4 发布前回归 review

方法：对本轮全部 diff 做逐行审查 + 对抗性实测（unset 的数组表 / 子表 / 内联表 /
唯一键边界、traffic 以 mock dashboard 端到端对照 README 示例、布尔与空白参数、
并发锁不变量），并复查测试质量、workflow YAML 与工作区残留。发现并修复
**5 条**（2 条为真实缺陷），新增 **5 条**回归用例（505 → 510）：

| # | 问题 | 性质 | 修复 |
|---|------|------|------|
| 1 | `{"timeout": true}` 被当作 1.0 秒静默接受 | 新代码内部不一致（`_bounded_int` 已排除 bool） | `_bounded_float` 显式排除 bool |
| 2 | `plugin user set/remove` 的读-改-写无锁 | **真实缺陷**（两个并发调用互相覆盖，与第四轮 `config set` 并发丢失同形态） | 读写包进实例锁 + "锁内保存"不变量测试 |
| 3 | `parse_scalar` 对裸文本返回未 strip 的原文 | 既有 quirk（TOML 裸值不允许首尾空白） | 返回 strip 后文本；`'" x "'` 引号形式仍可保留空格 |
| 4 | 纯空白的位置参数绕过空值检查 | **真实缺陷**（配合 #3 会把空串写进配置） | 空检查改 `text.strip()` |
| 5 | `--prompt` 无输入时抛裸 `EOFError` | 异常契约（映射成"未分类错误(1)"） | 收口为用法错误(2) 并提示改用 `--stdin` |

其余为整理：死变量 `ui._MASK`、函数内 `import math`、`config.__all__` 补齐
（`plan_unset` / `parse_scalar`）、`status()` 的三元表达式改显式 if。对抗性
实测全部通过：unset 五类边界、traffic 端到端（人读输出与 README 示例逐字符
一致）、sdist 干净安装、两份 workflow 的 YAML 解析、diff 调试残留扫描。

### 19.5 发布前最终验收（v0.2.3）

方法同 §17.12 / §18.9：**验证发布产物本身 + 复刻 CI 全套**。结论：**可发布**。

| 项 | 结果 |
|----|------|
| wheel / sdist（`frpsctl-0.2.3`） | 干净安装通过；**47 条命令路径 help 失败 0**（全命令树遍历） |
| wheel 自检（release.yml 的 required 集合，本轮强化） | `py.typed` / `diagnostics.py` / web 前端资源齐全（42 条目） |
| tag 一致性 / 前端资源 | `v0.2.3` == `__version__`；静态页可读（26,987 字符） |
| CI 复刻：ruff / 覆盖率门禁（80%） | 全绿 / 84.88% |
| 全量（含契约 0.71 + 真 frpc 插件契约） | **510 passed / 0 skipped** |
| CLI 冒烟（12 步，含 dry-run / unset / traffic） | 通过 |
| Web 冒烟（含 history 的**真实快照**：set / unset 两条记录与 steps 语义） | 通过 |
| 无二进制降级 / 0.70.0 下界矩阵 | 3 passed / 23 skipped / 26 passed |

验收中顺手强化两处守卫：CI 的 Web 冒烟断言升级为"history 必须能读到真实快照"
（空列表不再能骗过它）；release.yml 的产物自检集合加入 `core/diagnostics.py`
与 `web/static/index.html`（丢前端 = 页面 500 而 CLI 测试全绿）。

---

## 20. 第七轮全量迭代（v0.2.4：Web 体验与前端守卫）

本轮聚焦 Web 管理台的优化与增强、易用性、可用性、美观性。方法仍是**完整通读
全部源码、测试与文档（无截断）后逐条核对**——这一轮暴露出一个此前从未被
正视的断层：

> **后端能力已经齐了，前端没有把它们全部用出来；而前端本身在 CI 里几乎零防护。**

证据都在代码里（不是推测）：`index.html` 的 `logScroll` 恒为 true（日志永远
自动滚底）、API 已下发打码值而前端丢弃、`state_corrupted` / `version_hint` /
`plugin_warning` 三个后端字段前端从未使用、traffic 超 50 代理静默截断（CLI
会告警）、`history` 接口长期不进 §18.2 API 表；而全部测试与冒烟只走 HTTP——
**一处 JS 语法错误会让整站白屏而 CI 全绿**。

### 20.1 修复（5 条，含一条测试盲区）

| # | 问题 | 根因 | 根治方式 |
|---|------|------|---------|
| 1 | 前端无任何自动化保护 | 测试与冒烟都只走 HTTP，从不执行 JS | `tests/test_web_frontend.py`：逐 `<script>` 块 `node --check` + 静态纪律守卫（禁 innerHTML 家族 / 禁外部资源 / CSS 变量双向对齐 / 亮色主题覆盖检查） |
| 2 | 配置页切换视图丢草稿 | 每次切回都 `loadConfig()` 清空 dirty | `configLoaded` 标志：已加载不重载；"重新加载（丢弃修改）"是显式动作且二次确认 |
| 3 | 日志永远自动滚底 | `logScroll` 定义后从未被改写，条件恒真 | 真实滚动监听维护"跟随/暂停"状态并显示在标题栏；滚到底部自动恢复 |
| 4 | 清理离线记录后不刷新 | `pruneOffline` 成功路径没有后续刷新 | 成功后 `refreshLists()` |
| 5 | 会话表无上限 | 惰性清理只处理过期；持有口令者可反复登录 | `MAX_SESSIONS = 32`，驱逐最早到期者（与失败来源表同一护栏纪律） |

### 20.2 新增（把后端已有能力交付到界面）

| 能力 | 落点 |
|------|------|
| 回滚前"查看差异" | `snapshot_diff` 下沉 core（CLI `config diff` 与 `GET /api/config/history/{steps}/diff` 唯一实现）；历史表逐行"查看差异"，回滚从盲操作变为可预览 |
| Web 删除键 | `plan_change_many` + `apply_sets(unsets=…)`：改与删合成**一次事务**（一份快照、一次重启）；同键冲突是用法错误 |
| Web 新增键 | 配置表单底部添加任意键（预览/校验/危险组合拦截与 CLI 同一套）——此前新增必须回 CLI |
| 单代理流量曲线 | 点击代理行展开该代理 7 天曲线（数据与 CLI `traffic <name>` 同源） |
| 操作进行中状态 | start/stop/restart/回滚/应用期间按钮禁用 + 状态徽章"操作中…"；in-flight 守卫防轮询叠加（start 最长 10 秒，此前零反馈） |
| 状态面板补齐 | 损坏横幅（含处置指引）、版本告警、L3 插件告警（`plugin_warning` 此前未下发）、systemd 行、流量截断提示 |
| 双主题 | `prefers-color-scheme` 自动 + 手动切换（localStorage）；未手动选择时跟随系统实时变化；图表颜色改由 CSS 变量控制 |
| 界面打磨 | diff 语法高亮、图表 hover 数值与合计、实时速率文本、表格数字右对齐、状态 tag、错误 toast 常驻、内嵌 favicon（`/favicon.ico` → 204）、窄屏菜单折叠 |

### 20.3 工程

- 设计文档 §18.2 补上 `/api/session`、`/api/config/history`、
  `/api/config/history/{steps}/diff`（长期 drift），并新增
  `tests/test_docs.py::TestApiDocConsistency`——**API 表 ↔ 路由双向核对**，
  与命令表守卫同一条纪律；
- 快照动作文案 `set many:` → `edit many:`（混合变更语义更准确，快照记账同步）；
- §18.7 边界新增三项明确记账：不做 DOM 级前端测试框架（残余风险如实标注）、
  CSP nonce 化暂缓的理由、结构化数组编辑器暂缓的理由。

### 20.4 统计与边界（明确记账）

统计：修复 **7 条**（实施期 5 条 + 发布前 review 2 条）、新增 8 项能力、
新增 **35 条**测试（510 → 545；非契约 515），覆盖率 85%，前端单文件
650 → 984 行。

| 项 | 说明 |
|----|------|
| UI 逻辑分支的人工覆盖 | 语法/纪律已自动化，但"点击某按钮后 DOM 变化"仍只有人工点得到——不引入 jsdom 是刻意的（见 §18.7） |
| `allowPorts` 等数组仍按 JSON 文本编辑 | `parse_scalar` 与预览 diff 兜底；结构化编辑器等真实痛点 |
| 前端单文件会继续变大（本轮 650 → 984 行） | 拆分需要构建链，与"零外部资源"冲突；在行数带来实际维护痛点前不拆 |

### 20.5 发布前回归 review

方法同历次（§17.10 / §17.11 / §19.4）：**全部 diff 逐行审查 + 对抗性实测 +
测试基建复查**。对抗面集中在两处新代码——core 的混合变更入口与 984 行的
单文件前端。

发现并修复 **2 条真实缺陷**：

| # | 问题 | 复现 | 修复 |
|---|------|------|------|
| 1 | `plan_change_many` 把字符串当列表**逐字符迭代**：`unsets="ab"` 静默删掉 `a` 与 `b` 两个键（与 `policy._strict_list` 同型的经典陷阱）；`changes="bindPort"` 则在解包处抛裸 `ValueError` | 对抗性实测复现（"删掉的键: ('a','b')"） | 输入归一化单点化到 `plan_change_many`（`_normalize_pairs` / `_normalize_keys`）：字符串 / 字典 / 生成器一律用法错误；`apply_sets` 改为直接透传原始参数——"字符串当列表"没有第二个藏身处 |
| 2 | `snapshot_diff` / `rollback_to` 的 `steps < 1` 被 `max(0, steps-1)` **静默归一**成"一步"——参数笔误变成另一个动作（v0.2.3 修过 CLI 侧，core 侧一直敞着） | 代码审查 + 边界实测 | 两个 core 入口都加 `steps >= 1` 防御（与 CLI 的 `min=1` 同一条纪律），各自新增回归用例 |

另有三条**加固**（非缺陷，但成本极低而静默失败风险真实）：

- **JS id 交叉守卫**进 `test_web_frontend.py`：`$("id")` 引用 ↔ HTML 定义
  **双向核对**（当前 47/47 完全一致）——`node --check` 抓不到"id 拼错 =
  运行时 null = 白屏"，这条守卫把该盲区关掉；
- **柱状图柱宽 clamp**（`Math.max(1.5, …)`）：点数异常变多时负宽度会静默不渲染；
- **前端脚本真实执行烟测**（一次性，node + 最小 DOM stub）：主脚本顶层与
  boot 异步路径完整执行无异常、`humanBytes` / `humanDuration` 的 7 组取值
  逐一对齐——覆盖语法检查抓不到的未定义变量 / TDZ 类错误。

残余风险如实记账：DOM 级行为（点击后的界面变化）仍只能人工验证（§18.7 的
不做项），而 id 缺失这类"用户可见的白屏风险"已被自动化覆盖。

---

## 21. 第八轮迭代（v0.2.5：完整卸载与在线一键安装）

这一轮的起点是一个用户提问："有没有完整卸载的功能"。通读后确认：**没有**。
卸载能力此前是分散的五段式——Python 包（pipx/pip/install.sh）、三个 systemd
`uninstall`、以及一个**没有任何命令覆盖**的数据目录（`bin/` 里的二进制 +
`instances/` 里的配置/state/快照/插件策略与审计/口令文件）。用户必须手工串联
`rm -rf ~/.local/share/frpsctl`，而它会静默连 token 与口令一起删掉、不可逆、
多实例场景没有任何护栏。

### 21.1 设计：作用域模型 + 三条安全原则

**作用域模型**：默认卸载**当前实例**；`--all` 覆盖实例根下的全部实例。
两个"保留"维度各自独立：`--keep-data` 保留实例数据（配置/快照/审计），
`--keep-bin` 保留共享二进制（多实例环境下还有别的实例要用）。

三条安全原则都是项目既有纪律在新场景的应用：

| 原则 | 落地 |
|------|------|
| **不确定就拒绝**（ADR-7） | 实例在运行 / 身份不明（FOREIGN）/ 状态文件损坏 / 多实例共用二进制却要求删它——一律拒绝并给出处置；运行中要 `--force` 显式授权 |
| **顺序安全** | 先停服务 → 再清 unit → 再删数据 → 最后删共享二进制；任何一步失败都不会留下"服务还在跑但数据已被删"的失控状态；且所有实例的**预检在任何破坏动作之前**完成（一个不允许，一个都不动） |
| **降级必须可见** | unit 清理需要 root，权限不足时收进 warnings 并给出可复制命令；`/var/log/frps` 与服务账户只提示不代删（可能另有用途） |

三个实现要点：

1. **共享 unit 模板的边界**：`frps@.service` / `frpsctl-plugin@.service` /
   `frpsctl-web@.service` 是所有实例共享的模板。多实例机器上卸载单个实例只能
   `disable --now`，**不能删模板**——为此给三个服务类拆出了 `disable()`
   入口（既有 `uninstall()` 保持原语义：停用并删模板）。
2. **`--json` 必须显式 `--yes`**：破坏性操作不做隐式确认，也不让确认提示污染
   stdout 的机器可读契约。
3. **锁与删除的相容性**：删实例目录时持有实例锁——并发 `config set` 不能与
   删除交错；删除持有中的 `.lock` 文件本身安全（锁按 fd 持有，unlink 不影响
   已有 flock）。

### 21.2 命令与验收

```bash
frpsctl uninstall              # 卸载当前实例（列出清单 → 确认）
frpsctl uninstall --all        # 卸载实例根下的全部实例
frpsctl uninstall --keep-data  # 保留数据，只停 unit 与删二进制
frpsctl uninstall --keep-bin   # 保留共享二进制
frpsctl uninstall --force      # 运行中的实例先停止再卸载
```

卸载部分新增 22 条测试：集成层覆盖作用域规则与全部安全拒绝
（运行中 / FOREIGN / 损坏 / 多实例 / 缺实例）、`--force` 的停止语义、
预检与停止之间的竞态收紧、unit 权限
不足的可见降级、共享模板假警告回归、`--keep-*` 组合与外部配置（`--config`）
提示；CLI 层覆盖确认门（取消不动分毫）、`--json` 约束与"运行中拒绝 = 退出码
11"的契约；单元层覆盖 `disable()` 只停用不删模板与 `is_enabled()` 三态。

### 21.3 发布前回归 review

方法同历次：全部 diff 逐行审查 + 对抗性实测（复现 → 修复 → 回归）+ 测试基建
复查。发现并修复 **2 条真实缺陷** + 1 处文案歧义：

| # | 问题 | 复现 | 修复 |
|---|------|------|------|
| 1 | `_clean_units` 的判定基于 `template_path.exists()`——而模板是**全部实例共享**的：实例从未用过 systemd 时也会尝试停用，产生"需要 root 才能停用 frps unit"的假警告（多实例场景实测复现） | 对抗性脚本（模板存在 + 本实例无 unit → 输出假警告） | 判定改为 `is_active() / is_enabled()`（本实例 unit 的真实状态，为三个服务类新增 `is_enabled()`）；`remove_templates` 与"本实例是否在用"两个维度分开判定 |
| 2 | **预检与停止之间的竞态窗口**：`_ensure_stopped` 无条件停止运行态，理由是"预检已授权"——实例可能在窗口里被并发启动，无 `--force` 的卸载会越权停掉一个刚起来的服务 | 代码审查（时序推理） | `_ensure_stopped` 接收 `force` 并在停止前**复核**状态：非 `--force` 遇运行态即中止（退出码 11），两条竞态回归用例（RUNNING / SYSTEMD_ACTIVE）固化 |
| 3 | "需要 root"的警告统一写"停用 unit"，而覆盖全部实例时的实际动作是"停用并删除模板"——两种动作的处置不同（一个要保模板、一个要删） | 代码审查 | 文案按 `remove_template` 分支区分 |

修复过程本身也暴露一个测试设计教训（已固化）：给 `Systemd.is_active` 做注入会
**顺带改变 `resolve_owner()` 的所有权判定**（实例被预检当成 systemd 托管而拒绝
卸载）——测试要注入的是"清理阶段需要停用"，应注入 `is_enabled`（所有权判定
不看它）。这条写进了测试的 docstring。

对抗性实测清单（全部通过）：重复卸载（第二次报"实例不存在"退出码 3）；只读
子目录（删除失败如实报错退出码 1、提示可重跑，修权限后重跑成功）；半删状态
（手工删掉 frps.toml 后卸载仍成功）；多实例共享模板无假警告（转正为回归用例）；
管道模式二次运行（重下载源码 + 复用 venv = 升级语义）；非法
`FRPSCTL_INSTALL_URL`（退出码 1、三条处置指引、不产生半截安装目录）。

---

### 21.4 在线一键安装（第九轮内容并入本版本发布）

起点是一个用户提问："README 能不能写一个一键在线安装命令？现在的安装脚本
支不支持？如果服务器没有 Python 该怎么办？" 核查结论：`install.sh` **不支持**
管道直跑（它用 `BASH_SOURCE` 定位源码，管道方式下既没有自身路径也没有源码），
也**没有任何"没有 Python"的出路**（脚本自身要跑 python3 建 venv）。本轮把
三件事一起解决。

### 21.5 在线安装：新增能力

| 能力 | 落点 |
|------|------|
| 管道直跑 | `curl … \| bash`：源码不在脚本旁边时下载 tarball 到 `<prefix>/share/frpsctl-src/src`；`FRPSCTL_INSTALL_REF` 固定 tag / 分支 / commit、`FRPSCTL_INSTALL_URL` 换镜像 |
| 无 Python 出路 | 检测失败时打印 **uv 一键命令**（uv 是静态二进制、自带 Python）；"缺 venv/ensurepip"在第一步给出 apt / uv 两条指引 |
| README 三路线 | uv 一键（推荐）→ pipx / pip → 源码（clone / 管道 / 手动），另立"服务器没有 Python 怎么办"一节 |

### 21.6 在线安装：实测暴露并修复的三处缺陷

| # | 问题 | 复现 | 修复 |
|---|------|------|------|
| 1 | `--uninstall --prefix DIR` 走安装路径 | `MODE` 与布局信息挤在同一变量，`--prefix` 把 `--uninstall` 覆盖成 custom——帮助文本里演示的组合实际是坏的 | MODE / LAYOUT 分离 |
| 2 | `--uninstall` 被 Python 与源码检查挡住 | 卸载分支写在检查之后 | 检查移入安装路径（卸载分支之后）；卸载不再需要 Python |
| 3 | 半残 venv 被"复用" | venv 创建中断留下"有 python 没有 pip"的目录，无条件复用让之后每次安装都在同一坑里失败 | 复用前验证 `import pip`；创建失败清理目录并给出指引 |

另有一处 URL 形态问题在实测中发现：`archive/refs/heads/<ref>.tar.gz` 对 tag 返回
404——改用 GitHub 通用形态 `archive/<ref>.tar.gz`（自动解析 tag / 分支 / commit）。

### 21.7 在线安装：验收（发布前实测）

全部真实执行：目录模式完整安装（含自检）；重复运行幂等（复用 venv）；
**管道模式真实下载 GitHub 源码并安装**；`FRPSCTL_INSTALL_REF=v0.2.4` 固定 tag；
`--uninstall` 在"系统 Python 缺 ensurepip"的环境成功；完全无 Python 时打印
uv 指引；`--uninstall --prefix` / `--uninstall --system` 参数组合。安装部分新增
6 条自动化测试（`bash -n`、两条真跑行为测试、承诺一致性、静态守卫）；
**本轮合计 28 条**（545 → 573；非契约 543）；文档守卫的环境变量核对同步扩展
覆盖 `install.sh`。

---

## 22. 第九轮迭代（v0.2.6：可见性与性能——CLI + Web）

**目标**：把 core 已经实现、但 CLI/Web 没有出口的能力全部呈现（审计 / 体检 /
enabled / 列表总数 / 清理条数），并把两条高频数据路径做快（日志反向读、流量
汇总并发 + 缓存）。不新增 core 语义。

**素材来源**：发布前的全量精读（CLI 2622 行、Web 2160 行、core 核心 2471 行
逐行；core 其余与 plugin、全部测试 9926 行、全部文档 5647 行由并行通道完整
读尽并交叉核对）。两条独立线索指向同一结论：**core 能力面领先于呈现面**，
而 Web 的流量接口与日志读取存在随规模恶化的真实性能缺陷。

### 22.1 新增：已实现能力的出口

| 能力（此前无出口） | CLI | Web |
|--------------------|-----|-----|
| 审计读取（`plugin/audit.py` 只写） | `plugin audit tail|stats`（复用反向读；`-f` 轮转重开；`--since 24h/7d/ISO/unix`） | 审计视图（统计 + 分布 + 最近 50 条；缺失/关闭/仅内存如实说明） |
| 策略级字段编辑 | `plugin config list|set`（严格复验 + 0600 原子写 + 未知字段拒绝） | —（策略编辑仍属 CLI 职责） |
| `is_enabled`（v0.2.5 为卸载实现） | 三个 `service status` 同时报告 enabled | — |
| `run_doctor` | `doctor --json` 增加 counts | 系统体检卡片（按钮触发） |
| 翻页信封的 `total` | `clients/proxies` 显示总数与截断告警 | 列表总数 |
| 清理条数 | `prune` 返回清理数（前后各数一次离线记录） | 同左（服务端计算） |
| `WebSettings.access_log` | `web serve --access-log` | — |
| 口令轮换 | `web password set [--stdin/--prompt]` | — |
| 服务生命周期（插件/Web 的 start/stop/restart 原语） | `plugin service start\|stop\|restart`、`web service start\|stop\|restart` | — |
| `access_log` 的 systemd 入口 | `plugin/web service install --access-log`（此前只有前台 `serve` 能开） | — |
| 口令生成器单点（三份 `token_urlsafe(18)` 收敛） | `generate_password` 委托 `core.systemd.generate_web_password` | — |
| 审计裁决耗时（`elapsed_ms`） | `plugin audit stats` 平均/最大耗时 | 审计视图同字段 |
| `client_id` | `proxies --json` 字段 | — |
| 实例名补全 | `--instance` shell 补全（只读、失败即空） | — |
| `lastStartAt` | `proxies` 启用时长列 + JSON 字段 | — |

### 22.2 性能：两条高频路径

**日志 tail 从"全量扫描"改为"尾部反向读"**（`core/logs.tail_lines`）。旧实现
用 `deque(maxlen)` 顺序读整个文件：100MB 日志每看一次读 100MB，而 Web 每 5 秒
刷新一次。新实现从文件尾按 64KiB 块回扫，数到 `lines + 1` 个换行即停——多要
一个换行使返回的字节串从**完整行的起点**开始（否则 `splitlines` 首元素是半行）。
读取量与"需要几行"相关；跨块边界与多字节字符由"解码发生在完整累积字节串上 +
起点之前的半行必然被丢弃"共同保证。CLI `log` 的初始 tail 与 `plugin audit
tail` 复用同一实现。

**流量汇总：汇总/明细分离 + 并发 + 服务端缓存**。旧 `/api/traffic` 每次响应
携带最多 50 个代理 × 7 天明细（前端只用它画汇总图），且串行查询——dashboard
稍慢就能把一次刷新拖到秒级。现在：

- `GET /api/traffic` 只回逐日汇总（服务端用与 CLI `traffic` **同一份**
  `aggregate_days` 聚合）；`GET /api/traffic/{name}` 提供单代理明细，前端展开
  某行才拉取（60 秒缓存）；
- 查询并发化（`fetch_histories`，8 线程；`httpx.Client` 线程安全）；
- 服务端 30 秒缓存（浏览器 5 秒轮询 × 逐日粒度数据 = 大量无谓重查）。

### 22.3 修复（全量精读发现）

| # | 缺陷 | 影响 | 修复 |
|---|------|------|------|
| 1 | `typer.Exit` 的退出码在直接调用路径被吞成 0：`map_exceptions` 兜底分支把 `Exit`（继承 `RuntimeError`、无 `format_message`）当"未分类错误" | `doctor` 有 ERROR 时"应当退出 1"在测试/库调用路径失效（真实 CLI 路径因 Click standalone 恰好正常）——正是"测试测不到真实契约"的又一实例 | 带 `exit_code` 的异常统一转 `SystemExit(code)`；测试基建接收 `standalone_mode=False` 的**返回值**（Click 把 `Exit` 作为返回值给出，忽略它等于放弃断言退出码） |
| 2 | 审计相对路径解析基准不一致（写入跟随 CWD / systemd WorkingDirectory） | 同一份策略手工运行与 systemd 托管写到两个地方，"审计消失" | 写入与读取统一相对**策略文件目录**（`core/auditlog.resolve_audit_path`）；默认文件名常量单源 |
| 3 | `_paged` 丢弃信封 `total`、上限用尽静默截断 | "共 N 条"无从说起；超限无言 | `PageResult(items, total)` + `truncated`；CLI/Web 如实展示 |
| 4 | `proxies --type` 拼错静默返回空表 | 用户以为"没有代理" | 用法错误(2) + 合法集合提示（ADR-7） |
| 5 | pyproject 的 `PT011` ignore 无效（`select` 未含 PT） | 死配置 | 清理 |
| 6 | `plugin audit tail -f` 与 `status --watch` 的 stdout 块缓冲 | 重定向/管道下流式输出攒满 4KB 才吐——"实时"失效 | 显式逐行/逐轮 flush（与 `log -f` 既有实现对齐）；BrokenPipeError 走 `map_exceptions` 优雅退出 |
| 7 | 前端 `looksBalanced` 用深度计数，`{ a = 1 ]` 被误判合法 | 输入校验形同虚设（动态守卫抓到） | 类型栈校验 + 纳入 `]`/`}` 开头碎片 |
| 8 | `plugin service stop` 的 fail-closed 告警在 `--json` 下丢失 | 脚本拿不到关键告警 | 告警无条件进 stderr |
| 9 | v0.2.4 的前端 DOM stub 试验未固化（当时为手工验证） | 前端运行时行为零自动守卫 | 新增 `TestFrontendPureFunctionsRuntime`（node 执行抽取源码）+ 两条真子进程流式实时性断言 |

### 22.4 测试与验收

- **新增 71 条**（573 → 644；非契约 543 → 614）：日志反向读（跨块边界 / 多字节
  / 无尾换行 / I/O 量上界断言）、分页 total 与截断、清理计数、审计读取全谱
  （路径解析 / 统计 / 窗口 / 用户表上限 / 坏行）、策略级编辑（含 `audit.path
  null` 与严格拒绝）、口令轮换、`install` 全选项透传、`/api/doctor`、`/api/audit`、
  `/api/traffic/{name}`、traffic 服务端缓存（假时钟）、动作成功路径
  （restart / rollback——此前只有拒绝路径）、日志参数边界、未规范化路径、
  `web serve` 全链路（真进程 → 登录 → SIGTERM 退出码 0）。
- 文档一致性守卫同步扩展：README 命令表、§7.2 命令表（含审计/策略编辑/口令
  轮换）、§18.2 API 表（doctor / audit / traffic 明细）。

---

## 23. 第十轮迭代（v0.3.0：内核重构与传输层升级）

v0.2.x 六轮迭代把功能面铺满（61 条命令路径、8 组 API、644 条测试）之后，最大的
问题不再是"缺什么"，而是**三个巨文件与四处双份实现**。v0.3.0 是一次 minor 级
内核重构：用户可见契约零变化（命令路径 / 选项 / 退出码 / JSON 字段 / HTTP
状态码逐项锁定），但为后续所有功能迭代移除结构性阻力。

### 23.1 CLI 内核：拆包 + 装配点 + 表示层单点

| 项 | 之前 | 之后 |
|----|------|------|
| 命令实现 | `cli/__init__.py` 单文件 3253 行 | `cli/app.py`（Typer 组装 + 全局回调）+ `cli/runtime.py`（共享依赖装配）+ `cli/commands/{install,lifecycle,config,service,observe,plugin,web}.py`（按域拆分，最大 892 行）|
| 兼容 | — | `cli/__init__.py` 保留 shim：re-export 全部历史符号（一个版本周期后收敛），旧 import 与旧 patch 目标平滑过渡 |
| 共享依赖 | 各命令函数内直接构造（`_ctx` / `_lifecycle` / `_admin` / `_policy_path` / `_frpsctl_executable` / 值输入通道 / 跟随工具） | 全部集中 `cli/runtime.py`，命令模块以**模块对象**访问（`runtime._ctx(ctx)`）——测试替换 runtime 符号对所有命令一致生效，不会因 `from` 导入复制引用而静默失效 |
| 响应形状 | CLI 与 Web 各拼一遍（`_status_payload` / `web.api.status_payload` / 两处 doctor / 两处 health / 两处 start） | `frpsctl/report.py`（与 core/cli/web 平级的纯表示层）：status / start / health / doctor / audit 单点生成；CLI 与 Web 的差异用参数表达（`include_paths` / `dashboard`） |
| 命令契约 | 无显式守卫 | `tests/test_contract_snapshot.py` + `tests/snapshots/cli_commands.json`：**56 条路径 / 180 个参数**逐字节恒等（重构期间任何漂移立刻红） |

**为什么"模块对象访问"是根治**：`from x import f` 复制引用，`patch("x.f")` 不会
影响已经拿到引用的调用点——这正是 v0.2.6 测试被 CI 抓到"注入从未生效"的同一
类缺陷。装配点集中 + 模块对象调用把"测试替换依赖"变成一个稳定契约。

### 23.2 Web 传输层：条件请求 + 分层缓存 + 有界并发

| 项 | 内容 |
|----|------|
| ETag/304 | 已认证 GET 带内容 ETag + `Cache-Control: no-cache`（浏览器自动 `If-None-Match`，未变即 304 零 body）；写响应 / 错误 / 登录 / `/api/session` 一律 `no-store` |
| 服务端缓存 | `web/cache.py` 的 `TTLCache`：traffic 30s / clients·proxies 2s / logs 1s（按行数分键）；**写操作显式失效**（action / config apply 前清空）——浏览器 5 秒轮询在空转时的 dashboard 查询数降为 0 |
| 有界并发 | `_BoundedThreadingHTTPServer`：worker 上限（默认 32）用尽时 503 + 断开（收口"管理台不限制并发连接数"的旧边界） |
| CSP nonce | `<script>` / `<style>` 每请求 nonce、去掉 `'unsafe-inline'`、内联 style 属性全部改工具类；追加 `base-uri 'none'; form-action 'none'; frame-ancestors 'none'` |
| `/metrics` | Prometheus 文本（实例状态 / 三层健康 / dashboard 统计），Basic auth（用户名任意、口令 = 管理台口令），5 秒缓存，默认关闭（`--metrics` 开启，可写入 unit） |
| 操作审计 | `core/web_audit.py`（JSONL，同步写但失败不阻断）：变更动作与登录留痕（来源 / 会话指纹 / 结果 / 标量参数）；失败登录审计限速（每来源每分钟一条）；读侧 `GET /api/audit?scope=web` 与 `web audit tail|stats`；前端审计视图双 tab |

### 23.3 优化增强

- **owner 探测缓存**：`Lifecycle.resolve_owner` 实例级 2s TTL + `state.json`
  `(mtime_ns, size)` 戳失效；Web 复用进程级 `Lifecycle`（`WebContext.lifecycle()`）
  ——watch / 轮询下 systemctl 子进程数大幅下降，而变更动作（start/stop）仍
  立刻可见（戳变化或显式清缓存）。
- **`config apply`**：CLI 多键一次事务（`--set` 可重复 / `--unset` / `--dry-run`），
  复用 `apply_sets`——与 Web 配置表单同语义（一份快照、一次重启）。
- **审计轮转**：写侧（插件后台线程 / Web 同步写）超过阈值轮转 `path` → `.1`
  → `.2`；读侧 tail/stats 跨文件合并；策略新增 `audit.max_mb`（0 = 不轮转）。
- **前端逻辑分层**：审计渲染的数据变换提为纯函数（`pluginAuditStatsItems` /
  `pluginAuditNotes` / `webAuditStatsItems` / `webAuditNotes` / `webAuditRows`），
  node 动态断言覆盖扩至 6 个函数。
- **补全与映射**：`config get/set/unset` 配置键补全、`plugin user remove`
  用户名补全、`plugin config set` 字段补全（全部零副作用）；`ExitCode → HTTP`
  改为表驱动单点（新增退出码忘记补映射会落到 500 而非被静默归类）。
- **`capabilities`**：能力清单从代码派生（命令树 / `ExitCode` / `env.py`），
  供脚本与文档消费。
- **OIDC**：`auth.method = "oidc"` 的配置完整性校验（issuer/audience 必填）
  与 doctor 提示（缺项 ERROR、残留 token WARN）；协议本身仍由 frps 实现。
- **`--binary` 门槛**：补齐"第二条入口"的测试（§8.4 要求 install 与
  `--binary` 两条入口都做版本检查）。

### 23.4 代码即文档

- `frpsctl/docs.py`：README 的三张表与代码**双向对账**——命令（`capabilities`
  派生的每条命令必须能在 README 找到）、退出码（README 表与 `ExitCode` 枚举
  完全一致）、环境变量（`env.ENV_VARS` 的每个变量必须出现；README 表里出现的
  每个变量必须有定义）。CI 由 `test_docs.py` 调用；本地可
  `python -m frpsctl.docs check`。
- `frpsctl/env.py`：14 个环境变量的单点定义（此前散落在 8 个模块）。
- 历史漂移修复包（13 处）：测试数字、§13.1 契约清单（补 C9/C10）、§14
  里程碑回填标注、§16.2 旧矩阵"已被 §16.2.1 取代"标注、§12.1 引用修正、
  §11.3 与 §11.2.4 的"并发连接上限"矛盾标注、§17.1 模块计数更新、轮次编号
  注记、ADR-4 历史理由标注、§13 分层表述、README 版本示例等。

### 23.5 量化验证（实测计数，非估算）

| 场景 | 无缓存对照 | 当前实现 | 结果 |
|------|-----------|---------|------|
| 浏览器 5 秒轮询 30 秒（clients + proxies + traffic，50 代理） | 318 次 dashboard 请求 | **57 次** | 降幅 **82%**（方案目标 ≥80%） |
| 同一聚合查询的第二次响应（ETag 命中） | 完整 JSON（含 50+ 代理逐日明细可达数百 KB） | **304，0 字节 body** | 响应体归零 |
| `resolve_owner` 连续 3 次（TTL 内） | 3 次 systemctl 探测 | **1 次** | 子进程降 2/3 |

**量化验证抓出的设计错误（已修）**：列表缓存最初定 2 秒，而浏览器轮询间隔
是 5 秒——TTL **小于**轮询间隔时命中率≈0，等于没有缓存（实测确认）。改为
6 秒后轮询几乎每轮命中，而"改完立刻可见"由写操作显式失效保证。

### 23.6 实施调整（与 §23.1–23.4 计划的差异及理由）

| 计划项 | 实施结果 | 理由 |
|--------|---------|------|
| `cli/render.py` 的 Output 对象（可注入 stdout/stderr） | **未做**，保留 `ui.py` | 测试基建已通过替换 `sys.stdout` 完成注入；额外抽象没有第二个使用者，会成为死代码（本项目 0.2.6 刚清理过死代码）。等出现真实的多输出目标需求再引入 |
| 48 处 `--json` 选项样板由装饰器自动注入 | **未做**，保留手写选项 | Typer 装饰器魔术会改变帮助文本生成路径，风险中而收益只是删重复声明；命令面快照已把选项契约锁死，样板不构成维护风险 |
| P0-4 文档回路用"生成标记包裹 + 生成器改写" | 改为**双向对账检查**（`docs.py`） | 生成标记会把 README 的富格式（分类、典型触发）压成机器表；对账在保留人工表达的同时同样保证"漂移=CI 红" |
| P1-3 审计轮转"按天/大小" | 两者都做（按天为缺口，补齐） | `audit.max_days` 与 `audit.max_mb` 两维独立 |
| P1-4 前端 `buildClientRows` / `buildProxyRows` | 补齐（`clientRows` / `proxyRows` + node 断言） | 漏项，已补 |
| 验收标准"JSON 形状快照" | 补齐为 `tests/test_report.py` 的**完整键集基线** | 形状由 `report.py` 单点生成——在单点锁键集，CLI 与 Web 同时受保护 |

### 23.7 测试与验收

- **新增 91 条**（644 → 735；非契约 614 → 705）：契约快照（7）、CLI 盲区补齐
  （service install/uninstall、plugin serve e2e、log -f 实时性、rollback 成功
  路径、init --force、uninstall --all、`--binary` 门槛）、ETag/304 语义与缓存
  失效、操作审计（成功/失败/登录/限速/scope）、CSP nonce、/metrics（404/401/
  200）、有界并发 503、审计轮转与跨文件读取、config apply、补全、capabilities、
  OIDC、前端纯函数扩展。
- 重构期间的"零变化"由三张网共同保证：命令面快照（参数逐字节）、既有 644 条
  行为断言（全部保留）、报告层统一后的字段级断言。

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
| `bindPort` | int | `7000` | 控制端口（**实测**：写 `0` 会回落默认 7000 并照常监听，不是"禁用"） |
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

# 4b) 鉴权开关是"任一非空"（§3.3 的实测表）
#     起三个 frps 分别配 user=admin(无口令) / 都不配 / admin+secret，
#     然后观察无凭据请求与"空口令"请求的差异：
#       curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<dash>/api/v2/clients
#       curl -s -o /dev/null -w '%{http_code}\n' -u admin: http://127.0.0.1:<dash>/api/v2/clients
#     结论：第一行 401 / 第二行 200 → 启用鉴权但空口令是合法口令。
#     完整脚本见 tests/test_facts.py::TestC8PasswordlessAuth

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

1. `-v` / `--version` 是**持久标志**（`PersistentFlags`），`verify` 子命令同样认识它们——所以 §8.4 的 `config_flags()` 把标志放在子命令**之前**，两种位置虽然都能工作，但只沿用一种写法以免未来踩 Cobra 的解析顺序坑。
2. `--allow-unsafe` 是 `StringSlice`，值为 `TokenSourceExec`（对应 `security.ServerUnsafeFeatures`），**不是布尔开关**。
3. 版本矩阵会随 frp 发版变化，因此**它是 CI 断言（§13.1 C7），不是一次性结论**。本文档记录的是 v0.71.0 时点的观测值。
