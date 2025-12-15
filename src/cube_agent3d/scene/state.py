from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from ..protocol.types import Bounds


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _norm_quat(q: list[float]) -> list[float]:
    x, y, z, w = q
    n = math.sqrt(x*x + y*y + z*z + w*w)
    if n <= 1e-9:
        return [0.0, 0.0, 0.0, 1.0]
    return [x/n, y/n, z/n, w/n]


def _quat_from_yaw(yaw_rad: float) -> list[float]:
    half = yaw_rad * 0.5
    return [0.0, math.sin(half), 0.0, math.cos(half)]


@dataclass
class Cube:
    id: str
    pos: list[float]
    rot: list[float]
    scale: list[float]
    color: str
    age: int = 0


@dataclass
class SceneState:
    bounds: Bounds
    max_cubes: int
    rng: random.Random
    tick: int = 0
    cubes: dict[str, Cube] = field(default_factory=dict)

    def reset(self) -> None:
        self.tick = 0
        self.cubes.clear()
        self.spawn_cube(
            new_id="1",
            pos=[0.0, 0.5, 0.0],
            rot=[0.0, 0.0, 0.0, 1.0],
            scale=[1.0, 1.0, 1.0],
            color="#7dd3fc",
        )

    def spawn_cube(self, new_id: str, pos: list[float], rot: list[float], scale: list[float], color: str) -> bool:
        if len(self.cubes) >= self.max_cubes:
            return False
        if new_id in self.cubes:
            return False

        p = [
            _clamp(pos[0], self.bounds.x_min, self.bounds.x_max),
            _clamp(pos[1], self.bounds.y_min, self.bounds.y_max),
            _clamp(pos[2], self.bounds.z_min, self.bounds.z_max),
        ]
        r = _norm_quat(rot)
        s = [
            _clamp(scale[0], 0.25, 4.0),
            _clamp(scale[1], 0.25, 4.0),
            _clamp(scale[2], 0.25, 4.0),
        ]

        self.cubes[new_id] = Cube(id=new_id, pos=p, rot=r, scale=s, color=color, age=0)
        return True

    def duplicate_cube(self, source_id: str, new_id: str, offset: list[float]) -> bool:
        src = self.cubes.get(source_id)
        if not src:
            return False
        pos = [src.pos[0] + offset[0], src.pos[1] + offset[1], src.pos[2] + offset[2]]
        return self.spawn_cube(new_id=new_id, pos=pos, rot=src.rot[:], scale=src.scale[:], color=src.color)

    def move_cube_abs(self, cid: str, pos: list[float]) -> bool:
        c = self.cubes.get(cid)
        if not c:
            return False
        c.pos[0] = _clamp(pos[0], self.bounds.x_min, self.bounds.x_max)
        c.pos[1] = _clamp(pos[1], self.bounds.y_min, self.bounds.y_max)
        c.pos[2] = _clamp(pos[2], self.bounds.z_min, self.bounds.z_max)
        return True

    def rotate_cube_yaw(self, cid: str, yaw_rad: float) -> bool:
        c = self.cubes.get(cid)
        if not c:
            return False
        c.rot = _quat_from_yaw(yaw_rad)
        return True

    def scale_cube_abs(self, cid: str, scale: list[float]) -> bool:
        c = self.cubes.get(cid)
        if not c:
            return False
        c.scale[0] = _clamp(scale[0], 0.25, 4.0)
        c.scale[1] = _clamp(scale[1], 0.25, 4.0)
        c.scale[2] = _clamp(scale[2], 0.25, 4.0)
        return True

    def set_color(self, cid: str, color: str) -> bool:
        c = self.cubes.get(cid)
        if not c:
            return False
        c.color = color
        return True

    def step_age(self) -> None:
        for c in self.cubes.values():
            c.age += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "cubes": [
                {
                    "id": c.id,
                    "pos": c.pos,
                    "rot": c.rot,
                    "scale": c.scale,
                    "color": c.color,
                    "age": c.age,
                }
                for c in self.cubes.values()
            ],
        }

    def score_tower(self) -> float:
        if not self.cubes:
            return 0.0
        max_y = max(c.pos[1] for c in self.cubes.values())
        overlap_pen = 0.0
        ids = list(self.cubes.keys())
        for i in range(len(ids)):
            a = self.cubes[ids[i]]
            for j in range(i + 1, len(ids)):
                b = self.cubes[ids[j]]
                dx = a.pos[0] - b.pos[0]
                dz = a.pos[2] - b.pos[2]
                dy = a.pos[1] - b.pos[1]
                d2 = dx*dx + dz*dz
                if d2 < 0.55*0.55 and abs(dy) < 0.55:
                    overlap_pen += 1.0
        count_pen = max(0, len(self.cubes) - 32) * 0.02
        return (max_y * 2.0) - overlap_pen - count_pen
