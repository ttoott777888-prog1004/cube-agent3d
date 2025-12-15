from __future__ import annotations

import argparse
import asyncio
import sys

from .runtime.server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="cube-agent3d")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run local server + web UI")
    p_run.add_argument("--host", default="127.0.0.1")
    p_run.add_argument("--port", type=int, default=8000)
    p_run.add_argument("--tick-hz", type=float, default=10.0)
    p_run.add_argument("--max-cubes", type=int, default=256)
    p_run.add_argument("--seed", type=int, default=1234)
    p_run.add_argument("--session-dir", default="sessions")

    args = parser.parse_args()

    if args.cmd == "run":
        try:
            asyncio.run(
                run_server(
                    host=args.host,
                    port=args.port,
                    tick_hz=args.tick_hz,
                    max_cubes=args.max_cubes,
                    seed=args.seed,
                    session_root=args.session_dir,
                )
            )
        except KeyboardInterrupt:
            print("\n종료합니다.")
            return
    else:
        print("알 수 없는 명령입니다.", file=sys.stderr)
        sys.exit(1)
