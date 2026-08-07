"""Typed LLM errors for observable failures and rule-path fallback."""


class LLMError(Exception):
    """Base LLM failure."""

    def __init__(self, message: str, *, code: str = "LLM_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class LLMConfigError(LLMError):
    def __init__(self, message: str = "LLM provider not configured"):
        super().__init__(message, code="LLM_CONFIG")


class LLMTimeoutError(LLMError):
    def __init__(self, message: str = "LLM request timed out"):
        super().__init__(message, code="LLM_TIMEOUT")


class LLMAPIError(LLMError):
    def __init__(self, message: str, *, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message, code="LLM_API")


class LLMParseError(LLMError):
    def __init__(self, message: str = "Failed to parse LLM structured output"):
        super().__init__(message, code="LLM_PARSE")
