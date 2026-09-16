# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

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
