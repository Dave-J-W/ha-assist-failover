"""wyoming_direct.py against a fake server speaking the real framing."""

import asyncio
import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("af_wyoming", ROOT / "custom_components/assist_failover/wyoming_direct.py")
W = importlib.util.module_from_spec(_spec)
sys.modules["af_wyoming"] = W
_spec.loader.exec_module(W)


def event(event_type, data=None, payload=b""):
    body = json.dumps(data or {}).encode()
    header = {"type": event_type, "data_length": len(body)}
    if payload:
        header["payload_length"] = len(payload)
    return json.dumps(header).encode() + b"\n" + body + payload


async def read_event(reader):
    line = await reader.readline()
    if not line:
        return None
    header = json.loads(line)
    data = json.loads(await reader.readexactly(header.get("data_length", 0)) or b"{}")
    payload = await reader.readexactly(header.get("payload_length", 0))
    return header["type"], data, payload


def serve(handler, body):
    """Run body(port) against a server whose connections go to handler(reader, writer)."""

    async def run():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        try:
            return await body(server.sockets[0].getsockname()[1])
        finally:
            server.close()

    return asyncio.run(run())


async def text(*pieces):
    for piece in pieces:
        yield piece


AUDIO = {"rate": 22050, "width": 2, "channels": 1}


class Describe(unittest.TestCase):
    def check(self, reply, timeout=0.5):
        async def handle(reader, writer):
            await read_event(reader)
            if reply is None:
                await asyncio.sleep(5)
                return
            writer.write(reply)
            await writer.drain()
            writer.close()

        return serve(handle, lambda port: W.describe("127.0.0.1", port, timeout))

    def test_info_reply_is_returned(self):
        self.assertEqual(self.check(event("info", {"asr": []})), {"asr": []})

    def test_very_long_info_is_read(self):
        info = self.check(event("info", {"pad": "x" * 200000}))
        self.assertEqual(len(info["pad"]), 200000)

    def test_open_port_that_says_something_else_is_down(self):
        for reply in (event("error", {"text": "no"}), event("other"), b"not json\n", b"[1]\n", b""):
            self.assertIsNone(self.check(reply), reply)

    def test_info_cut_short_is_down(self):
        self.assertIsNone(self.check(b'{"type": "info", "data_length": 50}\n{"a"'))

    def test_silent_server_times_out(self):
        self.assertIsNone(self.check(None, timeout=0.05))

    def test_closed_port_is_down(self):
        async def run():
            server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server.close()
            await server.wait_closed()
            return await W.describe("127.0.0.1", port, 0.5)
        self.assertIsNone(asyncio.run(run()))


class Transcribe(unittest.TestCase):
    def run_with(self, finish):
        seen = []

        async def handle(reader, writer):
            while (item := await read_event(reader)) is not None:
                seen.append(item)
                if item[0] == "audio-stop":
                    break
            writer.write(finish)
            await writer.drain()
            writer.close()

        async def body(port):
            return await W.transcribe("127.0.0.1", port, "en", [b"aa", b"bb"])

        return serve(handle, body), seen

    def test_sends_the_clip_in_order_and_returns_the_transcript(self):
        result, seen = self.run_with(event("transcript", {"text": " hello"}))
        self.assertEqual(result, " hello")
        self.assertEqual([s[0] for s in seen], ["transcribe", "audio-start", "audio-chunk", "audio-chunk", "audio-stop"])
        self.assertEqual(seen[0][1], {"language": "en"})
        self.assertEqual(seen[1][1], {"rate": 16000, "width": 2, "channels": 1})
        self.assertEqual([s[2] for s in seen[2:4]], [b"aa", b"bb"])

    def test_empty_transcript_is_an_answer(self):
        self.assertEqual(self.run_with(event("transcript", {"text": ""}))[0], "")

    def test_server_error_raises(self):
        with self.assertRaises(W.WyomingError):
            self.run_with(event("error", {"text": "boom"}))

    def test_hangup_raises(self):
        with self.assertRaises(W.WyomingError):
            self.run_with(b"")


class Synthesize(unittest.TestCase):
    def run_with(self, replies, streaming, voice=None, wait_for="synthesize"):
        seen = []

        async def handle(reader, writer):
            while (item := await read_event(reader)) is not None:
                seen.append(item)
                if item[0] == wait_for:
                    break
            writer.write(b"".join(replies))
            await writer.drain()
            writer.close()

        async def body(port):
            return [c async for c in W.synthesize("127.0.0.1", port, text("Hello ", "there."), voice, streaming)]

        return serve(handle, body), seen

    def test_plain_server_gets_the_joined_text_and_ends_on_audio_stop(self):
        chunks, seen = self.run_with(
            [event("audio-start", AUDIO), event("audio-chunk", AUDIO, b"pcm"), event("audio-stop"), event("audio-chunk", AUDIO, b"late")],
            False,
            {"name": "v"},
        )
        self.assertEqual([s[0] for s in seen], ["synthesize"])
        self.assertEqual(seen[0][1], {"text": "Hello there.", "voice": {"name": "v"}})
        self.assertEqual(chunks[0][:4], b"RIFF")
        self.assertEqual(chunks[1:], [b"pcm"])

    def test_header_describes_the_audio(self):
        chunks, _ = self.run_with([event("audio-start", AUDIO), event("audio-stop")], False)
        self.assertEqual(int.from_bytes(chunks[0][24:28], "little"), 22050)

    def test_streaming_server_gets_pieces_and_continues_past_audio_stop(self):
        chunks, seen = self.run_with(
            [
                event("audio-start", AUDIO), event("audio-chunk", AUDIO, b"one"), event("audio-stop"),
                event("audio-start", AUDIO), event("audio-chunk", AUDIO, b"two"), event("audio-stop"),
                event("synthesize-stopped"),
            ],
            True,
            wait_for="synthesize-stop",
        )
        self.assertEqual([s[0] for s in seen], ["synthesize-start", "synthesize-chunk", "synthesize-chunk", "synthesize", "synthesize-stop"])
        self.assertEqual([s[1]["text"] for s in seen[1:3]], ["Hello ", "there."])
        self.assertEqual(chunks[1:], [b"one", b"two"])            # one header only

    def test_error_after_starting_raises(self):
        with self.assertRaises(W.WyomingError):
            self.run_with([event("audio-start", AUDIO), event("error", {"text": "boom"})], False)

    def test_hangup_raises(self):
        with self.assertRaises(W.WyomingError):
            self.run_with([b""], False)

    def test_chunk_before_start_is_not_passed_on(self):
        chunks, _ = self.run_with([event("audio-chunk", AUDIO, b"stray"), event("audio-stop")], False)
        self.assertEqual(chunks, [])


class Info(unittest.TestCase):
    INFO = {
        "asr": [{"models": [{"installed": True, "languages": ["en"]}, {"installed": False, "languages": ["fr"]}]}],
        "tts": [{"supports_synthesize_streaming": True, "voices": [
            {"name": "a", "description": "Voice A", "installed": True, "languages": ["en_US"]},
            {"name": "b", "installed": True, "languages": ["en_US", "en_GB"]},
            {"name": "c", "installed": False, "languages": ["de_DE"]},
        ]}],
    }

    def test_languages_skip_what_is_not_installed(self):
        self.assertEqual(W.languages(self.INFO, "asr"), {"en"})
        self.assertEqual(W.languages(self.INFO, "tts"), {"en_US", "en_GB"})

    def test_no_info_means_no_languages(self):
        self.assertEqual(W.languages(None, "tts"), set())

    def test_voices_for_a_language(self):
        self.assertEqual(W.voices(self.INFO, "en_US"), [("a", "Voice A"), ("b", "b")])
        self.assertEqual(W.voices(self.INFO, "de_DE"), [])

    def test_streaming_support(self):
        self.assertTrue(W.supports_streaming(self.INFO))
        self.assertFalse(W.supports_streaming({"tts": [{}]}))
        self.assertFalse(W.supports_streaming(None))


class ParseAddress(unittest.TestCase):
    def test_good(self):
        self.assertEqual(W.parse_address("192.168.1.2:10300"), ("192.168.1.2", 10300))
        self.assertEqual(W.parse_address(" pc.local:10200 "), ("pc.local", 10200))

    def test_bad(self):
        for value in ("", None, "pc", ":10300", "pc:", "pc:abc", "pc:0", "pc:70000"):
            self.assertIsNone(W.parse_address(value), value)


if __name__ == "__main__":
    unittest.main()
