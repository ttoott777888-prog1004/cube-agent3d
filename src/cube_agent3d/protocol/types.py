from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict


MsgType = Literal[
    "HELLO",
    "UI_START",
    "UI_STOP",
    "SERVER_STATUS",
    "STATE_SNAPSHOT",
    "ACTION_BATCH",
    "LOG_EVENT",
    "RESET",
]


class ClientMsg(TypedDict):
    type: MsgType
    payload: dict[str, Any]


class ServerMsg(TypedDict):
    type: MsgType
    payload: dict[str, Any]


@dataclass(frozen=True)
class Bounds:
    x_min: float = -20.0
    x_max: float = 20.0
    y_min: float = 0.0
    y_max: float = 20.0
    z_min: float = -20.0
    z_max: float = 20.0
