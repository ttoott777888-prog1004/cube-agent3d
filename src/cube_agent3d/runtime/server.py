from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any

from aiohttp import web, WSMsgType

from ..agent.engine import EnginePolicy
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


class Engine:
    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self.bounds = Bounds()
        self.rng = random.Random(cfg.seed)
        self.state = SceneState(bounds=self.bounds, max_cubes=cfg.max_cubes, rng=self.rng)

        self.policy = EnginePolicy(self.rng)

        self.running = False
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.logger = SessionLogger.create(cfg.session_root)

        # 에피소드: 256에서 시작 → 리셋마다 +1 → 최대 max_cubes(권장: 2000)
        self.episode_index = 0
        self.episode_cap = min(256, cfg.max_cubes)
        self.episode_cap_max = cfg.max_cubes

        self.state.reset()

    async def close(self) -> None:
        self.logger.close()

    def cube_count(self) -> int:
        cubes = getattr(self.state, "cubes", [])
        if isinstance(cubes, dict):
            return len(cubes)
        try:
            return len(cubes)
        except Exception:
            return 0

    def status_payload(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "tick": getattr(self.state, "tick", 0),
            "cube_count": self.cube_count(),
            "session_id": self.logger.session_id,
            "session_dir": str(self.logger.session_dir),
            "tick_hz": self.cfg.tick_hz,
            "max_cubes": self.cfg.max_cubes,
            "episode": self.episode_index,
            "episode_cap": self.episode_cap,
            "episode_cap_max": self.episode_cap_max,
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

    def _make_snapshot(self) -> dict[str, Any]:
        snap = self.state.snapshot()
        snap["probes"] = self.policy.probes()
        snap["episode"] = self.episode_index
        snap["episode_cap"] = self.episode_cap
        snap["episode_cap_max"] = self.episode_cap_max
        return snap

    async def _do_episode_reset(self, reason: str, *, keep_running: bool) -> None:
        # 리셋 이벤트도 “저장”
        score = 0.0
        try:
            score = float(self.state.score_tower())
        except Exception:
            score = 0.0

        self.logger.write_summary({
            "tick": getattr(self.state, "tick", 0),
            "cube_count": self.cube_count(),
            "score": score,
            "note": "episode_reset",
            "reason": reason,
            "episode": self.episode_index,
            "episode_cap": self.episode_cap,
        })

        # episode_cap 증가(최대 max_cubes)
        if self.episode_cap < self.episode_cap_max:
            self.episode_cap = min(self.episode_cap + 1, self.episode_cap_max)

        self.episode_index += 1

        # 상태 리셋
        self.state.reset()
        self.policy.on_episode_reset()

        if not keep_running:
            self.running = False

        snap = self._make_snapshot()
        await self.broadcast("SERVER_STATUS", self.status_payload())
        await self.broadcast("STATE_SNAPSHOT", snap)

    async def engine_loop(self) -> None:
        ticker = Ticker(self.cfg.tick_hz)
        t = time.perf_counter()

        while True:
            if self.running:
                # cap 도달 시: 자동 리셋(중요: “생성”의 다음 단계로 넘어가기)
                if self.cube_count() >= self.episode_cap:
                    await self._do_episode_reset("cap_reached", keep_running=True)
                    t = await ticker.sleep_next(t)
                    continue

                actions = self.policy.decide(self.state, episode_cap=self.episode_cap, max_cubes=self.cfg.max_cubes)

                # 정책이 “이상” 판단 시 자동 리셋
                if actions and actions[0].get("type") == "EPISODE_RESET":
                    await self._do_episode_reset(str(actions[0].get("reason", "policy_reset")), keep_running=True)
                    t = await ticker.sleep_next(t)
                    continue

                applied = self.apply_actions(actions)

                # 간단 학습 업데이트
                try:
                    self.policy.observe(applied)
                except Exception:
                    pass

                score = 0.0
                try:
                    score = float(self.state.score_tower())
                except Exception:
                    score = 0.0

                self.logger.write_actions({"tick": getattr(self.state, "tick", 0), "actions": applied})
                self.logger.write_summary({
                    "tick": getattr(self.state, "tick", 0),
                    "cube_count": self.cube_count(),
                    "score": score,
                    "note": "engine_policy",
                    "episode": self.episode_index,
                    "episode_cap": self.episode_cap,
                })

                snap = self._make_snapshot()
                self.logger.write_snapshot(snap)

                await self.broadcast("ACTION_BATCH", {"tick": getattr(self.state, "tick", 0), "actions": applied, "score": score})
                await self.broadcast("STATE_SNAPSHOT", snap)
                await self.broadcast("SERVER_STATUS", self.status_payload())

                # tick 진행
                try:
                    self.state.step_age()
                except Exception:
                    pass
                self.state.tick = getattr(self.state, "tick", 0) + 1

            else:
                await self.broadcast("SERVER_STATUS", self.status_payload())

            t = await ticker.sleep_next(t)

    def apply_actions(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        max_actions = 32

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

        await ws.send_str(json.dumps({"type": "SERVER_STATUS", "payload": engine.status_payload()}, ensure_ascii=False))
        await ws.send_str(json.dumps({"type": "STATE_SNAPSHOT", "payload": engine._make_snapshot()}, ensure_ascii=False))

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
                    # 수동 RESET: “학습/에피소드 진행”은 유지하고 상태만 리셋(원하시면 keep_running=True로 바꾸시면 됩니다)
                    await engine._do_episode_reset("manual_reset", keep_running=False)

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
