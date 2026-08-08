from __future__ import annotations


class ZangpuError(RuntimeError):
    pass


class ZangpuTransportError(ZangpuError):
    __slots__ = ("code", "retryable")

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(f"{code}: request transport failed")


class ZangpuProtocolError(ZangpuError):
    def __init__(self) -> None:
        super().__init__("PROTOCOL_ERROR: response contract validation failed")


class ZangpuAPIError(ZangpuError):
    __slots__ = ("code", "message", "operation_id", "request_id", "retryable", "status_code")

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        request_id: str | None,
        retryable: bool,
        operation_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retryable = retryable
        self.operation_id = operation_id
        super().__init__(f"{code}: {message}")
