from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..scene.state import SceneState


def _hex_color_from_height(y: float, y_max: float = 20.0) -> str:
    t = 0.0 if y_max <= 1e-6 else max(0.0, min(1.0, y / y_max))
    r = int(120 + 120 * t)
    g = int(211 - 160 * t)
    b = int(252 - 80 * t)
    return f"#{r:02x}{g:02x}{b:02x}"


@dataclass
class TowerPolicy:
    max_spawn_per_tick: int = 2
    max_actions_per_tick: int = 24

    def decide(self, st: SceneState) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []

        cubes = list(st.cubes.values())
        if not cubes:
            return actions

        top = max(cubes, key=lambda c: c.pos[1])
        base_id = "1" if "1" in st.cubes else top.id

        spawn_count = 0
        next_id_int = self._next_id_int(st)

        want_more = len(st.cubes) < min(st.max_cubes, 80)

        if want_more and spawn_count < self.max_spawn_per_tick:
            layer = len(st.cubes)
            ang = (st.tick * 0.15) + (layer * 0.35)
            radius = 0.25 + 0.15 * math.sin(st.tick * 0.07)
            nx = radius * math.cos(ang)
            nz = radius * math.sin(ang)
            ny = min(st.bounds.y_max, top.pos[1] + 1.05)

            new_id = str(next_id_int)
            next_id_int += 1
            actions.append({
                "type": "DUPLICATE",
                "source_id": base_id,
                "new_id": new_id,
                "offset": [nx, ny - st.cubes[base_id].pos[1], nz],
            })
            spawn_count += 1

        budget = self.max_actions_per_tick - len(actions)
        if budget <= 0:
            return actions

        selected_id = top.id

        for c in cubes:
            if budget <= 0:
                break

            tx = c.pos[0] * 0.92
            tz = c.pos[2] * 0.92
            ty = c.pos[1]
            actions.append({
                "type": "MOVE",
                "id": c.id,
                "pos": [tx, ty, tz],
            })
            budget -= 1
            if budget <= 0:
                break

            yaw = (st.tick * 0.08) + (c.pos[1] * 0.12)
            actions.append({
                "type": "ROTATE_YAW",
                "id": c.id,
                "yaw": yaw,
            })
            budget -= 1
            if budget <= 0:
                break

            t = max(0.0, min(1.0, c.pos[1] / max(1e-6, st.bounds.y_max)))
            s = 1.0 - 0.35 * t
            sy = 1.0
            actions.append({
                "type": "SCALE",
                "id": c.id,
                "scale": [s, sy, s],
            })
            budget -= 1
            if budget <= 0:
                break

            if c.id == selected_id:
                col = "#fbbf24"
            else:
                col = _hex_color_from_height(c.pos[1], st.bounds.y_max)
            actions.append({
                "type": "SET_COLOR",
                "id": c.id,
                "color": col,
            })
            budget -= 1

        return actions

    def _next_id_int(self, st: SceneState) -> int:
        mx = 0
        for k in st.cubes.keys():
            try:
                mx = max(mx, int(k))
            except Exception:
                continue
        return mx + 1
