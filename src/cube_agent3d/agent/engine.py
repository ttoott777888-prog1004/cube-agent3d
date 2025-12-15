from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Tuple

from ..protocol.types import Bounds
from ..scene.state import SceneState


def _iter_cubes(state: SceneState) -> list[Any]:
    cubes = getattr(state, "cubes", [])
    if isinstance(cubes, dict):
        return list(cubes.values())
    if isinstance(cubes, (list, tuple)):
        return list(cubes)
    try:
        return list(cubes)  # type: ignore[arg-type]
    except Exception:
        return []


def _cube_id(c: Any) -> str:
    if isinstance(c, dict):
        return str(c.get("id", ""))
    return str(getattr(c, "id", ""))


def _cube_pos(c: Any) -> Tuple[int, int, int]:
    if isinstance(c, dict):
        p = c.get("pos", [0, 0, 0])
    else:
        p = getattr(c, "pos", [0, 0, 0])
    if isinstance(p, (list, tuple)) and len(p) >= 3:
        x, y, z = p[0], p[1], p[2]
    else:
        x, y, z = 0, 0, 0
    return int(round(float(x))), int(round(float(y))), int(round(float(z)))


@dataclass
class PolicyConfig:
    probe_n: int = 32

    # 생성 위주: DUPLICATE 우선 확률
    dup_prob: float = 0.92
    # top-k 후보 중 랜덤 선택(한 방향으로 굳는 현상 방지)
    pick_top_k: int = 10

    # 정체/이상 감지(“스스로 이상하다”)
    stuck_window: int = 120
    # stuck_window 동안 cube_count 증가가 없으면 “정체”
    no_growth_reset: bool = True
    # head가 거의 안 움직이고(제자리) 성장도 없으면 “정체”
    head_move_eps: float = 0.001
    reset_cooldown: int = 80  # 리셋 직후 즉시 재리셋 방지

    # 색상
    color_on_dup: bool = True
    color_on_move: bool = False


class EnginePolicy:
    """
    목표:
    - “이동”보다 “생성(확장)” 우선
    - 26방향 후보 생성 + 바닥(y<0) 금지 + 충돌 금지
    - y>0은 아래 지지(support) 있는 후보를 강하게 선호 (공중부양 확장 방지)
    - 후보(probes)를 프론트에 전달하여 초록 반투명 상자로 표시
    - 정체/이상 감지 시 EPISODE_RESET 반환 (서버에서 자동 리셋 처리)
    """

    def __init__(self, rng: random.Random, cfg: PolicyConfig | None = None) -> None:
        self.rng = rng
        self.cfg = cfg or PolicyConfig()

        self._last_probes: list[dict[str, Any]] = []
        self._visited: set[Tuple[int, int, int]] = set()

        self._since_reset: int = 10_000
        self._cube_count_hist: deque[int] = deque(maxlen=self.cfg.stuck_window)
        self._head_pos_hist: deque[Tuple[int, int, int]] = deque(maxlen=self.cfg.stuck_window)

        # “학습” (가벼운 강화): 26방향 오프셋 가중치
        self._dir_w: dict[Tuple[int, int, int], float] = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if dx == dy == dz == 0:
                        continue
                    self._dir_w[(dx, dy, dz)] = 0.0

    def on_episode_reset(self) -> None:
        # 학습 가중치는 유지(=리셋해도 “경험”은 남김), 방문 집합/히스토리는 정리
        self._since_reset = 0
        self._last_probes = []
        self._visited.clear()
        self._cube_count_hist.clear()
        self._head_pos_hist.clear()
        # 너무 한쪽으로 쏠리지 않게 약간 감쇠
        for k in list(self._dir_w.keys()):
            self._dir_w[k] *= 0.92

    def probes(self) -> list[dict[str, Any]]:
        return self._last_probes

    def observe(self, applied: list[dict[str, Any]]) -> None:
        # 적용된 행동 기준으로 방향 가중치 업데이트(아주 단순)
        for a in applied:
            t = a.get("type")
            if t == "DUPLICATE":
                off = a.get("offset", [0, 0, 0])
                if isinstance(off, (list, tuple)) and len(off) >= 3:
                    dx, dy, dz = int(round(float(off[0]))), int(round(float(off[1]))), int(round(float(off[2])))
                    key = (max(-1, min(1, dx)), max(-1, min(1, dy)), max(-1, min(1, dz)))
                    if key in self._dir_w:
                        self._dir_w[key] += 0.15
            elif t == "MOVE":
                self._dir_w[(0, 0, 1)] = self._dir_w.get((0, 0, 1), 0.0) + 0.01

        # 폭주 방지 클램프
        for k in list(self._dir_w.keys()):
            if self._dir_w[k] > 2.5:
                self._dir_w[k] = 2.5
            if self._dir_w[k] < -2.5:
                self._dir_w[k] = -2.5

    def decide(self, state: SceneState, *, episode_cap: int, max_cubes: int) -> list[dict[str, Any]]:
        self._since_reset += 1

        cubes = _iter_cubes(state)
        if not cubes:
            self._last_probes = []
            return []

        cube_count = len(cubes)
        if cube_count >= episode_cap:
            return [{"type": "EPISODE_RESET", "reason": "cap_reached"}]

        # head(확장 여지 많은 큐브) 선택
        occ: set[Tuple[int, int, int]] = set(_cube_pos(c) for c in cubes)

        def frontier_score(c: Any) -> int:
            x, y, z = _cube_pos(c)
            score = 0
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        if dx == dy == dz == 0:
                            continue
                        nx, ny, nz = x + dx, y + dy, z + dz
                        if ny < 0:
                            continue
                        if (nx, ny, nz) in occ:
                            continue
                        score += 1
            return score

        head = max(cubes, key=frontier_score)
        head_id = _cube_id(head)
        hx, hy, hz = _cube_pos(head)

        # stuck/이상 감지용 히스토리 업데이트
        self._cube_count_hist.append(cube_count)
        self._head_pos_hist.append((hx, hy, hz))
        if self._should_reset():
            return [{"type": "EPISODE_RESET", "reason": "stuck_or_no_growth"}]

        # Bounds
        b = getattr(state, "bounds", Bounds())
        x_min = int(getattr(b, "x_min", -60))
        x_max = int(getattr(b, "x_max", 60))
        y_min = int(max(0, int(getattr(b, "y_min", 0))))
        y_max = int(getattr(b, "y_max", 120))
        z_min = int(getattr(b, "z_min", -60))
        z_max = int(getattr(b, "z_max", 60))

        # 후보 생성 및 스코어링
        cand: list[Tuple[float, Tuple[int, int, int], Tuple[int, int, int]]] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if dx == dy == dz == 0:
                        continue
                    nx, ny, nz = hx + dx, hy + dy, hz + dz

                    if ny < 0:
                        continue
                    if nx < x_min or nx > x_max or nz < z_min or nz > z_max:
                        continue
                    if ny < y_min or ny > y_max:
                        continue
                    if (nx, ny, nz) in occ:
                        continue

                    support = 1 if ny == 0 else (1 if (nx, ny - 1, nz) in occ else 0)
                    novelty = 1 if (nx, ny, nz) not in self._visited else 0

                    # 생성 목적: “유효한 확장 + 다양성 + 퍼짐”을 우선
                    spread = (abs(nx) + abs(nz)) * 0.08
                    height = ny * 0.03

                    w = self._dir_w.get((dx, dy, dz), 0.0)

                    score = (support * 6.0) + (novelty * 2.2) + spread + height + (w * 0.6)
                    score += self.rng.random() * 0.02

                    # 공중 확장은 “보이기는 하되” 선택은 거의 안 되게
                    if ny > 0 and support == 0:
                        score -= 12.0

                    cand.append((score, (nx, ny, nz), (dx, dy, dz)))

        cand.sort(key=lambda t: t[0], reverse=True)

        # probes: 상위 N개를 UI에 전달(초록 반투명 박스)
        probes: list[dict[str, Any]] = []
        for _, (px, py, pz), _ in cand[: self.cfg.probe_n]:
            probes.append({
                "pos": [px, py, pz],
                "scale": [1.02, 1.02, 1.02],
                "color": "#00ff66",
                "alpha": 0.20,
            })
        self._last_probes = probes

        if not cand:
            return [{"type": "EPISODE_RESET", "reason": "no_candidates"}]

        top_k = min(self.cfg.pick_top_k, len(cand))
        _, (tx, ty, tz), (dx, dy, dz) = cand[self.rng.randrange(0, top_k)]

        # “생성(복제)” 우선
        can_dup = cube_count < max_cubes
        if can_dup and self.rng.random() < self.cfg.dup_prob:
            new_id = f"c{getattr(state, 'tick', 0)}_{self.rng.randrange(1_000_000)}"
            self._visited.add((tx, ty, tz))

            out: list[dict[str, Any]] = [{
                "type": "DUPLICATE",
                "source_id": head_id,
                "new_id": new_id,
                "offset": [dx, dy, dz],
            }]

            if self.cfg.color_on_dup:
                out.append({
                    "type": "SET_COLOR",
                    "id": new_id,
                    "color": self._rand_color(),
                })

            # head도 가끔 따라가서 다음 확장 방향을 바꾸게
            if self.rng.random() < 0.25:
                out.append({
                    "type": "MOVE",
                    "id": head_id,
                    "pos": [tx, ty, tz],
                })

            return out

        # 생성이 불가/억제된 경우에만 MOVE
        self._visited.add((tx, ty, tz))
        out2: list[dict[str, Any]] = [{
            "type": "MOVE",
            "id": head_id,
            "pos": [tx, ty, tz],
        }]
        if self.cfg.color_on_move and self.rng.random() < 0.20:
            out2.append({"type": "SET_COLOR", "id": head_id, "color": self._rand_color()})
        return out2

    def _should_reset(self) -> bool:
        if self._since_reset < self.cfg.reset_cooldown:
            return False

        if len(self._cube_count_hist) < max(20, self.cfg.stuck_window // 3):
            return False

        if self.cfg.no_growth_reset:
            mn = min(self._cube_count_hist)
            mx = max(self._cube_count_hist)
            if mx - mn <= 0:
                return True

        # head 이동이 거의 없고(제자리) 동시에 성장도 없다면 reset
        if len(self._head_pos_hist) >= 20:
            x0, y0, z0 = self._head_pos_hist[0]
            x1, y1, z1 = self._head_pos_hist[-1]
            d = abs(x1 - x0) + abs(y1 - y0) + abs(z1 - z0)
            if d <= self.cfg.head_move_eps:
                mn = min(self._cube_count_hist)
                mx = max(self._cube_count_hist)
                if mx - mn <= 0:
                    return True

        return False

    def _rand_color(self) -> str:
        h = self.rng.random()
        s = 0.60 + 0.35 * self.rng.random()
        v = 0.78 + 0.20 * self.rng.random()
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
