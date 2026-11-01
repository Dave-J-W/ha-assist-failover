"""A small Wyoming client with no Home Assistant imports.

Why not a Wyoming config entry for the primary: core's entry setup connects with no timeout, so
if the primary's machine drops packets while Home Assistant starts, startup stalls for 5 minutes
and the entry ends in setup_error. Talking to the primary directly keeps it out of startup.
"""

import asyncio
import io
import json
import wave

# A server's info can list thousands of voices.
READ_LIMIT = 2**20

STT_RATE = 16000
STT_WIDTH = 2
STT_CHANNELS = 1


class WyomingError(Exception):
    """The server reported an error, or hung up early."""


async def _send(writer, event_type, data=None, payload=b""):
    body = json.dumps(data or {}).encode()
    header = {"type": event_type, "data_length": len(body)}
    if payload:
        header["payload_length"] = len(payload)
    writer.write(json.dumps(header).encode() + b"\n" + body + payload)
    await writer.drain()


async def _read(reader):
    line = await reader.readline()
    if not line:
        raise WyomingError("connection closed")
    header = json.loads(line)
    data = dict(header.get("data") or {})
    if header.get("data_length"):
        data.update(json.loads(await reader.readexactly(header["data_length"])))
    payload = b""
    if header.get("payload_length"):
        payload = await reader.readexactly(header["payload_length"])
    if header["type"] == "error":
        raise WyomingError(data.get("text") or "server error")
    return header["type"], data, payload


async def describe(host, port, timeout):
    """Return the server's info, or None. An open port alone doesn't prove the engine is up."""

    async def ask():
        reader, writer = await asyncio.open_connection(host, port, limit=READ_LIMIT)
        try:
            await _send(writer, "describe")
            event_type, data, _ = await _read(reader)
            return data if event_type == "info" else None
        finally:
            writer.close()

    try:
        return await asyncio.wait_for(ask(), timeout)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError, WyomingError, asyncio.TimeoutError):
        return None


async def transcribe(host, port, language, chunks):
    """Send a whole clip and return its transcript."""
    reader, writer = await asyncio.open_connection(host, port, limit=READ_LIMIT)
    audio = {"rate": STT_RATE, "width": STT_WIDTH, "channels": STT_CHANNELS}
    try:
        await _send(writer, "transcribe", {"language": language})
        await _send(writer, "audio-start", audio)
        for chunk in chunks:
            await _send(writer, "audio-chunk", audio, chunk)
        await _send(writer, "audio-stop")
        while True:
            event_type, data, _ = await _read(reader)
            if event_type == "transcript":
                return data.get("text") or ""
    finally:
        writer.close()


def _wav_header(rate, width, channels):
    """A WAV header with no frame count, for audio of unknown length."""
    with io.BytesIO() as output:
        with wave.open(output, "wb") as wav:
            wav.setframerate(rate)
            wav.setsampwidth(width)
            wav.setnchannels(channels)
        return output.getvalue()


async def _write_text(writer, text, voice, streaming):
    if streaming:
        await _send(writer, "synthesize-start", {"voice": voice} if voice else None)
    message = ""
    async for piece in text:
        message += piece
        if streaming:
            await _send(writer, "synthesize-chunk", {"text": piece})
    # Servers that do stream ignore this one; the others need it.
    await _send(writer, "synthesize", {"text": message, **({"voice": voice} if voice else {})})
    if streaming:
        await _send(writer, "synthesize-stop")


async def synthesize(host, port, text, voice=None, streaming=False):
    """Yield WAV audio for text, an async iterator of str. voice is {"name": ..., "speaker": ...}."""
    reader, writer = await asyncio.open_connection(host, port, limit=READ_LIMIT)
    write = asyncio.ensure_future(_write_text(writer, text, voice, streaming))
    header_sent = False
    try:
        while True:
            event_type, data, payload = await _read(reader)
            if event_type == "audio-start" and not header_sent:
                header_sent = True
                yield _wav_header(data["rate"], data["width"], data["channels"])
            elif event_type == "audio-chunk" and header_sent:
                yield payload
            elif event_type == "synthesize-stopped" or (event_type == "audio-stop" and not streaming):
                return
    finally:
        write.cancel()
        writer.close()


def languages(info, kind):
    """The languages a server's info offers. kind is "asr" or "tts"."""
    found = set()
    for program in (info or {}).get(kind) or []:
        for item in program.get("models" if kind == "asr" else "voices") or []:
            if item.get("installed", True):
                found.update(item.get("languages") or [])
    return found


def voices(info, language):
    """(name, description) of a server's installed voices for a language."""
    return [
        (voice["name"], voice.get("description") or voice["name"])
        for program in (info or {}).get("tts") or []
        for voice in program.get("voices") or []
        if voice.get("installed", True) and language in (voice.get("languages") or [])
    ]


def supports_streaming(info):
    return any(program.get("supports_synthesize_streaming") for program in (info or {}).get("tts") or [])


def parse_address(value):
    """Return (host, port) from "host:port", or None."""
    host, _, port = (value or "").strip().rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return host, int(port)
