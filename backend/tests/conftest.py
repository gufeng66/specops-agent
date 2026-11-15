"""测试进程级隔离：必须在任何业务模块导入前把 LLM 关掉。

`app.llm.client` 的全局 llm_client 是模块级单例，settings 也是导入期创建的。
只靠 fixture 里改 settings 不够——所以这里先设环境变量，再让 pydantic-settings
在创建 Settings 时读到它们。真实模型测试必须用 `-m real_llm` 并显式覆盖。
"""

from __future__ import annotations

import os

os.environ.setdefault("LLM_PROVIDER", "none")
os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ["APP_ENV"] = "test"
# 测试不写真实库文件；需要落盘的用例自己在 tmp_path 里传 database_path。
os.environ.setdefault("CHECKPOINT_MODE", "memory")
