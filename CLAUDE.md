# Message Watchdog — Project Instructions

## Code style
- Python 3.11+. Use `asyncio.get_running_loop()`, not `get_event_loop()`.
- Use `from __future__ import annotations` in all Python files.
- Use `run_sync()` from `watchdog.core.utils` instead of raw `run_in_executor` + `partial`.
- One `LLMProvider` per provider type — no `hasattr` duck-typing across provider instances.
