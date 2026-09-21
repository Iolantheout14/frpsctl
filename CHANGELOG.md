# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.3.4] - 2026-09-21

**Web 管理台结构性重构：单文件 → ESM 同源多模块 + 赛博朋克设计系统 + 八项新功能**。
前端从 1865 行单文件拆为 26 个模块（零构建链——ESM 是浏览器原生能力），
新增服务视图 / 版本管理（后台安装任务）/ 命令面板 / 详情抽屉等能力；
CLI 与 Web 的共享逻辑第三次下沉（互斥守卫与 serve argv 进 core）。
新增 71 条 Python 测试（全量 869 → 940；非契约 839 → 910）与 26 条前端模块单测
（`node --test` 直接 import 生产模块），新增 10 个 API 端点（契约零破坏：命令面/
选项/退出码/既有 JSON 字段不变）。

### 新增

- **前端模块化（零构建链）**：`index.html`（shell，11.9KB）+ 5 个 CSS +
  26 个 ESM 模块（`js/lib` 纯逻辑 / `js/ui` 组件 / `js/views` 视图）；
  新增静态资源路由 `/static/*`（白名单扩展名 + 防穿越 + ETag 条件请求 +
  `no-cache`——不做 immutable/版本串，避免升级后混用旧模块）。
  CSP 保持 **nonce-only**（外链 script/link 同样带每请求 nonce 注入）。
- **赛博朋克设计系统**：赛博暗为默认（网格底纹 + 扫描线 + 青/品红霓虹描边 +
  HUD 四角刻度 + 切角装饰 + 等宽数据 + 品牌 glitch），亮色为"白昼 HUD"
  降饱和映射；双主题令牌全量对齐（守卫强制）。
- **服务视图（新页）**：frps / Web 管理台 / 服务端插件的托管状态一屏可见；
  插件 `start|stop|restart` 可在页面操作（systemd 优先、direct 次之）；
  Web 管理台自身只读（启停会断开当前会话，给 CLI 提示）。
- **版本管理（新页）**：运行中/磁盘版本与一致性、最低与建议版本；
  表单提交安装任务（后台线程执行：下载进度 / 校验 / 落盘 / 换链），
  1 秒轮询进度；"重启使新版本生效"一键入口。
  Web 不暴露 `--insecure/--mirror`（始终强校验 + 默认镜像）。
- **后台任务模型**（`web/tasks.py`）：单飞行（同时只允许一个安装）+ 有界
  （最近 8 条）+ clock 可注入；安装经 `core.release` 的
  `bin/.install.lock`（CLI 与 Web 共用一把锁，杜绝并发换链）。
- **命令面板（Ctrl+K）**：视图切换 / 实例动作 / 主题切换 / 复制实例名的
  统一入口（↑↓ 选择、Enter 执行、Esc 关闭）。
- **详情抽屉**：客户端与代理行 → 侧滑抽屉（v2 详情端点透传：
  `/api/clients/{key}`、`/api/proxies/{name}`）。
- **代理类型分布环图 + 今日流量 Top 5 排行**（颜色走 CSS 类，主题即时生效）；
  **Hero 增强**：会话入站/出站累计与**速率峰值/均值**（来自本地采样基线）。
- **审计时间窗**：`?since=1h/24h/7d`（与 CLI `--since` 同一解析器），
  插件与 Web 两个 scope 都支持；非法值 400。
- **诊断导出**（`/api/diagnostics`）：状态 + 体检 + **打码后**配置 + 日志尾部
  200 行的文本附件（日志不脱敏，页面与文档都提示自行检查）。
- **增量日志**：`/api/logs?since=<offset>` 只返回新增完整行（不再每 5 秒
  整段重传与重渲染）；轮转/截断返回 `reset=true`；前端 DOM 节点有界（4000）。
- **配置"待重启"提示**（CLI + Web）：配置文件 mtime > 进程启动时刻的只读
  推导（`--no-restart`、手工编辑、外部工具都会命中）——CLI `status` 告警、
  Web 横幅 + 一键重启。
- **`allowPorts` 结构化编辑器**：`single`/`start-end` 表格行编辑（含即时
  校验与"+"增行），序列化为 TOML 内联数组后走同一条预览/应用事务。

### 变更

- **共享逻辑下沉 core（第三次）**：`core/serve_guard.py`（systemd ↔ direct
  双向互斥——Web 的插件启停此前会绕过 CLI 层守卫）、
  `serve_runtime.build_serve_argv()`（web/plugin 后台命令行的单点，
  含跨服务选项白名单校验）、`serve_runtime.frpsctl_executable()`
  （CLI 与 Web 共用的可执行文件定位）。
- `install()`（core.release）新增 `on_progress` 回调（阶段：checksum /
  download / verify / place / switch；回调存在时改走 urllib 分块下载以取得
  结构化进度）与 `bin/.install.lock` 互斥。
- 前端行为测试升级：`tests/frontend/*.test.mjs` 用 `node --test`
  **直接 import 生产模块**断言纯逻辑（format / table / audit-format /
  chart-math / port-ranges，24 条）——取代"从单文件抽取源码再 eval"的旧法；
  CI 增加显式 `setup-node` 与前端模块语法检查。
- README 已知边界改写：单文件 → "同源多文件 + 零外部域 + 零构建链"。

### 修复

- **`PageResult` 静默截断**（v0.3.1 遗留）：v2 信封缺 `total` 时旧实现把
  `total = len(items)` → 只取第一页且 `truncated=False`；现在缺 total 时
  按"页是否满"续拉，翻页上限用尽如实标记 `truncated` 且新增
  `total_known=False`（CLI 说"至少 N 条"、Web 列表同口径）。
- **`clear_offline_proxies` 裸 httpx 异常**：唯一未收口的请求路径——
  网络错误此前冒到 CLI/Web 变"未分类错误(1)"，现在收口 `AdminUnreachable(7)`。
- **`is_locked` 假阴性**：锁文件存在但当前用户读不到（root 建的 0600）时
  旧实现静默判 `False` → doctor 漏报并发操作；改为三态
  （True/False/**None**），doctor 对 None 报 INFO（降级可见）。
- **Web 审计的凭据防线**：`params` 落盘前经标量白名单 + 敏感键打码
  （`config.is_secret_key` 判据）+ 长度截断；审计文件显式 `0600`
  （此前权限跟随 umask，配置/快照/口令文件都是显式 0600）。
- **插件审计文件权限**：写入后同样显式 `0600`。
- **代理类型 3 环图的颜色**：改用 CSS 类驱动（SVG `fill` 属性会被同选择器
  CSS 覆盖——与 v0.3.3 柱状图渐变静默失效同一根因，柱状图一并改为
  CSS 引用的 `url(#渐变)`）。
- **`theme-anim` 死样式接通**：v0.3.3 定义的"主题切换过渡"类从未被 JS
  激活（声称实现但实际不生效），现在 `applyTheme` 添加并 300ms 后移除。
- **会话流量基线永不生效**（交付前对账发现）：`samples.length === 0` 判断——
  采样持久化在 localStorage，**刷新页面后基线永不设置、Hero 的会话入站/出站
  永远显示 "-"**；改为"本次页面加载的第一次采样"（内存态，语义即"本次会话"）。
- **亮色主题次要文本对比度不足**（交付前对账实测）：`--muted` 在浅卡片上仅
  **4.23:1**（低于 WCAG AA 的 4.5）→ 调深到 `#4b647c`（实测 5.1+）；新增
  `TestContrast` 对双主题的主文本（≥7）/次要文本（≥4.5）/状态色（≥3）做
  WCAG 2.1 **实测**守卫（此前只有"声称 ≥ AA"）。

### 发布前全量回归 review（14 项修复，7 项真实缺陷）

全部 diff 逐行审查 + 对抗性实测 + 反向验证，修复：systemd 托管下"配置待重启"
永不提示（判据依赖 systemd 分支为 None 的 uptime 字段 → 改为进程启动时刻直判）；
任务提交的"单飞行检查→插入"竞态（移入同一把锁）；Web 插件 restart 不读旧参数
里的自定义 `--policy` 路径（会静默换策略 → 旧参数优先 + 与 `plugin check` 同
判据）；Web 日志读取失败把错误文本写进日志体（永久污染 DOM → toast + 重置
offset）；`allowPorts` 编辑器在表单重建时静默丢弃未预览编辑（草稿文本优先 +
新增 `parsePortText`）；详情端点 404 语义、任务失败审计、淘汰跳过运行中、
诊断导出 filename 消毒、日志 offset 边界等。审查同时确认三处"看似可疑但
安全"的项（UTF-8 截断边界、total 虚高的截断语义、运行中淘汰不可达性），
并诚实记录一处测试局限（GIL 下窄窗口竞态无法可靠复现，原子性由代码审查
保证）。最后一轮复审再补 3 项（含 1 项修复引入的 TypeError 逃逸路径、相对
policy 路径的跨 CWD 解析、结构化编辑器草稿清除基准），并完成发布产物验证
（干净 venv 安装 wheel：26/26 前端模块与全部新 API 可用）。最终全量
940 passed / 覆盖率 85.89% / `node --test` 26。

### 测试

- Python 新增 71 条（全量 869 → 940；非契约 839 → 910）：增量日志 8、锁三态 2、
  审计参数与权限 2、守卫下沉 2、argv 单点 3、安装锁与进度 2、翻页无 total 3、
  服务/版本/任务/详情/审计 since/增量日志/诊断导出 17、静态路由 4。
- 前端新增 26 条（`tests/frontend/*.test.mjs`：25 条纯逻辑真 import + 1 条**模块加载冒烟**——顶层 DOM 访问与循环依赖在 CI 直接暴露）；
  守卫重写为多模块版（模块语法 / **import 图（路径存在 + 符号有导出 +
  无孤儿模块）** / 纪律 / CSS 双向 / id 双向 / 静态布局）。
- 既有测试同步：报告形状 +`config_pending_restart`、`fake_download` 接受
  `on_progress`。

## [0.3.3] - 2026-09-20

**后台服务与 Web 体验（systemd 之外的第二种托管形态 + 管理台视觉/交互大改）**。
Web 管理台与插件服务新增 `start|stop|restart|status`（direct 后台，与 systemd
双向互斥）；管理台前端重做设计系统（极光/玻璃/霓虹）、图表 2.0 与交互升级。
新增 39 条测试（828 → 869；非契约 798 → 839），契约快照 +8 命令。

### 新增

- **`web start|stop|restart|status` / `plugin start|stop|restart|status`**：
  非 systemd 环境的后台托管（子进程就是 `serve`，与 `ExecStart` 同构）——
  状态与启动参数落 `<实例>/web|plugin-state.json`（0600），日志落
  `<实例>/web|plugin.log`（8 MiB 轮转），停止走 SIGTERM → 等待 → SIGKILL。
  `restart` 复用上次参数（可覆盖）；`status` 统一显示 `systemd / direct /
  none` 三种归属。
- **后台进程的三重校验**（`core/serve_runtime.py`）：pid 存活 + 启动时刻 +
  命令行含 `serve` 标记；状态文件损坏/陈旧/指向外人一律拒绝而不是猜测
  （ADR-7 同一纪律）。
- **口令衔接**：`web start` 未给口令来源时默认用/生成实例内 `web-password`
  （与 `web password set|show` 同一文件）；`--password` 的值也先写文件——
  后台模式口令可读回，且明文不出现在进程命令行里。
- **互斥与孤儿防护**：`web/plugin start` 对 active 的 systemd unit 拒绝
  （反之 `service install|start|restart` 对存活的 direct 进程同样拒绝）；
  `uninstall` 预检新增 direct 服务（运行中拒绝，`--force` 先停再卸），
  `doctor` 报告 direct 服务状态（运行中 INFO / 状态损坏 WARN / 身份不符 ERROR）。
- **管理台 UI 重做**：极光渐变背景 + 玻璃卡片 + 渐变强调色 + 状态呼吸灯 +
  渐变指标条（首次滚动）；骨架屏与空态；自绘确认弹层（替代原生 confirm，
  支持 Enter/Esc）；toast 图标与进度条；登录页重做；主题切换平滑过渡。
- **图表 2.0**：7 天柱状与实时曲线改渐变（CSS 变量驱动，主题即时生效）；
  实时曲线平滑（Catmull-Rom→贝塞尔）并加面积填充；两者都有悬浮十字线 +
  自绘 SVG 浮动提示（不引入任何内联 style，CSP 不变）。
- **交互**：快捷键（`g d/g c/g a` 导航、`r` 刷新、`/` 聚焦过滤、`Esc` 关闭）、
  客户端/代理/审计表格本地过滤、客户端/代理点表头排序、双击复制名称、
  日志 ERROR/WARN 着色与一键复制、`prefers-reduced-motion` 全动画关闭。

### 变更

- 仪表盘"概览"卡并入顶部"实时概览"指标条（同一批数字不再重复展示）。
- npm/构建链保持零引入：仍是单文件前端（CSP nonce、无外部资源、无内联
  style）；前端守卫新增组件样式/无障碍/原生弹窗禁用 3 条断言。

### 测试

- 新增 39 条（828 → 869；非契约 798 → 839）：serve_runtime 真进程全边界
  （启动/停止/陈旧/外人/损坏/早退/端口未就绪/SIGKILL 兜底/日志轮转）、
  **真链路 e2e**（`python -m frpsctl web serve` → HTTP 200 → stop；
  `plugin serve` → Login 裁决 → stop 后审计刷盘）、CLI 后台命令接线与 JSON、
  systemd↔direct 互斥（web/plugin 双向）、uninstall 孤儿防护、doctor
  进程与端口一致性发现、前端守卫扩展 3 条。

## [0.3.2] - 2026-09-20

**systemd 全用户支持（任何账户都可以作服务用户）**。`frps` 不再是写死的默认：
`--user` 缺省时优先系统已有的 `frps` 用户、否则当前用户；新增 `--create-user`
自动创建系统账户；服务安装参数写入 `<实例>/service.json` 留档，`uninstall` 与
`doctor` 跟随实际配置。零新命令、零新环境变量；契约变化仅新增选项（快照同步）。
新增 47 条测试（781 → 828；非契约 751 → 798），覆盖率 86.38% → 87%。

### 新增

- **`--create-user`（`service install` / `web service install` /
  `plugin service install` 三处）**：目标服务用户不存在时自动创建系统账户
  （`useradd --system --no-create-home --shell /usr/sbin/nologin --user-group`），
  幂等、仅 root；须配合显式 `--user`（"创建谁"不能靠猜）。
- **服务账户解析（§12.2 / §25）**：`--user` 缺省 → 系统已有 `frps` 则用它
  （向后兼容现有部署），否则当前有效用户（root 部署零配置可装）；`--user` 接受
  数字 UID、`--group` 接受数字 GID（解析为账户名）；`--group` 缺省且同名组缺失
  时回退**用户主组**（可见提示，不再产出"装得上、起不来"的 unit）。
- **安装留档 `<实例>/service.json`（0600）**：记录三个服务实际使用的
  user/group/log_dir/bind 等渲染参数；`uninstall` 按留档提示实际服务账户与
  日志目录，`service status` 显示运行账户（`systemctl show` 实际值）。
- **`doctor` systemd 部署检查**：账户被删 / 组被删 / ExecStart 二进制对服务
  用户不可达 / 路径落在家目录 → ERROR；以 `systemctl show` 的实际值为准、
  留档回退；探测失败降级 WARN（doctor 不崩）。

### 修复

- **"用户存在但同名组不存在"的 unit 被放行**：`_account_ids` 此前只在显式
  `--group` 时查组，而渲染出的 `Group=` 实际是 `group or user`——缺失组会
  安装成功、`systemctl start` 才报 "Group not found"。现在用户与组都必查。
- **卸载的服务账户提示写死 `frps`**：用 `--user alice` 部署时 alice 的残留
  不会被提示，而系统里恰好有 frps 时反而误报；现在按安装留档逐个提示。
- **卸载的日志目录提示写死 `/var/log/frps`**：改用留档里的实际 `--log-dir`。
- **unit 文件注入面收紧**：`--user`/`--group` 值经字符集校验（不允许换行/
  空格/等号）后才进 unit 模板。

### 变更

- **`--user` 默认值由 `"frps"` 改为"按矩阵解析"**（契约快照同步）：缺省路径
  不再因系统没有 frps 用户而失败。
- **`--force` 覆盖共享模板且服务用户变化时输出警告**（模板是全部实例共享的，
  改 `User=` 影响所有使用它的实例）。
- **以 `root` 作服务用户时输出安全警告**（stderr；JSON 模式同样输出，脚本
  收集 stderr 时也必须看到）。

## [0.3.1] - 2026-09-19

**稳定性与可靠性补强（CLI + Web）**。零新命令、零新 API；唯一契约变化是
CLI 数值参数**补上限**（收紧，快照同步）。基于全量通读（源码 33 模块 + 三份
文档逐行）发现的 6 处 P0 缺陷全部根治，并落地 4 项量化性能/资源改进。
新增 46 条测试（735 → 781；非契约 705 → 751），覆盖率 86.38%。

### 修复

**systemd 异常契约（影响 CLI 全部命令）**

- `subprocess.TimeoutExpired` 此前无人接管：systemd 无响应时 `status` /
  `start` / `stop` / `doctor` 全部崩为"未分类错误(1)"——违反 `status`
  "永远能回答现在什么情况"的承诺（与 v0.2.1 修过的 `release._verify_binary`
  同型缺口）。现在 `_systemctl` 统一收口为契约内异常，调用路径分流：
  - **查询降级且可见**：`resolve_owner` 改为"按 state.json 判定 + 记录探测
    错误"，`StatusReport.systemd_probe_error` / `--json` 字段 / CLI 告警 /
    Web 顶部横幅 / `doctor` WARN 全部如实呈现；探测错误**进 owner 缓存**
    （否则每轮 status 都重打一次 10 秒超时）。
  - **变更 fail-closed**：`start` / `stop` / `restart` / `uninstall` 在
    所有权不可判定时拒绝执行（退出码 11）——降级判定可能把 systemd 托管
    实例误报成"未运行"，变更路径绝不允许。

**并发与资源**

- **插件服务并发无界**：此前是裸 `ThreadingHTTPServer`（线程数无上限），
  而管理台 v0.3.0 已有 32 上限——同栈两套行为。提取共享
  `core/httpserver.py`（`BoundedThreadingHTTPServer`），插件默认 **64**
  worker、超限立即 503 并断开（对 frp 而言"非 200 = 该操作失败"，fail-closed
  方向正确；插件请求极轻，正常规模远达不到）。accept 队列 64 → **256**
  （登录风暴容量余量）；并发压测校准出的语义如实记录：高并发冲撞下部分
  超限连接会被内核按 TCP 语义重置（**不引入等待**——frp 对插件无超时，
  拖慢 accept 的代价更大；RST 对 frp 同为操作失败）。
- **拒绝限速表无界**：`RejectLimiter` 的 `_hits` / `_suppressed` 以客户端
  自报 `user` 为键、只增不删——持续换名的拒绝风暴可无界增长内存。上限
  4096，超限驱逐窗口起点最早的条目（与 `web/auth` 失败来源表同一条纪律；
  驱逐不改变安全语义，请求仍被拒绝）。
- **失败登录审计限速是模块级单例**：多实例/多用例之间互相串味（测试此前
  靠"前后手工 clear 模块状态"绕过）。改为 `WebContext.login_audit` 实例
  持有（语义本就该是"每管理台进程一份"）；哨兵值 `0.0` → `None`（假时钟
  从 0 起步时首次调用会被误判为冷却中——测试暴露的真实边界缺陷）。

**环境健壮性**

- **ASCII locale 下 stdout 崩溃**：`LC_ALL=C` 且 PEP 538 coercion 失效
  （`PYTHONCOERCECLOCALE=0` / 容器无 C.UTF-8）时 stdout 是 strict+ascii，
  `doctor` / `capabilities` / `uninstall` 等输出中文的命令直接
  `UnicodeEncodeError` → "未分类错误(1)"。入口统一
  `core.diagnostics.configure_streams()`（UTF-8 + backslashreplace）——
  任何 locale 下输出都是一致的 UTF-8；`python -m frpsctl.docs` 入口同样处理。
- **stderr 断开时二次崩溃**：`web` / `plugin` 的 `_note`、`web.api` 的审计
  告警、`instance.prune_history`、`plugin.audit.close` 的告警写没有保护——
  管道断开（`2>&1 | head`）时会从辅助路径抛异常。全部补
  `suppress(OSError)`（与 `cli.ui` 的既有纪律一致）。

**CLI 参数边界**

- **数值参数补上限**（此前只有下界，Web 侧 v0.2.3 早有上限——同一纪律的
  另一半）：`--health-timeout` / `--timeout` ≤ 600、`--interval` ≤ 3600、
  `-n/--lines` ≤ 100000；越界归用法错误(2)。`--health-timeout 999999`
  曾是"等待 11.5 天"，`-n 10^9` 会把整份日志拉进内存。
- **契约快照纳入 `min`/`max`**：数值范围是命令契约的一部分（这次收紧就该
  被锁住），并补显式生成入口 `python -m tests.test_contract_snapshot --update`
  （此前文档写"删除基线并重跑"实际会 FileNotFoundError）。

### 性能 / 资源（实测）

- **`--version` 快路径**：精确匹配单个 `--version` 时在任何重 import 之前
  输出并退出——实测 DrvFs **1.9s → 0.08s**、真实文件系统（ext4）
  **1.45s → 0.001s**；`--version --json` 等组合行为不变。
- **惰性导入**：`core/admin.py` 的 httpx 移入使用点（全项目唯一顶层 httpx）、
  `PORT_FIELDS` 从 `schema` 迁到 `healthcheck`（`doctor` 不再拉起 pydantic）
  ——`status` 链路 import 1.75s → **0.78s**（DrvFs），httpx/pydantic 零加载。
- **`/api/status` 6 秒缓存**：页面 5 秒轮询 × （L2/L3 探针 + `server_info`）
  的开销收口（TTL 必须 ≥ 轮询间隔——v0.3.0 的量化教训）；写操作经
  `cache.invalidate()` 立即失效。预计 30 秒空转的探针次数 **6 → 1**。
- **趋势查询整体预算 6 秒**（`TRAFFIC_DEADLINE`）：慢 dashboard 下单个
  `/api/traffic` 最坏 ~19s → ≤6s；预算内返回已完成部分并带
  `partial=true`（CLI `traffic` 与 Web 图表均如实提示，绝不把部分当全量）。
- **`same_config_active` 批量查询**：多 active unit 从 N 次
  `systemctl show` 降为 **1 次**（发生在 `start` 的实例锁内）。

### 文档

- **发布前全量回归 review 修复包**（全部 diff 逐行 + 对抗性实测 + 测试有效性
  反向验证）：`_systemctl` 补齐 `OSError` 收口（systemctl 被删的罕见竞态不再
  崩 status）；install 的 systemd 探测降级补告警；`--version` 快路径收窄触发
  条件（宿主程序不再可能被劫持，附守卫测试）；实现注释与实测语义对齐、
  删除零调用别名。
- **最后一轮 review 的再收口**：测试层 systemctl 探测改为**无条件**确定性化
  （此前"仅无 systemd 宿主生效"的条件在真 systemd 机器上失效，偶发超时仍能
  误伤卸载类断言）；随之现形的两条脆弱测试改为显式语义（`process_gone`
  判据 / 显式回收僵尸）；并发 burst 测试补齐连接层异常捕获并去掉对调度敏感
  的"必须出现拒绝"断言（确定性拒绝由占位式测试保证）。
- README：已知边界重复行修复；`capabilities` 命令数示例 56 → 55（叶子
  口径）；退出码表补信号惯例（Ctrl-C=130、管道正常退出=0）；已知边界新增
  status 缓存 / 插件并发 / `partial` 三条；测试与覆盖率数字刷新。
- 设计方案：§23.2 的 TTL 表与实现对齐（6s/3s，指向 §23.5 量化修正）；
  新增 §24 第十一轮记录（缺陷清单 / 量化 / 边界记账）。
- `web/server.py` 模块 docstring 的 CSP 说明修正（仍写着 `'unsafe-inline'`
  的 v0.2.x 旧文）与 `start()` 并发注释同步。

## [0.3.0] - 2026-09-18

**内核重构与传输层升级**。用户可见契约零变化（命令路径 / 选项 / 退出码 /
JSON 字段 / HTTP 状态码），但为后续所有功能迭代移除结构性阻力：CLI 从
3253 行单文件拆成命令包、Web 从"每 5 秒全量重算"升级为条件请求 + 分层缓存、
文档三表（命令 / 退出码 / 环境变量）改为与代码对账。新增 91 条测试
（644 → 735；非契约 614 → 705）。

### 新增

**CLI**

- `config apply --set K=V（可重复）--unset K（可重复）[--dry-run]`：**多键**
  变更——一次提交 → 一份快照 → 一次重启（与 Web 配置表单同语义，复用
  `apply_sets`）。此前 CLI 只能逐键 `config set`，多键就是多次重启。
- `capabilities [--json]`：能力清单（命令树 / 退出码 / 环境变量 / 版本门槛），
  **从代码派生**（Typer app、`ExitCode`、`env.ENV_VARS`）——脚本与文档生成
  消费同一份数据。
- `web audit tail|stats`：Web 操作审计的只读面（见下）；`--since` 与插件审计
  同语义。
- `config get/set/unset <TAB>` 与 `plugin user remove <TAB>` / `plugin config
  set <TAB>`：动态键名/用户名补全（零副作用，失败即空）。
- `web serve --metrics` / `web service install --metrics`：Prometheus 文本
  `/metrics`（实例状态 / 三层健康 / dashboard 统计；Basic auth：用户名任意、
  口令 = 管理台口令；服务端 5 秒缓存）。
- OIDC 配置支持：`auth.method = "oidc"` 时校验 `auth.oidc.issuer` /
  `audience` 完整性（schema 层拒绝缺项）；`doctor` 把缺项报 ERROR、把
  `method = oidc` 下残留的 `token` 报 WARN。OIDC 协议本身仍由 frps 实现。

**Web 管理台**

- **操作审计**（`core/web_audit.py`）：变更动作（start/stop/restart/prune/
  rollback/config apply）与登录事件写入实例目录 `web-audit.jsonl`（来源 IP、
  会话**指纹**、成功/失败、参数标量）；失败登录审计带限速（每来源每分钟一条，
  防爆破刷爆）；审计视图新增 **Web 操作** tab；写失败绝不阻断动作但会告警。
- **条件请求（ETag/304）**：已认证 GET 响应带内容 ETag + `Cache-Control:
  no-cache`，浏览器自动携带 `If-None-Match`——"数据没变"时响应体为 0 字节。
  写响应 / 错误 / 登录 / 会话响应一律 `no-store`。
- **服务端 TTL 缓存**（`web/cache.py`，有界）：traffic 30s / clients·proxies
  6s / logs 3s（按行数分键；TTL 必须 ≥ 轮询间隔才有意义），变更动作与配置
  应用前**显式失效**——空转轮询不再每轮重放最多 50+ 次 dashboard 查询。
- **有界并发**：请求 worker 上限（默认 32）用尽时立即 `503` 并断开，不再
  无限堆线程（收口 README 旧边界"管理台不限制并发连接数"）。
- **CSP nonce 化**：`<script>` / `<style>` 每请求注入 nonce，CSP 去掉
  `'unsafe-inline'`（并加 `base-uri`/`form-action`/`frame-ancestors` 限制）；
  全部内联 style 属性改为工具类——这是纵深防御：注入面本已为零，现在出口
  也被封死。

**性能 / 可靠性**

- **owner 探测缓存**：`Lifecycle.resolve_owner` 实例级 2s TTL + `state.json`
  戳失效——`status --watch` 与 Web 轮询不再每轮 fork 两个 `systemctl`
  （变更动作会显式清缓存，缓存也不可能掩盖刚发生的所有权变化）。
- **审计轮转**：插件与 Web 审计文件超过**大小**（`audit.max_mb`）或**年龄**
  （`audit.max_days`，与 frp 日志 maxDays 同语义）自动轮转（`path` → `.1` →
  `.2`，保留 2 份；两维独立、0 = 禁用），失败安全降级；读取侧（tail/stats）
  跨轮转文件合并——审计不再无限增长。

**文档 / 工程**

- `frpsctl/docs.py`：命令 / 退出码 / 环境变量三表与 README 的**双向对账**
  （CI 守卫 + `python -m frpsctl.docs check`）；`env.py` 集中定义 14 个环境
  变量。新增命令或变量忘记写文档 → CI 红。
- 命令面**契约快照**（`tests/snapshots/cli_commands.json`，56 路径 / 180
  参数）：重构期间用"逐字节恒等"锁住命令契约。

### 变更

- CLI 拆包：`cli/__init__.py`（3253 行）→ `cli/app.py`（Typer 组装）+
  `cli/runtime.py`（共享依赖装配）+ `cli/commands/*`（按域 7 个模块，最大
  892 行）；`cli/__init__.py` 保留兼容 shim（re-export 全部符号，一个版本
  周期后收敛）。测试 patch 目标随之迁移（并统一到"patch 源头模块"）。
- **表示层单点**（`frpsctl/report.py`）：CLI 与 Web 的 status / start /
  health / doctor / audit 形状由同一份纯函数生成——消除 `_status_payload`
  与 `web.api.status_payload` 等四处双份实现。`start` 的 health 因此统一
  带上 `gate` / `plugin_warning`（向后兼容的增字段）。
- `ExitCode → HTTP` 映射改为表驱动单点（新增退出码忘记补映射会落到 500）。
- `--binary` 入口的版本门槛补齐测试（§8.4 要求 install 与 `--binary` 两条
  入口都检查；此前只有下载路径有守卫）。
- README：命令表 / Web 能力 / 已知边界 / 环境变量与代码对齐；文档漂移
  修复包（13 处历史遗留）随设计文档更新一并落地。

### 修复

- **发布前回归 review 修复包（v0.3.0 review 实测）**：
  - `/metrics` 的 Basic auth 失败**计入登录限速表且与登录共用来源解析**
    （此前它是绕开登录限速的第二条口令爆破通道；`--trusted-proxy` 部署下
    两条桶还必须按同一 XFF 最后一跳，否则通道重新分裂）；
  - 非 ASCII 口令（中文/emoji）此前会让 `hmac.compare_digest` 抛
    `TypeError`、登录线程断开——比较统一改为 encode 后常量时间比较；
    HTTP JSON 可构造的 lone surrogate（U+D800 单独出现）同样不再触发
    `UnicodeEncodeError`（surrogatepass 编码后比较）；
  - Web 审计的"轮转 + 追加"加写锁（并发动作下 rename 序列交错会丢归档）；
  - 响应缓存加**条目上限**（日志按行数分键可被遍历放大内存）；
  - `doctor` 对畸形 `auth`（非表）与 `auth.oidc`（非对象）不再崩溃、报 ERROR；
  - `plugin config list` 对老策略缺失的 `audit.max_mb`/`audit.max_days`
    显示数值默认值（此前显示成布尔 `true`）；
  - `config set` 的 noop 分支对敏感键打码（§10 硬约束 2）；
  - 每响应关闭连接并显式声明 `Connection: close`（worker 槽位按**请求**
    占用，少数标签页的 keep-alive 不再可能占满并发额度；空闲读超时 30 → 5
    秒；`send_error` 路径的重复头已去重）+ 连接类异常不再打 traceback；
  - `serve_forever` 不再对同一 httpd 二次启动事件循环（start 已在后台服务）；
  - `plugin/web audit tail -f` 在审计文件尚不存在时等待出现（非跟随模式
    仍给出可行动错误）；
  - `read_tail` 的坏行不再吃掉配额（跨轮转追溯按成功记录数）；
  - `status --watch` 复用同一 Lifecycle（owner 探测缓存真正命中）；
  - `capabilities` 的 frps 门槛从 `core/version` 派生（不再手写漂移）；
  - 列表缓存 TTL 修正**（量化验证发现）**：clients/proxies 的 TTL 原定 2 秒，
  而浏览器轮询间隔 5 秒——TTL 小于轮询间隔时命中率≈0，等于没有缓存。改为
  6 秒后轮询几乎每轮命中（写操作显式失效保证"改完立刻可见"）。实测：
  30 秒空转的 dashboard 请求从 318 降到 57（降幅 82%）。
- 前端逻辑分层：审计渲染与列表行的纯数据变换（`clientRows` / `proxyRows` /
  审计统计项 / 备注 / 行变换）提为纯函数，node 动态断言从 3 个扩展到 8 个
  ——"语法正确、逻辑错误"的白屏风险继续收窄。
- **响应形状基线**：`tests/test_report.py` 对 `report.py` 的全部形状（status
  两分支 / start / health / doctor / audit）做**完整键集**断言——CLI 与 Web
  共用同一单点，形状漂移（漏键/改名）在两处同时被抓住。
- 命令面文档：`capabilities` / `config apply` / `web audit` 等新命令全部进入
  README 与设计文档 §7.2，由对账守卫防回退。

## [0.2.6] - 2026-09-17

把 core 已经实现、但用户面看不到的能力**全部呈现**出来（审计 / 体检 / enabled /
列表总数 / 清理条数），并把两条高频数据路径做快（日志反向读、流量汇总并发 +
缓存）。不新增 core 语义，只新增"可见性"与"性能"。

### 新增

**CLI**

- `plugin audit tail [-n] [-f] [--json]` / `plugin audit stats [--since] [--json]`：
  审计的只读面（此前审计只写不读）。tail 复用日志的反向读取；`-f` 跟随新记录
  且在轮转时自动重开；stats 流式统计总量 / 允许 / 拒绝 / 按用户 / 按操作 /
  限速抑制累计，`--since` 支持 `24h` / `7d` / ISO 时间 / unix 时间戳。
- `plugin config list|set`：策略级设置的结构化编辑（此前只能手写 JSON）——
  `allow_unknown_user` / `require_client_id` / `reject_log_burst` /
  `reject_log_window` / `admin_url|user|password` / `audit.enabled` /
  `audit.path`（字面 `null` = 仅内存）。写入前用与 `plugin check` 相同的判据
  复验 + 0600 原子写；未知字段直接拒绝；`admin_password` 在读取侧打码。
- `web password set [--stdin] [--prompt]`：口令轮换。不给输入通道时生成随机
  口令并只显示一次；systemd 托管时提示重启生效。
- `web serve --access-log`：逐请求日志（WebSettings 早就有这个字段，CLI 从未
  透传）。
- 三个 `service status`（frps / 插件 / Web）同时报告 **enabled**（开机自启）
  与 active——`is_enabled` 在 v0.2.5 就为卸载实现了，用户面一直看不到。
- `clients` / `proxies` 显示**总数**；列表被翻页上限截断时向 stderr 告警、
  `--json` 带 `truncated`（此前静默截断）。`proxies` 增加启用时长列
  （`lastStartAt`）与 `--json` 的 `last_start_at` 字段。
- `prune` 返回**清理条数**（清理前后各数一次离线记录；列表被截断时如实标注
  只是下界）。
- `doctor --json` 增加 `counts`（error/warn/info），人读末尾显示计数。
- `--instance` 的 shell 补全（列出实例根下的实例名；只读、失败即空）。
- `instances --health` 多实例**并发**探测（顺序保持稳定）。

- `plugin service start|stop|restart` / `web service start|stop|restart`：三个服务类
  的 start/stop/restart 早已实现，但此前只有 frps 的经由 `frpsctl start/stop/restart`
  委托 systemd——插件与 Web 服务只能手工 `sudo systemctl`。`install` 的收尾提示
  同步改为指向这些命令。
- `plugin service install --access-log` / `web service install --access-log`：
  访问日志开关此前只存在于前台 `serve` 命令，systemd 部署无法开启。
- `plugin audit stats` 增加**裁决耗时**统计（平均/最大，来自 `elapsed_ms`）——
  审计记录这个字段的目的（回答"插件拖慢了登录吗"）此前没有出口；Web 审计
  视图同步展示。
- 口令生成器收敛为**单点**（`core.systemd.generate_web_password`，web 侧
  `generate_password` 改为委托）：此前 `web serve` / `web password set` /
  `web service install` 各自写 `token_urlsafe(18)`，而函数的 docstring 声称
  "单点实现"——注释与事实不符。
- `proxies --json` 增加 `client_id` 字段。

**Web 管理台**

- **审计视图**（第三个导航）：策略位置、统计卡片（总量/允许/拒绝/限速抑制/
  坏行）、按用户与按操作分布、最近 50 条记录（新 → 旧）。策略缺失/关闭/仅内存
  时如实说明原因，而不是报错。
- **系统体检卡片**（只读，与 CLI `doctor` 同一实现）：按钮触发（不自动轮询）、
  severity 分组着色、计数摘要；注明"以 Web 服务进程权限执行"。
- **流量接口重构**：`GET /api/traffic` 只回**逐日汇总**（服务端聚合 + 并发
  查询 + 30 秒缓存）；单代理明细走新的 `GET /api/traffic/{name}`，前端展开某行
  时才拉取（缓存 60 秒）。此前每次响应携带最多 50 个代理 × 7 天明细、每 5 秒
  串行重查一遍 dashboard。
- 日志卡片：行数可选（100/200/500/2000）；**暂停跟随期间不再拉取**（滚动到底
  部恢复并立即补拉）。
- 配置页：键名搜索过滤（重建表单时保留未预览的修改）；数组/内联表值的括号
  引号配对即时校验（红框提示）；有未保存修改时关闭页面弹出浏览器确认。
- 客户端/代理列表显示总数；"清理离线记录"用服务端返回的准确条数。

### 修复

**发布前回归 review（v0.2.6，实测复现 → 修复 → 回归固化）**

- **`plugin audit tail -f` 与 `status --watch` 的流式输出不实时**：stdout 重定向
  到文件/管道时是块缓冲，`ui.emit` 依赖进程退出冲刷——跟随/刷新要攒满 4KB（或
  进程被杀）才吐数据，"实时"语义失效。跟随与逐轮刷新改为**显式 flush**
  （`log -f` 的既有实现一直如此，两条新路径与之对齐），并按 BrokenPipeError
  冒泡语义处理（`| head` 优雅退出）。
- **前端 `looksBalanced` 漏判混合括号不匹配**：深度计数让 `{ a = 1 ]` 从 1 减
  到 0 被误判为合法。改为**类型栈**校验，并纳入 `]`/`}` 开头的语法碎片。
- `plugin service stop` 的 fail-closed 告警在 `--json` 下丢失——告警改为
  无条件进 stderr（脚本收集 stderr 时也必须看到）。
- 前端运行时守卫补齐：v0.2.4 的 DOM stub 试验当时是**手工验证、未固化**，
  现在纯函数边界（`looksBalanced` / `humanBytes` / `humanDuration`）由 node
  动态执行断言；`plugin audit tail -f` 与 `status --watch` 的实时性由真实
  子进程 + 管道读取断言（不是杀进程后的退出冲刷）。
- **测试隔离（CI 抓到）**：`--instance` 补全的失败路径测试有两处缺陷——
  patch 目标错误（`from` 导入的引用被复制，patch `core.instance` 不影响
  调用点）且未隔离实例根（断言依赖"宿主默认目录为空"，本地假绿）。修复后
  以"默认根存在实例"的污染环境全量复跑（615 条）系统性排除同类依赖。

- **`typer.Exit` 的退出码在直接调用路径下被吞成 0**：`map_exceptions` 的兜底
  分支把 `Exit`（继承 `RuntimeError`、无 `format_message`）当作"未分类错误"，
  `doctor` 有 ERROR 时"应当退出 1"在测试/库调用路径下失效（真实 CLI 路径因为
  Click standalone 模式恰好正常）。现在统一转成 `SystemExit(code)`；测试基建
  同时改为接收 `standalone_mode=False` 的返回值（Click 把 `Exit` 作为返回值
  而非异常给出，忽略它就测不到真实退出码）。
- **审计相对路径的解析基准不一致**：`audit.path` 相对路径此前跟随进程 CWD
  ——`plugin serve` 手工前台运行写到当前目录、systemd 托管写到实例目录
  （WorkingDirectory），同一份策略因启动方式不同而"审计消失"。现在写入与读取
  （`plugin audit` / Web 审计视图）统一相对**策略文件所在目录**解析。
- **日志 tail 全量扫描**：`tail_lines` 用 ring buffer 顺序读整个文件——100MB
  日志每看一次读 100MB、Web 每 5 秒再读一遍。改为从文件尾按 64KiB 块反向回扫，
  读取量与"需要几行"相关（含跨块边界、多字节字符、无尾换行等边界处理）。
- **admin 全量翻页的静默截断**：`_paged` 丢弃服务端 `total`、翻页上限用尽时
  无从察觉。现在返回 `PageResult(total/truncated)`，CLI 与 Web 如实汇报。
- **`proxies --type` 拼错静默返回空表**：改为用法错误(2) 并列出合法类型集合
  （与 ADR-7"不猜测"一致）。
- pyproject 清理无效的 `PT011` ignore 项（`select` 未启用 PT 规则）。

### 测试与文档

- 新增 71 条测试（573 → 644；非契约 543 → 614），覆盖：日志反向读（含跨块
  边界与 I/O 量上界）、分页 total/截断、清理计数、审计读取（路径解析/统计/
  时间窗口/用户表上限/坏行）、策略级设置编辑、口令轮换、install 选项透传、
  `/api/doctor`、`/api/audit`、`/api/traffic/{name}`、traffic 服务端缓存、
  动作成功路径（restart/rollback）、日志参数边界、未规范化路径、`web serve`
  全链路（起真进程 → 登录 → SIGTERM 退出）。
- 设计方案：§18.2 API 表、§7.2 命令表同步；新增 §22 第九轮实现记录。

## [0.2.5] - 2026-09-17

完整卸载 + 在线一键安装：把此前分散的五段式清理（包管理器 / 三个 systemd
uninstall / 手工 `rm -rf` 数据目录）收敛为一条带护栏的命令；`install.sh`
支持 `curl … | bash` 直跑，并给"没有 Python"的服务器一条现成出路（uv）。

### 新增

**完整卸载**

- `frpsctl uninstall`：完整卸载当前实例的数据（配置 / state / 快照 / 插件
  策略与审计 / 口令文件 / 日志）、对应 unit 与共享二进制。默认先列出将删清单
  并要求确认；`--yes` 跳过；`--json` 下**必须**显式 `--yes`（破坏性操作不做
  隐式确认）。
- `--all`：卸载实例根下的全部实例；`--keep-data` / `--keep-bin` 各自保留数据
  维度（多实例机器上卸一个实例时二进制必须保留）。
- 安全护栏（延续 ADR-7 与"降级必须可见"）：
  - 运行中的实例默认**拒绝**卸载（退出码 11，数据分毫未动）；`--force` 先停止
    再卸载，systemd 托管同理；
  - 身份不明（FOREIGN）与状态文件损坏一律拒绝；
  - 多实例共用二进制却要删它 → 拒绝（`--all` 或 `--keep-bin` 才放行）；
  - 预检在最前：任一实例不允许，一个都不动；执行顺序为"停服务 → 清 unit →
    删数据 → 删共享二进制"，不会留下"服务在跑而数据已删"的状态；
  - unit 清理需要 root，权限不足时汇总为"未清理项"并给出可复制命令；
    `/var/log/frps` 与服务账户只提示、不代删。
- 三个 systemd 服务类拆出 `disable()`：多实例机器上卸载单个实例只停用本实例
  的 unit，**不删共享模板**（`frps@.service` 是全部实例共用的）。

**在线一键安装**

- `install.sh` 支持**管道直跑**：`curl -fsSL …/install.sh | bash`——没有脚本
  同目录的源码时自动下载 tarball 到数据目录；`FRPSCTL_INSTALL_REF` 可固定
  tag / 分支 / commit，`FRPSCTL_INSTALL_URL` 可换镜像或内网源。源码 URL 用
  GitHub 通用形态 `archive/<ref>.tar.gz`（`refs/heads/` 固定前缀会让 tag 404，
  实测发现）。
- **没有 Python 的出路**：`install.sh` 检测不到 Python ≥ 3.11 时直接打印 uv
  一键命令（uv 自带 Python，无需系统 Python）；"有 python3 但缺 venv/ensurepip"
  会在第一步被检测并给出 apt / uv 两条指引。
- README 安装章节重排为三条路线（uv 一键 → pipx/pip → 源码），新增
  "服务器没有 Python 怎么办"一节。

### 修复

**卸载（发布前回归 review）**

- 判定"是否需要停用 unit"曾看**共享模板文件是否存在**——模板是所有实例共用
  的，它存在不代表本实例用过 systemd，多实例场景会产生"需要 root 才能停用
  frps unit"的假警告（实测复现）。现改用 `is_active() / is_enabled()`（本实例
  unit 的真实状态），并把"需要 root"的警告文案区分为"停用 unit"与"停用并
  删除模板"两种动作。

**安装器（实测暴露）**

- **`--uninstall --prefix DIR` 会走安装路径**：MODE 与安装布局挤在同一变量，
  `--prefix` 把 `--uninstall` 覆盖成 custom——"装到自定义前缀后想卸载"这个
  帮助里演示的组合实际是坏的。现在 MODE / LAYOUT 分离。
- **`--uninstall` 被 Python 与源码检查挡住**：卸载本不需要它们；检查已移入
  安装路径（卸载分支之后）。
- **半残 venv 被"复用"**：venv 创建中断会留下"有 python 没有 pip"的目录，
  无条件复用让之后每次安装都在同一个坑里失败——现先验证 `import pip` 再复用，
  创建失败时清理目录并给出指引。

### 工程

- 新增 28 条测试（545 → 573；非契约 543）——集成 15（作用域规则、全部安全
  拒绝路径、`--force` 停止语义、预检与停止之间的竞态收紧、`--keep-*` 组合、
  共享模板假警告回归、外部配置提示）/ CLI 5（确认门、`--json` 约束、运行中拒绝 = 退出码 11）/ 单元 2
  （`disable()` 不碰模板、`is_enabled()` 三态）/ installer 6（`bash -n`、
  `--uninstall` 不需要 Python（真跑）、无 Python 时打印 uv 指引（真跑）、
  帮助 / README 承诺一致性、`--prefix` 不碰 MODE 的静态守卫）；
- release.yml 的 wheel 自检集合加入 `core/uninstall.py`；README 新增"卸载"
  教程与安装三路线；设计文档 §7.2 命令表、§12.1 交付形态、§21（第八轮）同步；
- 文档守卫的环境变量核对扩展覆盖 `install.sh`（它读 `FRPSCTL_INSTALL_*`——
  此前只扫 Python 会把这两个变量误判成"幽灵"）。

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
