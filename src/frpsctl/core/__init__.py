"""frpsctl 核心层。

分层约束（§5）：`core/` **不打印任何东西**，只返回结构化结果或抛
`frpsctl.errors` 定义的异常。这条约束让 `--json` 与人读输出共享同一份逻辑。
"""

from __future__ import annotations
