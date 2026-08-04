from __future__ import annotations

import asyncio
import signal

from app.bootstrap import build_components


async def run_worker() -> None:
    components = build_components()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stopped.set)
    await components.start()
    try:
        await stopped.wait()
    finally:
        await components.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
