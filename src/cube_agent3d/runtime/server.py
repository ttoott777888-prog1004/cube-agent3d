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

        # 정책(탐색/확장 + 자동 리셋)
        self.policy = EnginePolicy()
        self.policy.seed(cfg.seed)

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
            "cube_count": len(self.state.cubes),
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
                actions = self.policy.decide(self.state)
                applied, did_reset = self.apply_actions(actions)

                # 점수는 UI 정보용(기존 tower_score를 유지)
                score = 0.0 if did_reset else self.state.score_tower()

                # 로깅
                self.logger.write_actions({
                    "tick": self.state.tick,
                    "actions": applied,
                })
                self.logger.write_summary({
                    "tick": self.state.tick,
                    "cube_count": len(self.state.cubes),
                    "score": score,
                    "note": "engine_policy" if not did_reset else "engine_policy:auto_reset",
                })
                self.logger.write_snapshot(self.state.snapshot())

                # 브로드캐스트
                await self.broadcast("ACTION_BATCH", {"tick": self.state.tick, "actions": applied, "score": score})
                await self.broadcast("STATE_SNAPSHOT", self.state.snapshot())

                # AUTO_RESET이면 tick 증가/age 증가를 하지 않음 (reset 상태 유지)
                if not did_reset:
                    self.state.step_age()
                    self.state.tick += 1
            else:
                await self.broadcast("SERVER_STATUS", self.status_payload())

            t = await ticker.sleep_next(t)

    def apply_actions(self, actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        applied: list[dict[str, Any]] = []
        did_reset = False

        max_actions = 24
        for a in actions[:max_actions]:
            t = a.get("type")
            ok = False

            if t == "AUTO_RESET":
                self.state.reset()
                did_reset = True
                ok = True

            elif t == "HINT_TARGET":
                # UI 전용 힌트(상태에는 적용하지 않음)
                ok = True

            elif t == "DUPLICATE":
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

        return applied, did_reset


async def run_server(host: str, port: int, tick_hz: float, max_cubes: int, seed: int, session_root: str) -> None:
    cfg = EngineConfig(tick_hz=tick_hz, max_cubes=max_cubes, seed=seed, session_root=session_root)
    engine = Engine(cfg)

    app = web.Application()

    async def _serve_pkg_file(pkg_rel: str, content_type: str, *, charset: str | None = None) -> web.Response:
        with resources.files("cube_agent3d.web").joinpath(pkg_rel).open("rb") as f:
            data = f.read()
        # aiohttp: charset는 content_type 문자열에 포함하면 안 됨 (별도 인자)
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
        await ws.send_str(json.dumps({"type": "STATE_SNAPSHOT", "payload": engine.state.snapshot()}, ensure_ascii=False))

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
                    await engine.broadcast("SERVER_STATUS", engine.status_payload())
                    await engine.broadcast("STATE_SNAPSHOT", engine.state.snapshot())

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
