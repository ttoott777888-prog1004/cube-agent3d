from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class Ticker:
    hz: float

    async def sleep_next(self, t0: float) -> float:
        dt = 1.0 / max(1e-6, self.hz)
        t1 = t0 + dt
        now = time.perf_counter()
        if t1 > now:
            await asyncio.sleep(t1 - now)
            return t1
        return now
