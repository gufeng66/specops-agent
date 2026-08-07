from fastapi import HTTPException, status


class SpecOpsError(Exception):
    def __init__(self, message: str, code: str = "SPECOPS_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class ThreadBusyError(SpecOpsError):
    def __init__(self, thread_id: str):
        super().__init__(
            f"线程 {thread_id} 正在处理中，请等待当前流程完成后再试",
            "THREAD_BUSY",
        )
        self.thread_id = thread_id


def thread_busy_http(thread_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "THREAD_BUSY",
            "message": f"线程 {thread_id} 正在处理中，请等待当前流程完成后再试",
            "thread_id": thread_id,
        },
    )
