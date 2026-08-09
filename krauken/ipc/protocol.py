"""IPC wire envelope. Newline-delimited JSON over Unix sockets rather than
HTTP-over-socket: the daemon exists specifically to stay simple enough to
trust (a bug in the most complex, most frequently-changing code must never
leave an actuator in an unsafe state), and pulling an ASGI/HTTP stack into
that process works directly against that goal. `krauken-ipc` (cli.py) is
the debuggability answer HTTP would otherwise have given us for free.

    -> {"v":1,"id":"a3f2","op":"system.ping","args":{},"deadline_ms":3000}
    <- {"v":1,"id":"a3f2","ok":true,"result":{"pong":true}}
    <- {"v":1,"id":"a3f2","ok":false,"error":{"code":"unknown_op","message":"...","details":{}}}
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

PROTOCOL_VERSION = 1
DEFAULT_DEADLINE_MS = 3000


@dataclass(frozen=True, slots=True)
class Request:
    id: str
    op: str
    args: Mapping[str, Any] = field(default_factory=dict)
    deadline_ms: int = DEFAULT_DEADLINE_MS

    @property
    def deadline_s(self) -> float:
        return self.deadline_ms / 1000.0


def new_request(op: str, args: Mapping[str, Any] | None = None, *, deadline_ms: int = DEFAULT_DEADLINE_MS) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "id": uuid.uuid4().hex[:8],
        "op": op,
        "args": dict(args or {}),
        "deadline_ms": deadline_ms,
    }


def decode_request(obj: dict[str, Any]) -> Request:
    return Request(
        id=obj["id"],
        op=obj["op"],
        args=obj.get("args", {}),
        deadline_ms=obj.get("deadline_ms", DEFAULT_DEADLINE_MS),
    )


def ok(request_id: str, result: Any) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result}


def err(request_id: str, code: str, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message, "details": dict(details or {})},
    }
