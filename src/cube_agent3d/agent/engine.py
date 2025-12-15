from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from typing import Any

from ..scene.state import SceneState


@dataclass
class EnginePolicy:
    """
    탐색/확장 정책:
    - 3D 공간에서 (x,z) 전방위 + (y) 높이까지 목표(target)를 스스로 선택
    - 목표 위치는 UI에서 '초록색 투명 상자'로 시각화 가능 (HINT_TARGET 액션)
    - 바닥 아래(y<0)는 금지. (추가로 큐브 스케일을 고려해 중심 y를 보정)
    - 일정 시간 동안 '위로만' 가거나 정체(stuck)로 판단되면 AUTO_RESET으로 상태 리셋
    """
    # 행동 제약
    max_actions_per_tick: int = 24
    max_spawn_per_tick: int = 1

    # 이동/탐색 파라미터
    step_len: float = 0.55
    target_hold_ticks: int = 18
    target_reach_eps: float = 0.85

    # 리셋(자기점검) 파라미터
    stuck_window: int = 64
    stuck_move_eps: float = 0.03
    up_only_dzdx_eps: float = 0.05
    up_only_dy_min: float = 0.08
    reset_cooldown: int = 60

    def __post_init__(self) -> None:
        self.rng = random.Random()
        self._target: list[float] | None = None
        self._target_tick: int = 0
        self._visited: list[tuple[float, float, float]] = []
        self._move_hist: deque[tuple[float, float, float]] = deque(maxlen=self.stuck_window)
        self._since_reset: int = 10_000

    def seed(self, seed: int) -> None:
        self.rng.seed(seed)

    def decide(self, st: SceneState) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []

        # 내부 카운터
        self._since_reset += 1

        cubes = list(st.cubes.values())
        if not cubes:
            return actions

        # ---- 자동 리셋 판단
        if self._should_auto_reset(st):
            self._since_reset = 0
            self._move_hist.clear()
            self._target = None
            self._visited.clear()
            return [{
                "type": "AUTO_RESET",
                "reason": "stuck_or_up_only",
            }]

        # ---- 타겟 갱신
        if self._target is None or (st.tick - self._target_tick) >= self.target_hold_ticks:
            self._target = self._pick_target(st)
            self._target_tick = st.tick

        # 현재 기준 큐브(조작 대상) 선택: 최근 생성된 큐브를 우선
        selected = self._pick_active_cube(st)

        # 타겟 도달 시 타겟 재생성
        if self._dist3(selected.pos, self._target) <= self.target_reach_eps:
            self._visited.append(tuple(self._target))
            self._target = self._pick_target(st)
            self._target_tick = st.tick

        # ---- UI 힌트(초록 투명 타겟)
        actions.append({
            "type": "HINT_TARGET",
            "pos": [float(self._target[0]), float(self._target[1]), float(self._target[2])],
            "scale": [1.02, 1.02, 1.02],
            "color": "#00ff66",
            "alpha": 0.20,
        })

        # ---- 복제(확장)
        spawned_id: str | None = None
        if len(st.cubes) < st.max_cubes and self.rng.random() < 0.28:
            dx, dy, dz = self._dir_step(selected.pos, self._target, step=1.05)
            new_id = st.next_id()
            actions.append({
                "type": "DUPLICATE",
                "source_id": selected.id,
                "new_id": new_id,
                "offset": [dx, max(0.0, dy), dz],
            })
            spawned_id = new_id

        # ---- 이동(전방위)
        move_id = spawned_id if spawned_id is not None else selected.id
        cur = st.cubes.get(move_id, selected)
        nx, ny, nz = self._step_toward(st, cur.pos, cur.scale, self._target, self.step_len)
        actions.append({
            "type": "MOVE",
            "id": move_id,
            "pos": [nx, ny, nz],
        })

        # 이동 히스토리(자기점검)
        self._move_hist.append((nx - cur.pos[0], ny - cur.pos[1], nz - cur.pos[2]))

        # ---- 회전/스케일/색상 (가벼운 다양화)
        if self.rng.random() < 0.9:
            yaw = (st.tick * 0.07) + self.rng.uniform(-0.25, 0.25)
            actions.append({
                "type": "ROTATE_YAW",
                "id": move_id,
                "yaw": float(yaw),
            })

        if self.rng.random() < 0.22:
            s = 0.85 + 0.35 * self.rng.random()
            sy = 0.95 + 0.15 * self.rng.random()
            actions.append({
                "type": "SCALE",
                "id": move_id,
                "scale": [float(s), float(sy), float(s)],
            })

        if spawned_id is not None or self.rng.random() < 0.14:
            actions.append({
                "type": "SET_COLOR",
                "id": move_id,
                "color": self._rand_color(),
            })

        return actions[: self.max_actions_per_tick]

    # ---------- 내부 유틸 ----------

    def _pick_active_cube(self, st: SceneState):
        best = None
        best_int = -1
        for k, v in st.cubes.items():
            try:
                i = int(k)
            except Exception:
                i = -1
            if i > best_int:
                best_int = i
                best = v
        if best is not None:
            return best
        return self.rng.choice(list(st.cubes.values()))

    def _pick_target(self, st: SceneState) -> list[float]:
        b = st.bounds

        best: list[float] | None = None
        best_score = -1e18

        pad = 1.2
        xmin, xmax = b.x_min + pad, b.x_max - pad
        zmin, zmax = b.z_min + pad, b.z_max - pad
        ymin = max(b.y_min + 0.55, 0.55)
        ymax = max(ymin + 0.1, min(b.y_max - 0.55, b.y_max * 0.65))

        cubes = list(st.cubes.values())

        for _ in range(18):
            x = self.rng.uniform(xmin, xmax)
            z = self.rng.uniform(zmin, zmax)
            y = self.rng.triangular(ymin, ymax, ymin + (ymax - ymin) * 0.35)

            cand = [x, y, z]

            if self._visited:
                dv = min(self._dist3(cand, list(v)) for v in self._visited[-120:])
            else:
                dv = 5.0

            if cubes:
                dc = min(self._dist3(cand, c.pos) for c in cubes)
            else:
                dc = 5.0

            py = max(0.0, (y - (ymin + ymax) * 0.5)) * 0.35

            score = (dv * 0.75) + (dc * 0.35) - py
            if score > best_score:
                best_score = score
                best = cand

        if best is None:
            best = [0.0, ymin, 0.0]

        self._visited.append(tuple(best))
        return [float(best[0]), float(best[1]), float(best[2])]

    def _step_toward(self, st: SceneState, pos: list[float], scale: list[float], target: list[float], step: float) -> tuple[float, float, float]:
        dx, dy, dz = self._dir_step(pos, target, step=step)
        nx = pos[0] + dx
        ny = pos[1] + dy
        nz = pos[2] + dz

        sy = float(scale[1]) if scale else 1.0
        min_center_y = max(st.bounds.y_min + 0.5 * sy, 0.5 * sy)
        if ny < min_center_y:
            ny = min_center_y

        nx = max(st.bounds.x_min, min(st.bounds.x_max, nx))
        ny = max(st.bounds.y_min, min(st.bounds.y_max, ny))
        nz = max(st.bounds.z_min, min(st.bounds.z_max, nz))

        return float(nx), float(ny), float(nz)

    def _dir_step(self, pos: list[float], target: list[float], step: float) -> tuple[float, float, float]:
        vx = target[0] - pos[0]
        vy = target[1] - pos[1]
        vz = target[2] - pos[2]
        d = math.sqrt(vx * vx + vy * vy + vz * vz)
        if d <= 1e-9:
            return 0.0, 0.0, 0.0
        s = min(1.0, step / d)
        return vx * s, vy * s, vz * s

    def _dist3(self, a: list[float], b: list[float]) -> float:
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        dz = a[2] - b[2]
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def _should_auto_reset(self, st: SceneState) -> bool:
        if self._since_reset < self.reset_cooldown:
            return False

        if len(self._move_hist) < max(12, self.stuck_window // 3):
            return False

        avg_move = sum(abs(dx) + abs(dy) + abs(dz) for dx, dy, dz in self._move_hist) / max(1, len(self._move_hist))
        if avg_move < self.stuck_move_eps:
            return True

        avg_dxz = sum(abs(dx) + abs(dz) for dx, dy, dz in self._move_hist) / max(1, len(self._move_hist))
        avg_dy = sum(dy for dx, dy, dz in self._move_hist) / max(1, len(self._move_hist))
        if avg_dxz < self.up_only_dzdx_eps and avg_dy > self.up_only_dy_min:
            return True

        return False

    def _rand_color(self) -> str:
        h = self.rng.random()
        s = 0.55 + 0.35 * self.rng.random()
        v = 0.78 + 0.18 * self.rng.random()
        r, g, b = self._hsv_to_rgb(h, s, v)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _hsv_to_rgb(self, h: float, s: float, v: float) -> tuple[int, int, int]:
        i = int(h * 6.0) % 6
        f = (h * 6.0) - i
        p = v * (1.0 - s)
        q = v * (1.0 - f * s)
        t = v * (1.0 - (1.0 - f) * s)

        if i == 0:
            r, g, b = v, t, p
        elif i == 1:
            r, g, b = q, v, p
        elif i == 2:
            r, g, b = p, v, t
        elif i == 3:
            r, g, b = p, q, v
        elif i == 4:
            r, g, b = t, p, v
        else:
            r, g, b = v, p, q

        return int(r * 255), int(g * 255), int(b * 255)
