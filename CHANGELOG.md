# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

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
