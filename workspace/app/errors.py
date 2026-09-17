"""统一的应用层异常，web 层据此映射 HTTP 状态码。"""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def not_found(message: str = "资源不存在") -> ApiError:
    return ApiError(404, "not_found", message)


def conflict(message: str, details: dict | None = None) -> ApiError:
    return ApiError(409, "conflict", message, details)
