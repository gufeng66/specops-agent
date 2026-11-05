"""领域异常。线程忙碌用 409，避免同一 thread 并发跑两遍图。"""

from fastapi import HTTPException, status


class SpecOpsError(Exception):
    """带 code 的领域异常，HTTP 层可映射成 JSON detail。"""
    def __init__(self, message: str, code: str = "SPECOPS_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class ThreadBusyError(SpecOpsError):
    """同一 thread 已在 planning / awaiting_review，禁止并发开跑。"""
    def __init__(self, thread_id: str):
        super().__init__(
            f"线程 {thread_id} 正在处理中，请等待当前流程完成后再试",
            "THREAD_BUSY",
        )
        self.thread_id = thread_id


def conflict_http(code: str, message: str, **extra: object) -> HTTPException:
    """409。认领冲突、执行中、中断不匹配都走这里。"""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message, **extra},
    )


def thread_busy_http(thread_id: str) -> HTTPException:
    """409 + THREAD_BUSY。前端 sse.ts 用 detail.message 展示中文。"""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "THREAD_BUSY",
            "message": f"线程 {thread_id} 正在处理中，请等待当前流程完成后再试",
            "thread_id": thread_id,
        },
    )
