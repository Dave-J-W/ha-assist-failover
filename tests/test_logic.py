"""logic.py: which engine answers, what makes it move on, and what counts as alive."""

import asyncio
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("af_logic", ROOT / "custom_components/assist_failover/logic.py")
L = importlib.util.module_from_spec(_spec)
sys.modules["af_logic"] = L          # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(L)


async def reach(value):
    return value


async def gen(*items):
    for item in items:
        yield item


class FirstAnswer(unittest.TestCase):
    def setUp(self):
        self.ran = []

    def attempt(self, name, result, timeout=1.0, reachable=None):
        async def run():
            self.ran.append(name)
            if isinstance(result, Exception):
                raise result
            return result
        return L.Attempt(name, run, timeout, reachable)

    def go(self, *attempts):
        return asyncio.run(L.first_answer(list(attempts)))

    def test_primary_answers_and_backup_never_runs(self):
        o = self.go(self.attempt("pc", "hi"), self.attempt("pi", "no"))
        self.assertEqual((o.value, o.engine, o.failures), ("hi", "pc", []))
        self.assertEqual(self.ran, ["pc"])

    def test_empty_answer_is_an_answer(self):
        o = self.go(self.attempt("pc", ""), self.attempt("pi", "no"))
        self.assertEqual((o.value, o.engine), ("", "pc"))

    def test_error_result_falls_back(self):
        o = self.go(self.attempt("pc", None), self.attempt("pi", "hi"))
        self.assertEqual((o.value, o.engine), ("hi", "pi"))
        self.assertIn("pc: returned an error", o.failures)

    def test_exception_falls_back(self):
        o = self.go(self.attempt("pc", OSError("refused")), self.attempt("pi", "hi"))
        self.assertEqual(o.engine, "pi")
        self.assertIn("refused", o.failures[0])

    def test_unreachable_skips_the_engine(self):
        o = self.go(self.attempt("pc", "no", reachable=lambda: reach(False)), self.attempt("pi", "hi"))
        self.assertEqual(o.engine, "pi")
        self.assertEqual(self.ran, ["pi"])
        self.assertEqual(o.failures, ["pc: unreachable"])

    def test_hung_engine_times_out_and_falls_back(self):
        async def hang():
            await asyncio.sleep(30)
        o = self.go(L.Attempt("pc", hang, 0.01), self.attempt("pi", "hi"))
        self.assertEqual(o.engine, "pi")
        self.assertIn("timed out", o.failures[0])

    def test_all_failed_reports_no_engine(self):
        o = self.go(self.attempt("pc", None), self.attempt("pi", None))
        self.assertIsNone(o.engine)
        self.assertIsNone(o.value)
        self.assertEqual(len(o.failures), 2)


class Replay(unittest.TestCase):
    def test_second_reader_gets_everything_the_first_consumed(self):
        async def run():
            text = L.ReplayableText(gen("a", "b", "c"))
            first = text.reader()
            got_first = [await anext(first)]          # the primary reads one piece, then dies
            got_second = [c async for c in text.reader()]
            return got_first, got_second
        self.assertEqual(asyncio.run(run()), (["a"], ["a", "b", "c"]))

    def test_source_is_read_only_once(self):
        pulls = []

        async def source():
            for item in ("a", "b"):
                pulls.append(item)
                yield item

        async def run():
            text = L.ReplayableText(source())
            await text.wait_for_text()
            return [c async for c in text.reader()], [c async for c in text.reader()]
        self.assertEqual(asyncio.run(run()), (["a", "b"], ["a", "b"]))
        self.assertEqual(pulls, ["a", "b"])

    def test_empty_message_ends_both_readers(self):
        async def run():
            text = L.ReplayableText(gen())
            await text.wait_for_text()
            return [c async for c in text.reader()]
        self.assertEqual(asyncio.run(run()), [])

    def test_reader_gets_text_as_it_arrives_not_at_the_end(self):
        release = asyncio.Event

        async def run():
            gate = release()

            async def slow():
                yield "first"
                await gate.wait()
                yield "second"

            text = L.ReplayableText(slow())
            reader = text.reader()
            first = await asyncio.wait_for(anext(reader), 1)
            gate.set()
            return first, await anext(reader)
        self.assertEqual(asyncio.run(run()), ("first", "second"))


class FirstAudio(unittest.TestCase):
    def go(self, *chunks):
        async def run():
            started = await L.first_audio(gen(*chunks))
            if started is None:
                return None
            return started[0], [c async for c in started[1]]
        return asyncio.run(run())

    def test_header_alone_is_not_audio(self):
        self.assertIsNone(self.go(b"h" * 44))

    def test_nothing_at_all_is_not_audio(self):
        self.assertIsNone(self.go())

    def test_header_then_audio_keeps_every_chunk_in_order(self):
        audio = b"a" * 2000
        self.assertEqual(self.go(b"h" * 44, audio, b"tail"), ([b"h" * 44, audio], [b"tail"]))

    def test_a_single_whole_file_counts(self):
        whole = b"f" * 5000
        self.assertEqual(self.go(whole), ([whole], []))


class OptionsFor(unittest.TestCase):
    def test_drops_what_the_engine_does_not_take(self):
        self.assertEqual(L.options_for({"voice": "v", "x": 1}, ["voice"]), {"voice": "v"})

    def test_engine_with_no_options(self):
        self.assertEqual(L.options_for({"voice": "v"}, None), {})


class ServerAlive(unittest.TestCase):
    def check(self, reply, timeout=0.5):
        async def run():
            async def handle(reader, writer):
                await reader.readline()
                if reply is not None:
                    writer.write(reply)
                    await writer.drain()
                    writer.close()
                else:
                    await asyncio.sleep(5)

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            alive = await L.server_alive("127.0.0.1", port, timeout)
            server.close()
            return alive
        return asyncio.run(run())

    def test_info_reply_is_alive(self):
        self.assertTrue(self.check(b'{"type": "info", "data_length": 2}\n{}'))

    def test_a_very_long_info_line_is_still_alive(self):
        self.assertTrue(self.check(b'{"type": "info", "pad": "' + b"x" * 200000 + b'"}\n'))

    def test_open_port_that_says_something_else_is_not(self):
        self.assertFalse(self.check(b'{"type": "error"}\n'))
        self.assertFalse(self.check(b'not json\n'))
        self.assertFalse(self.check(b'[1]\n'))
        self.assertFalse(self.check(b''))

    def test_silent_server_times_out(self):
        self.assertFalse(self.check(None, timeout=0.05))

    def test_closed_port_is_not_alive(self):
        async def run():
            server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server.close()
            await server.wait_closed()
            return await L.server_alive("127.0.0.1", port, 0.5)
        self.assertFalse(asyncio.run(run()))


if __name__ == "__main__":
    unittest.main()
