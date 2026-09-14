"""LLM 失败类型。节点捕获后回退规则或返回空建议，不把异常冒泡成整图失败。"""


class LLMError(Exception):
    """LLM 失败基类。code 写入日志，节点据此回退，不把异常冒成 500。"""

    def __init__(self, message: str, *, code: str = "LLM_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class LLMConfigError(LLMError):
    """未配置 provider / 空 Key。"""
    def __init__(self, message: str = "LLM provider not configured"):
        super().__init__(message, code="LLM_CONFIG")


class LLMTimeoutError(LLMError):
    """墙钟超时（wait_for）或 httpx 读超时。"""
    def __init__(self, message: str = "LLM request timed out"):
        super().__init__(message, code="LLM_TIMEOUT")


class LLMAPIError(LLMError):
    """HTTP ≥400 或传输错误。"""
    def __init__(self, message: str, *, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message, code="LLM_API")


class LLMParseError(LLMError):
    """响应不是 JSON，或对不上 Pydantic schema。"""
    def __init__(self, message: str = "Failed to parse LLM structured output"):
        super().__init__(message, code="LLM_PARSE")
