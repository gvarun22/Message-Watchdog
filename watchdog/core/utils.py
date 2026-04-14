"""
Shared async utilities.
"""
from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_sync(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    Run a synchronous (blocking) callable in the default thread executor so
    it doesn't block the asyncio event loop.

    Usage::

        result = await run_sync(requests.get, url, timeout=5)
        await run_sync(smtp.sendmail, from_, to_, msg)
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))
