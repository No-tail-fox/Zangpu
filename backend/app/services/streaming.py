from __future__ import annotations

import json

from pydantic import ValidationError

from backend.app.integrations.bifrost.models import BifrostProtocolError, ChatUsage

MAX_SSE_LINE_BYTES = 1024 * 1024


class OpenAIStreamDecoder:
    __slots__ = ("_buffer", "_data_lines", "_done", "_usage")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._data_lines: list[bytes] = []
        self._done = False
        self._usage: ChatUsage | None = None

    @property
    def usage(self) -> ChatUsage | None:
        return self._usage

    @property
    def done(self) -> bool:
        return self._done

    def feed(self, chunk: bytes) -> list[bytes]:
        if not isinstance(chunk, bytes) or not chunk or self._done:
            raise BifrostProtocolError
        self._buffer.extend(chunk)
        if len(self._buffer) > MAX_SSE_LINE_BYTES and b"\n" not in self._buffer:
            raise BifrostProtocolError

        frames: list[bytes] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if len(line) > MAX_SSE_LINE_BYTES:
                raise BifrostProtocolError
            if self._done and line:
                raise BifrostProtocolError
            if not line:
                frame = self._finish_event()
                if frame is not None:
                    frames.append(frame)
                continue
            if line.startswith(b":"):
                continue
            if not line.startswith(b"data:"):
                raise BifrostProtocolError
            value = line[5:]
            if value.startswith(b" "):
                value = value[1:]
            self._data_lines.append(value)
        return frames

    def finish(self) -> None:
        if self._buffer or self._data_lines or not self._done or self._usage is None:
            raise BifrostProtocolError

    def _finish_event(self) -> bytes | None:
        if not self._data_lines:
            return None
        data = b"\n".join(self._data_lines)
        self._data_lines.clear()
        if data == b"[DONE]":
            if self._done or self._usage is None:
                raise BifrostProtocolError
            self._done = True
            return b"data: [DONE]\n\n"
        if self._done:
            raise BifrostProtocolError

        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, ValueError):
            raise BifrostProtocolError from None
        if (
            not isinstance(payload, dict)
            or payload.get("object") != "chat.completion.chunk"
            or not isinstance(payload.get("id"), str)
            or not 1 <= len(payload["id"]) <= 128
            or not isinstance(payload.get("model"), str)
            or not 1 <= len(payload["model"]) <= 255
            or not isinstance(payload.get("choices"), list)
            or len(payload["choices"]) > 128
            or "error" in payload
        ):
            raise BifrostProtocolError

        usage_payload = payload.get("usage")
        if usage_payload is not None:
            try:
                usage = ChatUsage.model_validate(usage_payload)
            except ValidationError:
                raise BifrostProtocolError from None
            if self._usage is not None:
                raise BifrostProtocolError
            self._usage = usage
        if not payload["choices"] and usage_payload is None:
            raise BifrostProtocolError
        return b"data: " + data + b"\n\n"
