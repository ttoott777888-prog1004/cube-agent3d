from __future__ import annotations

import asyncio
import json
import math
import random
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any, Iterable, Optional

from aiohttp import WSMsgType, web

from ..protocol.types import Bounds
from ..scene.state import SceneState
from ..storage.logger import SessionLogger
from .tick import Ticker


@dataclass
class EngineConfig:
    tick_hz: float
    max_cubes: int
    seed: int
    session_root: str


class ExplorerPolicy:
    """
    - 3D 전 방향 탐색(바닥 아래 금지)
    - "초록색 투명 상자" = 후보 위치(probes)로 UI에 전달
    - 정체/이상 감지 시 자동 리셋
    """

    def __init__(
        self,
        rng: random.Random,
        *,
        max_cubes: int,
        step: float = 1.0,
        min_y: float = 0.5,
        probe_count: int = 18,
        duplicate_prob: float = 0.55,
        reset_patience: int = 650,
        stuck_patience: int = 120,
        y_soft_cap: float = 18.0,
    ) -> None:
        self.rng = rng
        self.max_cubes = int(max_cubes)
        self.step = float(step)
        self.min_y = float(min_y)
        self.probe_count = int(probe_count)
        self.duplicate_prob = float(duplicate_prob)

        self.reset_patience = int(reset_patience)
        self.stuck_patience = int(stuck_patience)
        self.y_soft_cap = float(y_soft_cap)

        self.head_id: Optional[str] = None
        self._next_id = 1

        self.visited: dict[tuple[int, int, int], int] = {}
        self.no_improve_ticks = 0
        self.stuck_ticks = 0

        self.latest_probes: list[dict[str, Any]] = []
        self.latest_target: Optional[list[float]] = None
        self._last_head_id: Optional[str] = None

    def reset(self, state: SceneState) -> None:
        snap = state.snapshot()
        cubes = snap.get("cubes", []) or []
        self.head_id = str(cubes[0].get("id")) if cubes else None

        ids = {str(c.get("id")) for c in cubes if "id" in c}
        self._next_id = 1
        while str(self._next_id) in ids:
            self._next_id += 1

        self.visited.clear()
        self.no_improve_ticks = 0
        self.stuck_ticks = 0

        self.latest_probes = []
        self.latest_target = None
        self._last_head_id = None

        self.preview(state)

    def _fresh_id(self, existing: set[str]) -> str:
        while True:
            cid = str(self._next_id)
            self._next_id += 1
            if cid not in existing:
                return cid

    @staticmethod
    def _voxel_key(pos: Iterable[float]) -> tuple[int, int, int]:
        x, y, z = pos
        return (int(round(x)), int(round(y)), int(round(z)))

    @staticmethod
    def _dist2(a: Iterable[float], b: Iterable[float]) -> float:
        ax, ay, az = a
        bx, by, bz = b
        dx = ax - bx
        dy = ay - by
        dz = az - bz
        return dx * dx + dy * dy + dz * dz

    def _collect(self, state: SceneState) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        snap = state.snapshot()
        cubes = snap.get("cubes", []) or []
        by_id: dict[str, dict[str, Any]] = {}
        for c in cubes:
            cid = str(c.get("id"))
            by_id[cid] = c
        return cubes, by_id

    def preview(self, state: SceneState) -> None:
        cubes, by_id = self._collect(state)
        if not cubes:
            self.latest_probes = []
            self.latest_target = None
            return

        if self.head_id is None or self.head_id not in by_id:
            self.head_id = str(cubes[0].get("id"))

        head = by_id[self.head_id]
        head_pos = head.get("pos", [0.0, self.min_y, 0.0])

        probes, target = self._plan(list(head_pos), cubes)
        self.latest_probes = probes
        self.latest_target = target

    def _plan(self, head_pos: list[float], cubes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Optional[list[float]]]:
        positions = [c.get("pos", [0.0, 0.0, 0.0]) for c in cubes]

        dirs: list[tuple[int, int, int]] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if dx == 0 and dy == 0 and dz == 0:
                        continue
                    # y 이동 남발 방지
                    if dy != 0 and self.rng.random() < 0.35:
                        continue
                    dirs.append((dx, dy, dz))

        cand: list[tuple[float, list[float]]] = []
        hx, hy, hz = head_pos

        for dx, dy, dz in dirs:
            px = hx + dx * self.step
            py = hy + dy * self.step
            pz = hz + dz * self.step

            if py < self.min_y:
                continue

            p = [float(px), float(py), float(pz)]

            mind2 = 1e18
            for q in positions:
                d2 = self._dist2(p, q)
                if d2 < mind2:
                    mind2 = d2
            novelty = math.sqrt(max(mind2, 0.0))

            y_pen = max(0.0, p[1] - self.y_soft_cap) * 0.35
            radial = math.sqrt(p[0] * p[0] + p[2] * p[2]) * 0.10

            score = novelty + radial - y_pen + self.rng.random() * 0.03
            cand.append((score, p))

        cand.sort(key=lambda x: x[0], reverse=True)

        probes: list[dict[str, Any]] = []
        for i, (_, p) in enumerate(cand[: self.probe_count]):
            probes.append({
                "pos": [round(p[0], 4), round(p[1], 4), round(p[2], 4)],
                "alpha": 0.22 if i > 0 else 0.38,
            })

        target = cand[0][1] if cand else None
        return probes, target

    def compute_score(self, state: SceneState) -> float:
        snap = state.snapshot()
        cubes = snap.get("cubes", []) or []
        if not cubes:
            return 0.0

        xs = [c["pos"][0] for c in cubes if "pos" in c]
        ys = [c["pos"][1] for c in cubes if "pos" in c]
        zs = [c["pos"][2] for c in cubes if "pos" in c]

        xspan = (max(xs) - min(xs)) if xs else 0.0
        zspan = (max(zs) - min(zs)) if zs else 0.0
        ymax = max(ys) if ys else 0.0

        visited_bonus = float(len(self.visited)) * 1.0
        spread_bonus = (xspan + zspan) * 2.0
        y_pen = max(0.0, ymax - self.y_soft_cap) * 1.5

        return visited_bonus + spread_bonus - y_pen

    def should_auto_reset(self) -> Optional[str]:
        if self.no_improve_ticks >= self.reset_patience:
            return f"no_improve>{self.reset_patience}"
        if self.stuck_ticks >= self.stuck_patience:
            return f"stuck>{self.stuck_patience}"
        return None

    def decide(self, state: SceneState) -> list[dict[str, Any]]:
        cubes, by_id = self._collect(state)
        if not cubes:
            self.latest_probes = []
            self.latest_target = None
            return []

        if self.head_id is None or self.head_id not in by_id:
            self.head_id = str(cubes[0].get("id"))

        head = by_id[self.head_id]
        head_pos = list(head.get("pos", [0.0, self.min_y, 0.0]))

        probes, target = self._plan(head_pos, cubes)
        self.latest_probes = probes
        self.latest_target = target

        self.visited[self._voxel_key(head_pos)] = self.visited.get(self._voxel_key(head_pos), 0) + 1

        existing_ids = {str(c.get("id")) for c in cubes if "id" in c}

        actions: list[dict[str, Any]] = []

        # 헤드 표시(초록), 이전 헤드는 어둡게
        if self._last_head_id and self._last_head_id != self.head_id:
            actions.append({"type": "SET_COLOR", "id": self._last_head_id, "color": "#0f172a"})
        actions.append({"type": "SET_COLOR", "id": self.head_id, "color": "#22c55e"})

        if target is None:
            self.stuck_ticks += 1
            return actions

        can_dup = len(cubes) + 1 <= self.max_cubes
        if can_dup and (self.rng.random() < self.duplicate_prob):
            new_id = self._fresh_id(existing_ids)
            offset = [target[0] - head_pos[0], target[1] - head_pos[1], target[2] - head_pos[2]]
            actions.append({
                "type": "DUPLICATE",
                "source_id": self.head_id,
                "new_id": new_id,
                "offset": [round(offset[0], 4), round(offset[1], 4), round(offset[2], 4)],
            })
            actions.append({"type": "SET_COLOR", "id": new_id, "color": "#22c55e"})
            self._last_head_id = self.head_id
            self.head_id = new_id
        else:
            actions.append({
                "type": "MOVE",
                "id": self.head_id,
                "pos": [round(target[0], 4), round(target[1], 4), round(target[2], 4)],
            })

        return actions


class Engine:
    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self.bounds = Bounds()
        self.rng = random.Random(cfg.seed)
        self.state = SceneState(bounds=self.bounds, max_cubes=cfg.max_cubes, rng=self.rng)

        self.policy = ExplorerPolicy(self.rng, max_cubes=cfg.max_cubes)

        self.running = False
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.logger = SessionLogger.create(cfg.session_root)

        self._best_score = -1.0

        self.state.reset()
        self.policy.reset(self.state)

    async def close(self) -> None:
        self.logger.close()

    def status_payload(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "tick": self.state.tick,
            "cube_count": len(self.state.cubes),
            "session_id": self.logger.session_id,
            "session_dir": str(self.logger.session_dir),
            "tick_hz": self.cfg.tick_hz,
            "max_cubes": self.cfg.max_cubes,
            "policy": "explorer_v1",
        }

    def decorated_snapshot(self) -> dict[str, Any]:
        snap = self.state.snapshot()
        snap["probes"] = self.policy.latest_probes
        if self.policy.latest_target is not None:
            snap["target"] = self.policy.latest_target
        if self.policy.head_id is not None:
            snap["head_id"] = self.policy.head_id
        return snap

    async def broadcast(self, msg_type: str, payload: dict[str, Any]) -> None:
        dead: list[web.WebSocketResponse] = []
        data = json.dumps({"type": msg_type, "payload": payload}, ensure_ascii=False)
        for ws in list(self.ws_clients):
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    async def _engine_event(self, level: str, message: str, **extra: Any) -> None:
        await self.broadcast("ENGINE_EVENT", {"level": level, "message": message, **extra})

    async def _do_reset(self, reason: str) -> None:
        self.state.reset()
        self.policy.reset(self.state)
        self._best_score = -1.0

        await self._engine_event("warn", "AUTO_RESET", reason=reason)
        await self.broadcast("SERVER_STATUS", self.status_payload())
        await self.broadcast("STATE_SNAPSHOT", self.decorated_snapshot())

    async def engine_loop(self) -> None:
        ticker = Ticker(self.cfg.tick_hz)
        t = time.perf_counter()
        while True:
            if self.running:
                actions = self.policy.decide(self.state)
                applied = self.apply_actions(actions)

                explore_score = self.policy.compute_score(self.state)

                self.logger.write_actions({"tick": self.state.tick, "actions": applied})
                self.logger.write_summary({
                    "tick": self.state.tick,
                    "cube_count": len(self.state.cubes),
                    "score": explore_score,
                    "note": "explorer_policy",
                })
                self.logger.write_snapshot(self.state.snapshot())

                await self.broadcast("ACTION_BATCH", {"tick": self.state.tick, "actions": applied, "score": explore_score})
                await self.broadcast("STATE_SNAPSHOT", self.decorated_snapshot())

                # stuck / no-improve 감지
                if len(applied) <= 0:
                    self.policy.stuck_ticks += 1
                else:
                    self.policy.stuck_ticks = 0

                if explore_score > self._best_score + 1e-6:
                    self._best_score = explore_score
                    self.policy.no_improve_ticks = 0
                else:
                    self.policy.no_improve_ticks += 1

                reason = self.policy.should_auto_reset()
                if reason is not None:
                    await self._do_reset(reason)
                else:
                    self.state.step_age()
                    self.state.tick += 1

                if self.state.tick % 10 == 0:
                    await self.broadcast("SERVER_STATUS", self.status_payload())
            else:
                self.policy.preview(self.state)
                await self.broadcast("SERVER_STATUS", self.status_payload())
                await self.broadcast("STATE_SNAPSHOT", self.decorated_snapshot())

            t = await ticker.sleep_next(t)

    def apply_actions(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []

        max_actions = 24
        for a in actions[:max_actions]:
            t = a.get("type")
            ok = False

            if t == "DUPLICATE":
                ok = self.state.duplicate_cube(
                    source_id=str(a["source_id"]),
                    new_id=str(a["new_id"]),
                    offset=list(a["offset"]),
                )
            elif t == "MOVE":
                ok = self.state.move_cube_abs(cid=str(a["id"]), pos=list(a["pos"]))
            elif t == "ROTATE_YAW":
                ok = self.state.rotate_cube_yaw(cid=str(a["id"]), yaw_rad=float(a["yaw"]))
            elif t == "SCALE":
                ok = self.state.scale_cube_abs(cid=str(a["id"]), scale=list(a["scale"]))
            elif t == "SET_COLOR":
                ok = self.state.set_color(cid=str(a["id"]), color=str(a["color"]))

            if ok:
                applied.append(a)

        return applied


async def run_server(host: str, port: int, tick_hz: float, max_cubes: int, seed: int, session_root: str) -> None:
    cfg = EngineConfig(tick_hz=tick_hz, max_cubes=max_cubes, seed=seed, session_root=session_root)
    engine = Engine(cfg)

    app = web.Application()

    async def _serve_pkg_file(pkg_rel: str, content_type: str, *, charset: str | None = None) -> web.Response:
        with resources.files("cube_agent3d.web").joinpath(pkg_rel).open("rb") as f:
            data = f.read()
        if charset is not None:
            return web.Response(body=data, content_type=content_type, charset=charset)
        return web.Response(body=data, content_type=content_type)

    async def index(request: web.Request) -> web.Response:
        return await _serve_pkg_file("index.html", "text/html", charset="utf-8")

    async def app_js(request: web.Request) -> web.Response:
        return await _serve_pkg_file("app.js", "application/javascript", charset="utf-8")

    async def style_css(request: web.Request) -> web.Response:
        return await _serve_pkg_file("style.css", "text/css", charset="utf-8")

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        engine.ws_clients.add(ws)

        await ws.send_str(json.dumps({"type": "SERVER_STATUS", "payload": engine.status_payload()}, ensure_ascii=False))
        await ws.send_str(json.dumps({"type": "STATE_SNAPSHOT", "payload": engine.decorated_snapshot()}, ensure_ascii=False))

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                mtype = data.get("type")
                if mtype == "HELLO":
                    await ws.send_str(json.dumps({"type": "SERVER_STATUS", "payload": engine.status_payload()}, ensure_ascii=False))
                    await ws.send_str(json.dumps({"type": "STATE_SNAPSHOT", "payload": engine.decorated_snapshot()}, ensure_ascii=False))
                elif mtype == "UI_START":
                    engine.running = True
                    await engine._engine_event("info", "RUN")
                    await engine.broadcast("SERVER_STATUS", engine.status_payload())
                elif mtype == "UI_STOP":
                    engine.running = False
                    await engine._engine_event("info", "STOP")
                    await engine.broadcast("SERVER_STATUS", engine.status_payload())
                elif mtype == "RESET":
                    engine.running = False
                    await engine._do_reset("manual_reset")
            elif msg.type == WSMsgType.ERROR:
                break

        engine.ws_clients.discard(ws)
        return ws

    app.add_routes([
        web.get("/", index),
        web.get("/app.js", app_js),
        web.get("/style.css", style_css),
        web.get("/ws", ws_handler),
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)

    loop_task = asyncio.create_task(engine.engine_loop())

    print(f"서버 시작: http://{host}:{port}")
    print(f"세션: {engine.logger.session_id}  (저장 위치: {engine.logger.session_dir})")

    try:
        await site.start()
        while True:
            await asyncio.sleep(3600)
    finally:
        loop_task.cancel()
        try:
            await engine.close()
        except Exception:
            pass
        await runner.cleanup()
