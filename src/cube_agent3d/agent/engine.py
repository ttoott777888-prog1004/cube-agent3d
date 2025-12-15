from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any, Iterable, Tuple

from aiohttp import web, WSMsgType

from ..protocol.types import Bounds
from ..scene.state import SceneState
from ..storage.logger import SessionLogger
from ..runtime.tick import Ticker


@dataclass
class EngineConfig:
    tick_hz: float
    max_cubes: int
    seed: int
    session_root: str


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
    x, y, z = (p[0], p[1], p[2]) if isinstance(p, (list, tuple)) and len(p) >= 3 else (0, 0, 0)
    return int(round(float(x))), int(round(float(y))), int(round(float(z)))


class ExplorerPolicy:
    """
    - 26방향(3x3x3-1) 후보 위치를 만들고
    - y<0(바닥 밑) 금지
    - 충돌(이미 점유) 금지
    - y>0이면 아래 지지(support) 있는 곳을 선호
    - 후보를 probes로 제공(프론트에서 초록 반투명 표시)
    """

    def __init__(self, rng: random.Random, *, probe_n: int = 24) -> None:
        self.rng = rng
        self.probe_n = probe_n
        self._last_probes: list[dict[str, Any]] = []
        self._visited: set[Tuple[int, int, int]] = set()

    def probes(self) -> list[dict[str, Any]]:
        return self._last_probes

    def decide(self, state: SceneState, *, max_cubes: int) -> list[dict[str, Any]]:
        cubes = _iter_cubes(state)
        if not cubes:
            self._last_probes = []
            return []

        occ: set[Tuple[int, int, int]] = set(_cube_pos(c) for c in cubes)

        # frontier(확장 여지 많은 큐브) 선택
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
                        # y>0이면 아래 지지 체크(없어도 후보로는 보여주되 점수 낮춤)
                        score += 1
            return score

        best = max(cubes, key=frontier_score)
        head_id = _cube_id(best)
        hx, hy, hz = _cube_pos(best)

        # Bounds가 있으면 최대한 존중(없으면 넉넉한 기본값)
        b = getattr(state, "bounds", Bounds())
        x_min = int(getattr(b, "x_min", -40))
        x_max = int(getattr(b, "x_max", 40))
        y_min = int(getattr(b, "y_min", 0))
        y_max = int(getattr(b, "y_max", 80))
        z_min = int(getattr(b, "z_min", -40))
        z_max = int(getattr(b, "z_max", 40))

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

                    # 전방위로 퍼지게: 원점으로부터 거리(너무 과하면 bounds에 막힘)
                    spread = (abs(nx) + abs(nz)) * 0.10
                    height = ny * 0.05

                    # 점수(고정된 한 방향으로만 못 가게 랜덤 미세 노이즈 포함)
                    score = (support * 5.0) + (novelty * 2.0) + spread + height + (self.rng.random() * 0.01)

                    # 아래 지지 없는 공중 후보도 “보이긴 하게” 하되 선택은 거의 안 되게
                    if ny > 0 and support == 0:
                        score -= 10.0

                    cand.append((score, (nx, ny, nz), (dx, dy, dz)))

        cand.sort(key=lambda t: t[0], reverse=True)

        # probes(초록 반투명 표시용): 상위 N개
        probes: list[dict[str, Any]] = []
        for i, (_, (px, py, pz), _) in enumerate(cand[: self.probe_n]):
            probes.append({
                "pos": [px, py, pz],
                "scale": [1.02, 1.02, 1.02],
            })
        self._last_probes = probes

        if not cand:
            return []

        # 최종 선택: 상위 몇 개 중 하나를 랜덤(완전 결정적이면 또 한쪽으로 굳음)
        top_k = min(6, len(cand))
        pick = cand[self.rng.randrange(0, top_k)]
        _, (tx, ty, tz), (dx, dy, dz) = pick

        # MOVE vs DUPLICATE: max_cubes 도달 전에는 DUPLICATE 우선
        cube_count = len(getattr(state, "cubes", [])) if not isinstance(getattr(state, "cubes", []), dict) else len(getattr(state, "cubes", {}).keys())
        can_dup = cube_count < max_cubes

        if can_dup and self.rng.random() < 0.75:
            new_id = f"c{getattr(state, 'tick', 0)}_{self.rng.randrange(1_000_000)}"
            self._visited.add((tx, ty, tz))
            return [{
                "type": "DUPLICATE",
                "source_id": head_id,
                "new_id": new_id,
                "offset": [dx, dy, dz],
            }]

        self._visited.add((tx, ty, tz))
        return [{
            "type": "MOVE",
            "id": head_id,
            "pos": [tx, ty, tz],
        }]


class Engine:
    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self.bounds = Bounds()
        self.rng = random.Random(cfg.seed)
        self.state = SceneState(bounds=self.bounds, max_cubes=cfg.max_cubes, rng=self.rng)

        self.policy = ExplorerPolicy(self.rng, probe_n=24)

        self.running = False
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.logger = SessionLogger.create(cfg.session_root)
        self.state.reset()

    async def close(self) -> None:
        self.logger.close()

    def status_payload(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "tick": self.state.tick,
            "cube_count": len(self.state.cubes) if not isinstance(self.state.cubes, dict) else len(self.state.cubes.keys()),
            "session_id": self.logger.session_id,
            "session_dir": str(self.logger.session_dir),
            "tick_hz": self.cfg.tick_hz,
            "max_cubes": self.cfg.max_cubes,
        }

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

    async def engine_loop(self) -> None:
        ticker = Ticker(self.cfg.tick_hz)
        t = time.perf_counter()

        while True:
            if self.running:
                actions = self.policy.decide(self.state, max_cubes=self.cfg.max_cubes)
                applied = self.apply_actions(actions)

                score = self.state.score_tower()

                self.logger.write_actions({"tick": self.state.tick, "actions": applied})
                self.logger.write_summary({
                    "tick": self.state.tick,
                    "cube_count": len(self.state.cubes) if not isinstance(self.state.cubes, dict) else len(self.state.cubes.keys()),
                    "score": score,
                    "note": "explorer_policy",
                })

                snap = self.state.snapshot()
                snap["probes"] = self.policy.probes()  # <-- 초록 반투명 탐색 후보
                self.logger.write_snapshot(snap)

                await self.broadcast("ACTION_BATCH", {"tick": self.state.tick, "actions": applied, "score": score})
                await self.broadcast("STATE_SNAPSHOT", snap)
                await self.broadcast("SERVER_STATUS", self.status_payload())

                self.state.step_age()
                self.state.tick += 1
            else:
                await self.broadcast("SERVER_STATUS", self.status_payload())

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
                ok = self.state.move_cube_abs(
                    cid=str(a["id"]),
                    pos=list(a["pos"]),
                )
            elif t == "ROTATE_YAW":
                ok = self.state.rotate_cube_yaw(
                    cid=str(a["id"]),
                    yaw_rad=float(a["yaw"]),
                )
            elif t == "SCALE":
                ok = self.state.scale_cube_abs(
                    cid=str(a["id"]),
                    scale=list(a["scale"]),
                )
            elif t == "SET_COLOR":
                ok = self.state.set_color(
                    cid=str(a["id"]),
                    color=str(a["color"]),
                )

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

        snap = engine.state.snapshot()
        snap["probes"] = engine.policy.probes()
        await ws.send_str(json.dumps({"type": "SERVER_STATUS", "payload": engine.status_payload()}, ensure_ascii=False))
        await ws.send_str(json.dumps({"type": "STATE_SNAPSHOT", "payload": snap}, ensure_ascii=False))

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                mtype = data.get("type")
                if mtype == "HELLO":
                    await ws.send_str(json.dumps({"type": "SERVER_STATUS", "payload": engine.status_payload()}, ensure_ascii=False))
                elif mtype == "UI_START":
                    engine.running = True
                    await engine.broadcast("SERVER_STATUS", engine.status_payload())
                elif mtype == "UI_STOP":
                    engine.running = False
                    await engine.broadcast("SERVER_STATUS", engine.status_payload())
                elif mtype == "RESET":
                    engine.running = False
                    engine.state.reset()
                    snap2 = engine.state.snapshot()
                    snap2["probes"] = engine.policy.probes()
                    await engine.broadcast("SERVER_STATUS", engine.status_payload())
                    await engine.broadcast("STATE_SNAPSHOT", snap2)
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
