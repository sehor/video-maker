from typing import Any


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def not_found(resource: str) -> ApiError:
    return ApiError(404, f"{resource.upper()}_NOT_FOUND", "资源不存在或无权访问")
