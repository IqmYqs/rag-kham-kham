"""asyncio helpers.

psycopg's async mode cannot run on Windows' default ProactorEventLoop, so scripts and tests
must use a SelectorEventLoop there. Linux (Docker/Vercel) is unaffected.
"""

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    return asyncio.SelectorEventLoop if sys.platform == "win32" else None


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run` that works with psycopg on every OS."""
    return asyncio.run(coro, loop_factory=loop_factory())


def uvicorn_loop_factory(use_subprocess: bool = False) -> Callable[[], asyncio.AbstractEventLoop]:
    """For `uvicorn --loop app.core.aio:uvicorn_loop_factory` on Windows without --reload."""
    return asyncio.SelectorEventLoop
