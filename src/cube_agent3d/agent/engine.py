
from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from typing import Any, Iterable

from ..scene.state import SceneState


@dataclass(frozen=True)
class GhostHint:
    """Client-side visualization hint (green transparent target marker)."""
    pos: list[float]
    scale: list[float]
    ttl_ms: int = 900  # how long the UI should keep the ghost

    def to_dict(self) -> dict[str, Any]:
        return {"pos": self.pos, "scale": self.scale, "ttl_ms": self.ttl_ms}


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _ground_center_y(scale_y: float) -> float:
    # Ground plane is y=0. Cube must not penetrate the floor.
    # For an axis-aligned box, center_y >= half-height.
    return 0.5 * max(0.01, scale_y)


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    rr = int(_clamp(r, 0.0, 1.0) * 255.0 + 0.5)
    gg = int(_clamp(g, 0.0, 1.0) * 255.0 + 0.5)
    bb = int(_clamp(b, 0.0, 1.0) * 255.0 + 0.5)
    return f"#{rr:02x}{gg:02x}{bb:02x}"


def _auto_color(x: float, y: float, z: float, tick: int) -> str:
    # Stable-but-dynamic color: position + tick -> HSV -> RGB.
    hue = (0.33 + (x * 0.015) + (z * 0.012) + (tick * 0.006)) % 1.0
    sat = 0.70
    val = 0.98 if y < 10.0 else 0.90
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return _rgb_to_hex(r, g, b)


def _half_extents(scale: list[float]) -> tuple[float, float, float]:
    return (0.5 * scale[0], 0.5 * scale[1], 0.5 * scale[2])


def _aabb_overlap(a_pos: list[float], a_scale: list[float], b_pos: list[float], b_scale: list[float]) -> bool:
    ax, ay, az = a_pos
    bx, by, bz = b_pos
    ahx, ahy, ahz = _half_extents(a_scale)
    bhx, bhy, bhz = _half_extents(b_scale)

    # Slightly shrink overlap threshold to reduce "sticky" behavior.
    eps = 0.02
    return (
        abs(ax - bx) < (ahx + bhx - eps)
        and abs(ay - by) < (ahy + bhy - eps)
        and abs(az - bz) < (ahz + bhz - eps)
    )


def _is_free(st: SceneState, pos: list[float], scale: list[float], ignore_id: str | None = None) -> bool:
    for c in st.cubes.values():
        if ignore_id is not None and c.id == ignore_id:
            continue
        if _aabb_overlap(pos, scale, c.pos, c.scale):
            return False
    return True


def _dirs_26() -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx == 0 and dy == 0 and dz == 0:
                    continue
                out.append((dx, dy, dz))
    return out


_DIRS = _dirs_26()


class EnginePolicy:
    """
    - 전체 방향(±X/±Y/±Z)으로 탐색/이동/복사
    - 바닥 아래로 내려가지 않음: center_y >= 0.5 * scale_y
    - '가고자 하는 위치'를 ACTION_BATCH 액션에 ghost 힌트로 포함 (UI에서 초록 투명으로 표시)
    """

    def __init__(
        self,
        *,
        max_actions_per_tick: int = 18,
        max_spawn_per_tick: int = 2,
        base_step: float = 1.10,
        ghost_ttl_ms: int = 900,
    ) -> None:
        self.max_actions_per_tick = max_actions_per_tick
        self.max_spawn_per_tick = max_spawn_per_tick
        self.base_step = base_step
        self.ghost_ttl_ms = ghost_ttl_ms

    def decide(self, st: SceneState) -> list[dict[str, Any]]:
        if not st.cubes:
            return []

        rng = st.rng

        # Frontier(새로 생긴 큐브) 쪽을 더 자주 선택해서 확장성을 올립니다.
        cubes = list(st.cubes.values())
        cubes.sort(key=lambda c: c.age)  # smaller age = newer
        frontier = cubes[: max(1, min(len(cubes), 8))]
        active = rng.choice(frontier)

        actions: list[dict[str, Any]] = []

        step = self.base_step * max(active.scale[0], active.scale[2], 1.0)

        cand_best: tuple[float, list[float]] | None = None

        # 여러 후보를 탐색해서 "비어있고 확장에 유리한" 곳을 고릅니다.
        for _ in range(28):
            dx, dy, dz = rng.choice(_DIRS)

            # 바닥 근처에서 아래로 더 내려가는 시도는 약화
            if active.pos[1] <= _ground_center_y(active.scale[1]) + 0.05 and dy < 0:
                dy = 0

            tx = active.pos[0] + dx * step
            ty = active.pos[1] + dy * step
            tz = active.pos[2] + dz * step

            # 바닥 규칙 + bounds 클램프
            ty = max(ty, _ground_center_y(active.scale[1]))
            tx = _clamp(tx, st.bounds.x_min, st.bounds.x_max)
            ty = _clamp(ty, st.bounds.y_min, st.bounds.y_max)
            tz = _clamp(tz, st.bounds.z_min, st.bounds.z_max)

            target = [tx, ty, tz]

            # 점유/충돌이면 탈락
            if not _is_free(st, target, active.scale, ignore_id=active.id):
                continue

            # 휴리스틱: 원점에서 멀어질수록(확장) + 약간의 높이 + 랜덤
            d = math.sqrt(tx * tx + tz * tz)
            score = (d * 0.85) + (ty * 0.22) + (rng.random() * 0.25)

            if cand_best is None or score > cand_best[0]:
                cand_best = (score, target)

        if cand_best is None:
            # 막혀있으면 최소한 UI가 멈춘 것처럼 보이지 않게 회전/색만 갱신
            yaw = rng.uniform(-math.pi, math.pi)
            actions.append({"type": "ROTATE_YAW", "id": active.id, "yaw": yaw})
            actions.append({"type": "SET_COLOR", "id": active.id, "color": _auto_color(active.pos[0], active.pos[1], active.pos[2], st.tick)})
            return actions[: self.max_actions_per_tick]

        target = cand_best[1]
        ghost = GhostHint(pos=target, scale=active.scale[:], ttl_ms=self.ghost_ttl_ms).to_dict()

        can_spawn = len(st.cubes) < st.max_cubes
        do_spawn = can_spawn and (rng.random() < 0.62)

        if do_spawn:
            new_id = self._next_id(st.cubes.keys())
            actions.append({
                "type": "DUPLICATE",
                "source_id": active.id,
                "new_id": new_id,
                "offset": [target[0] - active.pos[0], target[1] - active.pos[1], target[2] - active.pos[2]],
                "ghost": ghost,
            })
            actions.append({"type": "SET_COLOR", "id": new_id, "color": _auto_color(target[0], target[1], target[2], st.tick)})
        else:
            actions.append({"type": "MOVE", "id": active.id, "pos": target, "ghost": ghost})
            actions.append({"type": "SET_COLOR", "id": active.id, "color": _auto_color(target[0], target[1], target[2], st.tick)})

        if rng.random() < 0.18 and len(actions) < self.max_actions_per_tick:
            yaw = rng.uniform(-math.pi, math.pi)
            actions.append({"type": "ROTATE_YAW", "id": active.id, "yaw": yaw})

        # 스케일은 가끔만 + 바닥 침투 방지용 MOVE 보정
        if rng.random() < 0.14 and len(actions) + 2 <= self.max_actions_per_tick:
            factor = rng.uniform(0.78, 1.28)
            ns = [
                _clamp(active.scale[0] * factor, 0.40, 2.50),
                _clamp(active.scale[1] * factor, 0.40, 2.50),
                _clamp(active.scale[2] * factor, 0.40, 2.50),
            ]
            actions.append({"type": "SCALE", "id": active.id, "scale": ns})
            miny = _ground_center_y(ns[1])
            if active.pos[1] < miny:
                actions.append({"type": "MOVE", "id": active.id, "pos": [active.pos[0], miny, active.pos[2]]})

        return actions[: self.max_actions_per_tick]

    @staticmethod
    def _next_id(ids: Iterable[str]) -> str:
        m = 1
        for s in ids:
            try:
                m = max(m, int(s))
            except Exception:
                continue
        return str(m + 1)
