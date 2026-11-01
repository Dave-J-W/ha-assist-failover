"""Failover logic with no Home Assistant imports, so tests can load this file by path."""

import asyncio
from dataclasses import dataclass, field
import json
import time
from typing import Any

# A Wyoming WAV stream sends its header before any audio, and a header alone proves nothing.
# 44 bytes is a plain WAV header; anything this small is not speech.
HEADER_ONLY_BYTES = 128

# A Wyoming server's info can list thousands of voices; the first line is all we read.
READ_LIMIT = 2**20


async def server_alive(host, port, timeout):
    """Ask a Wyoming server to describe itself. An open port alone doesn't prove the engine is up."""

    async def ask():
        reader, writer = await asyncio.open_connection(host, port, limit=READ_LIMIT)
        try:
            writer.write(b'{"type": "describe"}\n')
            await writer.drain()
            return json.loads(await reader.readline()).get("type") == "info"
        finally:
            writer.close()

    try:
        return await asyncio.wait_for(ask(), timeout)
    except (OSError, ValueError, AttributeError, asyncio.TimeoutError):
        return False


@dataclass
class Attempt:
    """One engine to try. run() returns its answer, or None if the engine failed."""

    name: str
    run: Any
    timeout: float
    reachable: Any = None


@dataclass
class Outcome:
    value: Any
    engine: str | None
    seconds: float
    failures: list = field(default_factory=list)


async def first_answer(attempts):
    """Return the first engine's answer. An empty transcript counts: silence is not a failure."""
    failures = []
    start = time.monotonic()
    for attempt in attempts:
        try:
            if attempt.reachable is not None and not await attempt.reachable():
                failures.append(f"{attempt.name}: unreachable")
                continue
            value = await asyncio.wait_for(attempt.run(), attempt.timeout)
        except asyncio.TimeoutError:
            failures.append(f"{attempt.name}: timed out after {attempt.timeout:g} s")
            continue
        except Exception as err:  # noqa: BLE001 - any engine fault must fall through to the next one
            failures.append(f"{attempt.name}: {type(err).__name__}: {err}")
            continue
        if value is None:
            failures.append(f"{attempt.name}: returned an error")
            continue
        return Outcome(value, attempt.name, time.monotonic() - start, failures)
    return Outcome(None, None, time.monotonic() - start, failures)


class ReplayableText:
    """Lets a second engine read text the first one already consumed, without waiting for all of it.

    A streaming conversation agent keeps its head start on the primary, and the backup still
    gets the whole message.
    """

    def __init__(self, source):
        self._source = aiter(source)
        self._seen = []
        self._done = False
        self._lock = asyncio.Lock()

    async def _pull(self, index):
        async with self._lock:
            while len(self._seen) <= index and not self._done:
                try:
                    self._seen.append(await anext(self._source))
                except StopAsyncIteration:
                    self._done = True

    async def wait_for_text(self):
        """Wait for the first text, so a slow agent isn't counted against the engine's timeout."""
        await self._pull(0)

    async def reader(self):
        index = 0
        while True:
            await self._pull(index)
            if index >= len(self._seen):
                return
            yield self._seen[index]
            index += 1


async def first_audio(data_gen):
    """Read until real audio arrives. Returns (chunks so far, the rest), or None if there is none."""
    stream = aiter(data_gen)
    chunks = []
    size = 0
    while size <= HEADER_ONLY_BYTES:
        try:
            chunk = await anext(stream)
        except StopAsyncIteration:
            return None
        chunks.append(chunk)
        size += len(chunk)
    return chunks, stream


def options_for(options, supported):
    """Keep only what this engine accepts, since two engines rarely take the same options."""
    return {name: value for name, value in options.items() if name in (supported or [])}
