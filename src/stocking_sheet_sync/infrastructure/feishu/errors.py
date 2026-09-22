class FeishuApiError(RuntimeError):
    def __init__(self, message: str, code: int, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class CopyRejected(RuntimeError):
    """飞书明确拒绝复制，可以重新触发。"""


class CopyOutcomeUnknown(RuntimeError):
    """复制结果无法确认，需要人工核对目标文件夹。"""
