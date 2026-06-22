import enum
from typing import Any


class ErrorType(enum.Enum):
    NOT_FOUND = "NOT_FOUND"
    INTERNAL = "INTERNAL"
    VALIDATION = "VALIDATION"
    AUTH_FAILED = "AUTH_FAILED"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    RECORDING_ACTIVE = "RECORDING_ACTIVE"
    RECORDING_INACTIVE = "RECORDING_INACTIVE"
    NO_DEVICE = "NO_DEVICE"
    API_ERROR = "API_ERROR"
    CLAUDE_UNAVAILABLE = "CLAUDE_UNAVAILABLE"


def _ok(data: Any = None) -> dict:
    return {"success": True, "data": data, "error": None}


def _err(type: ErrorType, message: str) -> dict:
    return {"success": False, "data": None, "error": {"type": type.value, "message": message}}
