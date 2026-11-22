import asyncio
from collections.abc import Awaitable, Callable

ConnectionProbe = Callable[[], Awaitable[bool]]


class SendConnection:
    """Gate new sends until a probe succeeds. Failed probes retry on the next one-second tick."""

    def __init__(self, probe: ConnectionProbe | None = None):
        self.probe = probe
        self.ready = asyncio.Event()
        self.connected: bool | None = True if probe is None else None
        if self.connected:
            self.ready.set()

    def transport_failed(self) -> None:
        if self.probe is not None:
            self.connected = False
            self.ready.clear()

    async def monitor(self) -> None:
        if self.probe is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            self.connected = await self.probe()
            if self.connected:
                self.ready.set()
            else:
                self.ready.clear()
            await asyncio.sleep(max(0, started + 1 - loop.time()))
