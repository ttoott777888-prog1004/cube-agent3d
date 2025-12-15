from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _now_session_id() -> str:
    t = time.time()
    lt = time.localtime(t)
    ms = int((t - int(t)) * 1000)
    return f"{lt.tm_year:04d}{lt.tm_mon:02d}{lt.tm_mday:02d}-{lt.tm_hour:02d}{lt.tm_min:02d}{lt.tm_sec:02d}-{ms:03d}"


@dataclass
class SessionLogger:
    root_dir: Path
    session_id: str
    session_dir: Path
    actions_path: Path
    summary_path: Path
    snapshots_path: Path

    _fa: Any = None
    _fs: Any = None
    _fp: Any = None

    @classmethod
    def create(cls, root_dir: str) -> "SessionLogger":
        root = Path(root_dir)
        root.mkdir(parents=True, exist_ok=True)

        sid = _now_session_id()
        sdir = root / sid
        sdir.mkdir(parents=True, exist_ok=True)

        actions = sdir / "actions.jsonl"
        summary = sdir / "summary.jsonl"
        snapshots = sdir / "snapshots.jsonl"

        inst = cls(
            root_dir=root,
            session_id=sid,
            session_dir=sdir,
            actions_path=actions,
            summary_path=summary,
            snapshots_path=snapshots,
        )
        inst._fa = open(actions, "a", encoding="utf-8")
        inst._fs = open(summary, "a", encoding="utf-8")
        inst._fp = open(snapshots, "a", encoding="utf-8")
        return inst

    def close(self) -> None:
        for f in (self._fa, self._fs, self._fp):
            try:
                if f:
                    f.flush()
                    f.close()
            except Exception:
                pass

    def write_actions(self, row: dict[str, Any]) -> None:
        self._fa.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fa.flush()

    def write_summary(self, row: dict[str, Any]) -> None:
        self._fs.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fs.flush()

    def write_snapshot(self, row: dict[str, Any]) -> None:
        self._fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fp.flush()
