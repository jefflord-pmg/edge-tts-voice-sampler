import argparse
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import voice_samples


class PositiveIntTests(unittest.TestCase):
    def test_accepts_positive_integers(self):
        self.assertEqual(voice_samples.positive_int("3"), 3)

    def test_rejects_zero_and_non_integer_values(self):
        for value in ("0", "-2", "1.5", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    voice_samples.positive_int(value)


class WebSocketTraceTests(unittest.TestCase):
    def test_records_message_paths_and_audio_byte_count(self):
        trace = voice_samples.WebSocketTrace()
        websocket = SimpleNamespace(close_code=None)
        for path in ("turn.start", "audio.metadata", "turn.end"):
            message = SimpleNamespace(
                type=SimpleNamespace(name="TEXT"),
                data=f"Path:{path}\r\n\r\n{{}}",
            )
            trace.record(message, websocket)

        header = b"Path:audio\r\nContent-Type:audio/mpeg"
        packet = len(header).to_bytes(2, "big") + header + b"12345"
        trace.record(
            SimpleNamespace(type=SimpleNamespace(name="BINARY"), data=packet),
            websocket,
        )

        self.assertEqual(trace.turn_starts, 1)
        self.assertEqual(trace.turn_ends, 1)
        self.assertEqual(trace.audio_bytes, 5)
        self.assertIn("turn.end", trace.summary())
        self.assertIn("5 audio bytes", trace.summary())

    def test_diagnoses_turn_ended_without_audio(self):
        trace = voice_samples.WebSocketTrace()
        websocket = SimpleNamespace(close_code=None)
        trace.record(
            SimpleNamespace(
                type=SimpleNamespace(name="TEXT"),
                data="Path:turn.end\r\n\r\n{}",
            ),
            websocket,
        )
        self.assertIn("without sending audio", trace.diagnosis())

    def test_diagnoses_audio_without_a_complete_turn(self):
        trace = voice_samples.WebSocketTrace()
        trace.audio_bytes = 12
        self.assertIn("may be cut off", trace.diagnosis())


class AtomicSaveTests(unittest.TestCase):
    def test_replaces_existing_file_after_audio_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.mp3"
            path.write_bytes(b"old audio")

            voice_samples.atomic_save(path, b"complete new audio")

            self.assertEqual(path.read_bytes(), b"complete new audio")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_preserves_existing_file_if_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.mp3"
            path.write_bytes(b"old audio")

            with patch.object(voice_samples.os, "replace", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    voice_samples.atomic_save(path, b"new audio")

            self.assertEqual(path.read_bytes(), b"old audio")
            self.assertEqual(list(Path(directory).iterdir()), [path])


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_then_returns_audio(self):
        trace = voice_samples.WebSocketTrace()
        failure = voice_samples.AttemptFailure(
            voice_samples.edge_tts.exceptions.NoAudioReceived("no audio"),
            trace,
            0.2,
            retryable=True,
        )
        with (
            patch(
                "voice_samples.synthesize_once",
                new_callable=AsyncMock,
                side_effect=[failure, failure, (b"audio", trace, 0.1)],
            ) as synthesize,
            patch("voice_samples.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("builtins.print"),
        ):
            result = await voice_samples.synthesize_with_retries(
                "hello", "en-US-AndrewNeural", 3, 30, False
            )

        self.assertEqual(result, b"audio")
        self.assertEqual(synthesize.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])

    async def test_does_not_retry_non_transient_errors(self):
        trace = voice_samples.WebSocketTrace()
        failure = voice_samples.AttemptFailure(
            ValueError("bad input"), trace, 0.1, retryable=False
        )
        with (
            patch(
                "voice_samples.synthesize_once",
                new_callable=AsyncMock,
                side_effect=failure,
            ) as synthesize,
            patch("voice_samples.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("builtins.print"),
        ):
            result = await voice_samples.synthesize_with_retries(
                "hello", "en-US-AndrewNeural", 3, 30, False
            )

        self.assertIsNone(result)
        self.assertEqual(synthesize.await_count, 1)
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
