# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.4] - 2026-09-17

Web 管理台的体验迭代：把后端已有的能力全部交付到界面，并给单文件前端补上
自动化守卫。修复 5 条真实缺陷（含一条"白屏而 CI 全绿"的测试盲区）。

### 修复

- **前端在 CI 里几乎零防护**：所有测试与冒烟都只走 HTTP、不执行 JS——一处
  语法错误会让整站白屏而 CI 全绿。新增 `tests/test_web_frontend.py`：逐
  `<script>` 块 `node --check`（无 node 时 skip）+ 静态纪律守卫（禁 innerHTML
  家族、禁外部资源引用、CSS 变量双向对齐、亮色主题必须覆盖全部颜色变量）。
- **配置页切换视图会丢失未保存的修改**：每次切回配置页都重新拉取并清空草稿。
  现在已加载过就不重载，"重新加载（丢弃修改）"是显式动作（有未保存修改时
  二次确认）。
- **日志面板永远自动滚底**：`logScroll` 恒为 true，用户向上翻日志会被 5 秒
  刷新拽回底部。现在按真实滚动位置维护"跟随/暂停"（显示在标题栏，回到底部
  自动恢复）。
- **清理离线记录后不刷新列表**：用户看到条目还在，以为操作没生效。
- **认证会话表无上限**：持有口令的调用方反复登录可持续推高内存——与失败
  来源表同类的"输入驱动的表必须有界"，现在上限 32（驱逐最早到期者）。
- 发布前回归 review 追加修复：**`plan_change_many` 把字符串当列表逐字符迭代**
  （`unsets="ab"` 会静默删掉 `a` 与 `b` 两个键；对抗性实测复现，现为用法错误，
  输入归一化单点化在 core 并覆盖 `changes`/`unsets` 两种形状）；`snapshot_diff`
  与 `rollback_to` 的 `steps < 1` 曾被 `max(0, steps-1)` 静默归一成"一步"
  （core 入口补防御，与 CLI 的 min=1 同一条纪律）；代理柱状图的柱宽 clamp
  （防御"负宽度静默不渲染"）。

### 新增

- **回滚前"查看差异"**：新接口 `GET /api/config/history/{steps}/diff`（打码，
  与 CLI `config diff --steps` 共用 core 的 `snapshot_diff`）——回滚从盲操作
  变为可预览；历史表每行都有"查看差异"按钮。
- **Web 配置删除键**：配置表单每行"删除"按钮 → 与修改合并成一次事务
  （`plan_change_many` + `apply_sets(unsets=…)`：一份快照、一次重启）；
  "删除不存在的键"照旧是配置错误。
- **Web 配置新增键**：此前只能改已有键，新增必须回到 CLI；现在表单底部可
  添加任意键（预览/校验/危险组合拦截与 CLI 同一套）。
- **单代理流量曲线**：点击代理行展开该代理的 7 天曲线（数据源与 CLI
  `traffic <name>` 相同）。
- **操作进行中状态**：启动/重启/停止/回滚/应用期间按钮禁用、状态徽章显示
  "操作中…"（启动最长等 10 秒，此前完全无反馈）；轮询与手动刷新不会叠加。
- **状态面板补齐**：state.json 损坏红色横幅（含处置指引）、0.70.x 版本告警、
  L3 插件告警（`plugin_warning` 此前没有下发）、systemd unit 行、流量超 50
  个代理时"已截断"提示（CLI 有、Web 曾静默）。
- **双主题**：`prefers-color-scheme` 自动 + 手动切换（本地持久化）；图表
  颜色改由 CSS 变量控制，切换即时生效。
- 界面打磨：diff 语法高亮（+绿/−红）、图表 hover 数值与合计、实时速率文本、
  表格数字右对齐与状态 tag、错误 toast 常驻（手动关闭）、日志"跟随/暂停"、
  内嵌 favicon（`/favicon.ico` 收尾返回 204）、窄屏"菜单"折叠。

### 工程

- `snapshot_diff` 下沉 core（CLI `config diff` 与 Web 差异接口的唯一实现）；
  `plan_change_many` 统一混合变更；快照动作文案 `set many:` → `edit many:`。
- 设计文档 §18.2 的 API 表补上 `/api/session`、`/api/config/history`、
  `/api/config/history/{steps}/diff`（长期 drift），并把"API 表 ↔ 路由"
  双向核对加入 `tests/test_docs.py`。
- 新增 35 条测试（510 → 545；非契约 515），覆盖率 85%；前端单文件 650 → 984 行。
  其中前端守卫含 **JS 对 id 的引用与 HTML 定义的双向核对**——`node --check`
  抓不到的"id 拼错 = 白屏"由此变成 CI 断言。

## [0.2.3] - 2026-09-17

全量通读后的根治性迭代：Web 参数边界、systemd 交叉状态、限速来源模型、分层
倒置，以及六项新能力（config unset / traffic / plugin user / web password /
配置历史回滚 / dry-run 与 stdin 输入通道）。

### 修复

- **Web 动作接口的负参数会立即 SIGKILL**：`/api/actions/stop` 的 `timeout` 走
  宽松解析，`-1` 让等待循环一次都不执行、直接升级 SIGKILL——与 CLI 侧
  `stop --timeout -1`（v0.2.0 修复）是同一缺陷的镜像。现在全部动作参数做范围
  校验（越界 / 非数值 → 400），并有一条测试断言"进程必须仍存活"。
- **`status` 在 systemd 托管 + 损坏 state.json 时误报 STOPPED**：损坏分支完全
  不探测 unit，而 systemd 下 state.json 本就不参与判定（direct → systemd 迁移
  的残留即可触发）。现在 systemd 实例照常报告；非 systemd 仍报"不可判定"。
  同时给 status 的"永不异常"承诺补上 TOCTOU 收口（损坏检查与读取之间的竞态）。
- **认证失败限速的来源模型**：来源表加上限（1024，防慢速内存放大）；反代部署
  下所有请求同源、攻击者 5 次失败即可连带锁住管理员 → 新增
  `web serve --trusted-proxy`（默认关；开启后按 `X-Forwarded-For` 最后一跳
  限速，`web service install` 亦可写入 unit）。
- **`config rollback -1` / `config diff --steps 0` 静默归一**：步数必须 ≥ 1
  （用法错误 2），参数笔误不再变成另一个动作。
- 健康 detail 在 L2 与 L3 同时失败时只显示 L3 的信息 → 现在汇总两层，且
  L2 失败时也会渲染失败原因（控制面是恢复顺序上的第一层）。
- 发布前回归 review 追加修复：Web 动作参数的**布尔值**（`{"timeout": true}`
  曾被当作 1.0 秒静默接受）；`plugin user set/remove` 的读-改-写**没有锁**
  （两个并发调用互相覆盖——与第四轮 `config set` 并发丢失同形态，现与 config
  写共用实例锁）；**首尾空白**（`parse_scalar` 此前保留未 strip 的原文，现按
  TOML 裸值语义去空白；纯空白的值一律拒绝）；`--prompt` 在无输入时抛裸
  `EOFError`（现为用法错误并提示改用 `--stdin`）。

### 新增

- `frpsctl config unset <key>`：删键回落 frp 默认值，与 `config set` 同一事务
  闭环（校验 → 快照 → 重启 → 失败自动回滚）；键不存在报配置错误(3)。
- `frpsctl config set --dry-run`：跑完全部真实校验（含 `frps verify` 与危险组合
  拦截）但零落盘、零快照——CLI 版的"预览"；`config unset` 同样支持。
- `config set --stdin / --prompt`：敏感值不再必须走 argv（shell 历史与
  `/proc/<pid>/cmdline`）；三种值来源互斥。
- `frpsctl traffic [name]`：近 7 天流量历史（无参 = 全部代理逐日汇总；离线
  代理 404 = 无数据，单代理失败不拖垮整体）。
- `frpsctl plugin user set|remove|list`：策略的结构化编辑（写入前同
  `plugin check` 判据复验、0600 原子写、未知键原样保留）。
- `frpsctl web password show`：读回 `web service install` 生成的口令（权限
  过宽时向 stderr 告警）。
- Web 管理台的"历史与回滚"卡片：列出快照（时间 / 动作 / 步数）并可回滚到
  任意一份；新接口 `GET /api/config/history` 只读 meta.json，不下发快照原文。

### 工程

- **分层倒置根治**：`core/` 不再反向 import `cli/`（此前 8 处
  `cli.ui.trace` / `mask_secret`）——诊断开关下沉为 `core/diagnostics.py`，
  打码统一走 `config.mask_value`。
- 死代码清理（零调用即删）：`lock.held_locks`、`plugin.server.serve` /
  `wait_ready`、`WebSettings.password`（构造后从未被读，容易误以为生效）。
- 新增 `tests/test_docs.py`：README 命令 / 环境变量 / 退出码与设计文档命令面的
  **双向一致性守卫**——文档 drift 从此在 CI 里直接暴露；同步刷新设计文档
  §1.3 / §5 / §7.2。
- CI 矩阵加入 Python 3.14；Web 与插件的监听 backlog 设为 64（过载行为可预期）。
- 新增 75 条测试（435 → 510；非契约 480），覆盖率 82% → **85%**。

## [0.2.2] - 2026-09-17

Web 管理台（内置界面）与 `kick` 语义修正。

### 新增

- `frpsctl web serve`：内置 Web 管理台——仪表盘（状态 / 三层健康 / 7 天流量
  柱状图 / 会话内实时曲线）、客户端与代理列表、日志面板、进程启停、配置编辑
  （预览打码 diff → 应用 → 失败自动回滚 → 一键回滚）。单文件前端、零外部
  资源（CSP `default-src 'none'`），默认只绑回环。
- `frpsctl web service install|uninstall|status`：Web 管理台的 systemd 集成
  （生成 0600 口令文件并移交服务用户；口令明文不进 unit）。
- `frpsctl prune`：清理 dashboard 统计里的离线代理记录。
- 配置编辑的多键事务 `apply_sets` / `plan_set_many`：一次快照、一次重启，
  带 CAS（预览之后文件被改 → 拒绝而不是覆盖）。
- 契约层新增 **C9**（代理写 API 的真实语义）与 **C10**（traffic 端点的"无数据
  = 404"语义，Web 容错的前提），0.70.0 上同样成立。

### 修复

- **修正 `kick` 的语义错误**：frp 的 `DELETE /api/proxies` 实际是
  `ClearOfflineProxies()`（只接受 `?status=offline`），**不存在**强制下线在线
  代理的 API。原 `kick` 按"按 name 下线"实现该端点，真机永远返回 400——一个
  从未工作过的功能（Web 端到端测试暴露）。已由 `prune` 取代。

### 工程

- 新增 78 条测试（357 → 435），覆盖率 82%。含 Web 层的全路由认证扫描（16 条）、
  浏览器刷新恢复、TOML datetime 序列化、单代理故障注入等回归用例。
- core 下沉三处共用逻辑（消除重复实现）：`parse_bind`（插件服务同时受益）、
  `mask_value`（打码单点）、`core/logs.py`（`frpsctl log` 与 web 共用）。
- CI 新增 **Web 管理台冒烟**步骤（此前 CI 从不触碰 web）：登录 → 会话恢复 →
  状态 → 配置 → 静态页，全链路验证。

## [0.2.1] - 2026-09-17

第四轮全量迭代：并发根治、策略 fail-open 修复与便捷性命令。

### 新增

- `frpsctl instances [--health]`：多实例一行式概览（owner / 状态 / pid / 版本 / 健康）。
- `frpsctl clients` / `frpsctl proxies [--type]`：v2 Admin API 的客户端与代理列表
  （自动翻页取全量，`--json` 可管道）。
- `frpsctl config list [--prefix] [--tree]`：列出全部配置键（值自动打码；
  `--tree` 按表分组缩进展示）。
- `frpsctl plugin service install|uninstall|status`：插件服务的 systemd unit
  （`Restart=always`，安装前四项体检：账户 / frpsctl 可达且不在家目录 / 策略文件 / 回环）。
- `frpsctl service logs [-f] [-n]`：journald 集成（unit 级日志）。
- `start`/`restart` 等待健康检查时输出**逐轮进度**（终端原地刷新，非终端按行
  限流；`--json` 不输出进度）；`install` 在终端上恢复 curl 下载进度条
  （管道中自动静默）。

### 修复

- **并发（P0）**：`config set` 的候选生成（plan）移入实例锁内——两个并发变更不再
  互相静默覆盖（实测复现：后写入者覆盖前者，两边都报成功）；`config edit` 引入
  锁内 CAS（编辑期间文件被并发修改 → 拒绝草稿并提示重新编辑）；
  `config rollback` / `config diff` 的读取同样入锁。
- **安全**：策略 JSON 的宽松转换曾是 fail-open——`"allow_unknown_user": "false"`
  （字符串）被 `bool()` 判成 **True**，等于打开鉴权后门；`allow_random_port` 同理。
  现改为严格类型（类型错误报 3 并指出字段名），并修掉 `"audit": "false"` 的裸
  `AttributeError`。
- `--health-timeout` 对 systemd 实例生效（此前硬编码 10s，参数被静默忽略）。
- `same_config_active` 扫描全部 active service：自建 unit 指向同一配置不再漏检
  （direct 与 systemd 双起防护补洞）。
- `status` 单次探测所有权（systemd 下从 4 个子进程降为 2 个，消除两次探测间的 TOCTOU）。
- 下载在 curl 失败时回退 urllib（此前兜底代码不可达）；curl 进度不再被捕获。
- `config edit` / `verify` / `config diff` 在配置缺失时给出配置错误(3)，而不是
  未分类错误(1)；`EDITOR="vim -u NONE"` 这类带参数写法可用（引号不配对归用法错误 2）。
- `reject_log_burst` / `reject_log_window` 真正生效：拒绝风暴期间审计限速，
  被抑制的条数在 `suppressed` 字段如实汇报（此前是零引用的死配置）。
- `write_state` 走统一原子写（fsync + 随机临时名 + 保留属主）。
- `parse_listen`：`bindPort = 0` 实测回落默认 7000，不再当作"无监听"。
- `do_GET /healthz` 的 BrokenPipe 不再把回溯打进插件 stderr。
- "是否需要 `--allow-unsafe`" 收敛为单点判定（此前四份实现）。
- 死代码清理（`restart` 的无效 try、`policy.validate` 的空分支）。
- `doctor` 重复探测去重（`frps -v` 与 `resolve_owner` 各一次）。
- 回归 review 追加修复：`doctor` 对低于门槛的二进制给出**正确诊断**
  （"低于最低支持版本"而非"无法读取版本 / 可能不是官方 frps"）；systemd unit
  渲染统一绝对化——`--root ./instances` / `--binary ./bin/frps` 这类相对输入
  不再写出 `ExecStart=bin/frps` 的坏 unit。

- 回归 review 追加修复：展示层异常不再拖垮启动（`on_tick` 进度回调的异常被
  隔离——`start 2>&1 | head` 场景曾把刚派生的 frps 误杀）；`note` / `progress` /
  `warn` / `trace` 在 stderr 管道断开时静默（不再二次崩溃）；tty 进度补清行尾码
  （消除残影）。

### 工程

- 新增 61 条回归用例（总计 357），覆盖率 83%。
- `init --bind-port` 加范围约束（`0` 归用法错误 2）；`config set --json` 的 noop
  路径输出 JSON（此前打印人读文本）。

## [0.2.0] - 2026-09-16

第三轮全量迭代：安全缺口、部署守护、稳定性与发布链路。

### 新增

- `start` / `restart` 在健康 gate（L1 ∧ L2）未通过时以**退出码 12** 收场并向
  stderr 告警——此前完全静默（退出码 0，脚本会当成成功）。未过 gate 的进程
  仍被托管：`status` 可见、`stop` 可停。
- `install --mirror URL`（可重复）与 `FRPSCTL_MIRROR` 环境变量：下载镜像可配置，
  兑现"下载地址可被镜像替换"的承诺。
- `service install --user/--group`：systemd 服务账户可指定（默认 `frps`）。
- `status` 输出 `listen` 行（人读）与 `listen` 字段（JSON），对齐设计文档 §7.4。
- `status --watch --json` 改为单行 NDJSON，可被逐行消费（`jq -c` 等）。
- shell 补全（`--install-completion` / `--show-completion`）。
- PyPI 发布链路：版本号单一来源（hatchling 读 `__init__.py`）、release workflow、
  CHANGELOG、`py.typed`。

### 修复

- **安全**：`config diff` / `edit` / `set` / `rollback` 的内联表与**跨行结构**
  （三引号多行字符串、多行内联表/数组）机密不再明文泄露；解析失败但形似含机密
  时保守打码；裸键名 `token`/`password`/`clientSecret` 同样命中。
- `config rollback` 每次只产生 **1** 份快照：此前 `rollback_to` 与 `apply_change`
  各存一份完全相同的内容，10 份历史实际只够 5 次操作，且快照创建在实例锁外。
- 全局选项前移支持 `--` 终止符：`config set k -- --json` 的字面值不再被搬走。
- `service install` 前置体检：服务账户不存在、二进制对服务用户不可达、
  日志目录不可写、**路径位于家目录（`ProtectHome=true`）**——这些过去都要等
  `systemctl start` 才炸，且错误现场与安装动作相隔很远。
- unit 的 `ReadWritePaths` 增加**实例目录**：`ProtectSystem=strict` 下其余路径
  只读，而 frp 默认要往实例目录写 `./frps.log`。
- `config set` 重建配置时**保留原属主**：systemd 部署把配置移交给服务用户后，
  再次改配置不会再"夺回"属主导致 frps 读不到。
- `plugin serve` 的 SIGTERM handler 安装纳入 `try`：安装瞬间收到信号不再绕过
  审计刷盘（此前 `finally` 尚未生效，进程直接死亡）。
- 配额检查按用户锁重构：dashboard 查询不再持全局锁，一个用户的慢查询不会串行
  挂住所有用户的登录链路（frp 侧对插件 HTTP 客户端没有超时）。
- `install --with-frpc --only-download` 不再切换 frpc 软链，与 frps 语义统一。
- `log` 改为纯 Python tail：不依赖外部 `tail` 命令，支持日志轮转后自动重开新文件。
- `plugin init` 生成策略文件改用原子写（此前 `write_text` 存在半截文件与权限窗口）。
- 健康等待期间进程死亡时按启动失败收尾并清理 `state.json`（不再留下假 RUNNING）。
- 负数数值选项（`--interval` / `--timeout` / `-n` 等）归入用法错误(2)：
  此前 `--timeout -1` 会跳过等待直接 SIGKILL——参数笔误造成不可逆动作。
- `status --watch` 重定向到文件时不再写入 ANSI 清屏码。

### 工程

- CI 增加 `ruff check` 与 80% 覆盖率门禁；`[dev]` 补齐 `pytest-cov` / `ruff`。
- 新增 70 条回归用例（单元 / CLI / 集成 / 插件各层，总计 296 条）。

## [0.1.0] - 2026-09-15

首个版本。

- 生命周期：`install` / `init` / `start` / `stop` / `restart` / `status` / `log`，
  含实例锁、三重进程身份校验、启动早退检测、三层健康判定。
- 配置闭环：`config get|set|edit|diff|rollback`，tomlkit 无损补丁、官方
  `frps verify` 权威校验、原子写、快照历史、失败自动回滚。
- 运维面：`doctor` 体检与安全 lint、`service install` systemd 集成、`kick`。
- 服务端插件：多用户鉴权、端口白名单、代理名/类型约束、`max_proxies` 配额、
  异步 JSONL 审计；fail-closed 与绑回环硬约束。
- 五层测试：单元 / 集成 / CLI / 契约（真二进制）/ 故障注入。
