from typing import Any


class Result:
    def __init__(self, success: bool, data: Any = None, message: str = "", code: int = 200):
        self.success = success
        self.data = data
        self.message = message
        self.code = code

    def to_dict(self):
        return {
            "success": self.success,
            "data": self.data,
            "message": self.message,
            "code": self.code
        }

    @staticmethod
    def success(data: Any = None, message: str = "操作成功", code: int = 200):
        return Result(True, data, message, code)

    @staticmethod
    def error(message: str = "操作失败", code: int = 400, data: Any = None):
        return Result(False, data, message, code)
