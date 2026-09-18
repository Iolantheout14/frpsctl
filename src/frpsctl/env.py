"""环境变量常量表（0.3.0）。

**为什么要有这张表**：环境变量此前散落在 8 个模块的 `os.environ.get(...)` 里，
"有哪些变量、什么语义"只能靠 grep 与 README 对账（文档审计抓到过漂移）。
这里集中定义后：

- `frpsctl capabilities --json` 与 README 的环境变量表**同源生成**；
- 新增变量时，文档生成回路（`frpsctl.docs`）与 CI 对账会提醒同步。
"""

from __future__ import annotations

__all__ = ["ENV_VARS"]

#: 变量名 → 一句话语义。顺序即文档展示顺序（常用的靠前）。
ENV_VARS: dict[str, str] = {
    "FRPSCTL_INSTANCE": "实例名（`--instance` 的默认值，默认 default）",
    "FRPSCTL_DATA_HOME": "数据根目录（`bin/` 与 `instances/` 的父目录）",
    "FRPSCTL_ROOT": "实例根目录（默认 `<data_home>/instances`）",
    "FRPSCTL_MIRROR": "下载镜像列表（逗号分隔；`install --mirror` 优先）",
    "FRPSCTL_ADMIN_PASSWORD": "dashboard 口令（`--admin-password` 优先，其次配置文件）",
    "FRPSCTL_WEB_PASSWORD": "Web 管理台口令（`--password` 优先）",
    "FRPSCTL_PLUGIN_POLICY": "插件策略文件路径（`--policy` 优先）",
    "FRPSCTL_TRACEBACK": "任何非空值：未分类错误时打印完整回溯",
    "FRPSCTL_INSTALL_REF": "install.sh：源码 ref（tag/分支，默认 main）",
    "FRPSCTL_INSTALL_URL": "install.sh：源码 tarball 地址（镜像/离线内网）",
    "FRPSCTL_TEST_BINARY": "测试：真实 frps 路径（契约层）",
    "FRPSCTL_TEST_FRPC": "测试：真实 frpc 路径（插件契约层）",
    "XDG_DATA_HOME": "XDG 数据目录（未设 `FRPSCTL_DATA_HOME` 时使用）",
    "EDITOR": "`config edit` 使用的编辑器（`VISUAL` 次之，默认 vi）",
}
