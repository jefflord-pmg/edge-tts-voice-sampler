"""Interactively type a sentence and hear it in a selected US English Edge voice.

Run with:  py voice_samples.py
Options:   py voice_samples.py --help
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    import aiohttp
    import edge_tts
except ImportError as error:
    sys.exit(
        "Missing dependency. Install the requirements with: "
        f"py -m pip install -r requirements.txt\nDetails: {error}"
    )


VOICES = [
    "en-US-AnaNeural",
    "en-US-AndrewNeural",
    "en-US-AriaNeural",
    "en-US-AvaNeural",
    "en-US-BrianNeural",
    "en-US-ChristopherNeural",
    "en-US-EmmaNeural",
    "en-US-EricNeural",
    "en-US-GuyNeural",
    "en-US-JennyNeural",
    "en-US-MichelleNeural",
    "en-US-RogerNeural",
    "en-US-SteffanNeural",
]

DEFAULT_TEXT = "Hello! It's a lovely day to learn something new."
DEFAULT_ATTEMPTS = 3
DEFAULT_ATTEMPT_TIMEOUT = 30


@dataclass
class TraceEvent:
    elapsed: float
    name: str
    detail: str = ""


class WebSocketTrace:
    """Record WebSocket message types without logging speech text or payloads."""

    def __init__(self) -> None:
        self.started_at = time.perf_counter()
        self.events: list[TraceEvent] = []
        self.audio_bytes = 0
        self.close_code: int | None = None
        self.turn_starts = 0
        self.turn_ends = 0

    def record(self, message: object, websocket: object) -> None:
        """Capture a short, privacy-safe description of a received message."""
        try:
            message_type = message.type.name  # type: ignore[attr-defined]
            name = message_type.lower()
            detail = ""

            if message_type == "TEXT":
                header = message.data.partition("\r\n\r\n")[0]  # type: ignore[attr-defined]
                path = next(
                    (
                        line.split(":", 1)[1].strip()
                        for line in header.split("\r\n")
                        if line.lower().startswith("path:")
                    ),
                    "text",
                )
                name = path
                if path == "turn.start":
                    self.turn_starts += 1
                elif path == "turn.end":
                    self.turn_ends += 1

            elif message_type == "BINARY":
                packet = message.data  # type: ignore[attr-defined]
                if len(packet) >= 2:
                    header_length = int.from_bytes(packet[:2], "big")
                    if 2 + header_length <= len(packet):
                        header = packet[2 : 2 + header_length].decode("ascii", "replace")
                        fields = {}
                        for line in header.split("\r\n"):
                            if ":" in line:
                                key, value = line.split(":", 1)
                                fields[key.lower()] = value.strip().lower()
                        if fields.get("path") == "audio":
                            name = "audio"
                            if fields.get("content-type") == "audio/mpeg":
                                self.audio_bytes += len(packet) - 2 - header_length
                                detail = f"{len(packet) - 2 - header_length} bytes"
                        else:
                            name = "binary"

            elif message_type in {"CLOSE", "CLOSING", "CLOSED"}:
                self.close_code = getattr(websocket, "close_code", None)
                if self.close_code is not None:
                    detail = f"close code {self.close_code}"

            self.events.append(
                TraceEvent(time.perf_counter() - self.started_at, name, detail)
            )
        except Exception as error:
            # Diagnostics must never interfere with speech generation.
            self.events.append(
                TraceEvent(
                    time.perf_counter() - self.started_at,
                    "trace-error",
                    type(error).__name__,
                )
            )

    def summary(self) -> str:
        counts = Counter(event.name for event in self.events if event.name != "audio")
        messages = ", ".join(f"{name} x{count}" for name, count in counts.items())
        if not messages:
            messages = "no WebSocket messages observed"
        audio_frames = sum(event.name == "audio" for event in self.events)
        if audio_frames:
            messages += f"; {audio_frames} audio frame(s)"
        messages += f"; {self.audio_bytes:,} audio bytes"
        if self.close_code is not None:
            messages += f"; close code {self.close_code}"
        return messages

    def diagnosis(self) -> str:
        if self.audio_bytes and (
            self.turn_starts == 0 or self.turn_starts != self.turn_ends
        ):
            return "Audio arrived, but a speech turn did not complete; the sample may be cut off."
        if self.audio_bytes:
            return "Audio arrived; check the detailed trace for where the request ended."
        if self.turn_ends:
            return (
                "The service ended the speech turn without sending audio. "
                "This usually points to an upstream voice/service or request issue."
            )
        if self.turn_starts:
            if self.close_code == 1006:
                return (
                    "The service started the turn, but the WebSocket closed abnormally "
                    "before sending audio; this may be an upstream or network interruption."
                )
            return "The service started the turn but sent no audio before the connection ended."
        if self.events:
            return (
                "The connection closed before a speech turn started; "
                "check the network or service."
            )
        return (
            "No WebSocket messages were observed; "
            "check the network, proxy, or service availability."
        )

    def print_debug(self) -> None:
        if not self.events:
            print("    WebSocket trace: no messages observed")
            return
        print("    WebSocket trace:")
        for event in self.events:
            suffix = f" ({event.detail})" if event.detail else ""
            print(f"      +{event.elapsed:6.2f}s {event.name}{suffix}")


class AttemptFailure(Exception):
    def __init__(
        self,
        cause: Exception,
        trace: WebSocketTrace,
        elapsed: float,
        retryable: bool,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.trace = trace
        self.elapsed = elapsed
        self.retryable = retryable


class IncompleteAudioError(Exception):
    """Raised when audio arrived but its WebSocket speech turn did not finish."""


@contextmanager
def observe_websocket_messages(trace: WebSocketTrace) -> Iterator[None]:
    """Temporarily observe aiohttp WebSocket messages used by edge-tts.

    edge-tts does not currently expose its WebSocket response sequence. This
    narrowly scoped hook records only message paths, byte counts, timings, and
    close codes. It never logs message bodies, SSML, or audio data.
    """
    websocket_class = aiohttp.ClientWebSocketResponse
    original_receive = websocket_class.receive

    async def observed_receive(websocket: object, *args: object, **kwargs: object) -> object:
        message = await original_receive(websocket, *args, **kwargs)  # type: ignore[arg-type]
        trace.record(message, websocket)
        return message

    websocket_class.receive = observed_receive  # type: ignore[method-assign]
    try:
        yield
    finally:
        websocket_class.receive = original_receive  # type: ignore[method-assign]


async def synthesize_once(
    text: str, voice: str, attempt_timeout: int
) -> tuple[bytes, WebSocketTrace, float]:
    trace = WebSocketTrace()
    audio = bytearray()
    started_at = time.perf_counter()
    communicate = edge_tts.Communicate(
        text,
        voice,
        connect_timeout=min(10, attempt_timeout),
        receive_timeout=attempt_timeout,
    )

    async def collect_audio() -> None:
        async for message in communicate.stream():
            if message["type"] == "audio":
                audio.extend(message["data"])

    try:
        with observe_websocket_messages(trace):
            await asyncio.wait_for(collect_audio(), timeout=attempt_timeout)
    except Exception as error:
        elapsed = time.perf_counter() - started_at
        retryable = isinstance(
            error,
            (
                edge_tts.exceptions.EdgeTTSException,
                aiohttp.ClientError,
                OSError,
                TimeoutError,
            ),
        )
        raise AttemptFailure(error, trace, elapsed, retryable) from error

    elapsed = time.perf_counter() - started_at
    if not audio:
        error = edge_tts.exceptions.NoAudioReceived("The service returned no audio data.")
        raise AttemptFailure(error, trace, elapsed, retryable=True)
    if trace.turn_starts == 0 or trace.turn_starts != trace.turn_ends:
        error = IncompleteAudioError(
            f"Received {len(audio):,} audio bytes, but the speech turn did not complete."
        )
        raise AttemptFailure(error, trace, elapsed, retryable=True)

    return bytes(audio), trace, elapsed


async def synthesize_with_retries(
    text: str,
    voice: str,
    attempts: int,
    attempt_timeout: int,
    debug: bool,
) -> bytes | None:
    for attempt_number in range(1, attempts + 1):
        try:
            audio, trace, elapsed = await synthesize_once(
                text, voice, attempt_timeout
            )
        except AttemptFailure as failure:
            print(
                f"  Attempt {attempt_number}/{attempts} failed "
                f"after {failure.elapsed:.1f}s: "
                f"{type(failure.cause).__name__}: {failure.cause}"
            )
            print(f"    WebSocket: {failure.trace.summary()}")
            print(f"    Diagnosis: {failure.trace.diagnosis()}")
            if debug:
                failure.trace.print_debug()

            if not failure.retryable:
                print("    Not retrying this non-transient error.")
                return None
            if attempt_number == attempts:
                print("    No attempts remain; any previous sample was left unchanged.")
                return None

            delay = attempt_number
            print(f"    Retrying in {delay}s...")
            await asyncio.sleep(delay)
        else:
            print(
                f"  Attempt {attempt_number}/{attempts} succeeded "
                f"in {elapsed:.1f}s ({len(audio):,} audio bytes)."
            )
            if debug:
                trace.print_debug()
            return audio

    return None


def atomic_save(path: Path, audio: bytes) -> None:
    """Replace a sample only after its complete audio has been written."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(audio)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive whole number") from error
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", help="Start with this sentence instead of prompting")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("voice_samples"),
        help="Folder for generated MP3 files (default: voice_samples)",
    )
    parser.add_argument(
        "--attempts",
        type=positive_int,
        default=DEFAULT_ATTEMPTS,
        help=f"Maximum tries per voice (default: {DEFAULT_ATTEMPTS})",
    )
    parser.add_argument(
        "--attempt-timeout",
        type=positive_int,
        default=DEFAULT_ATTEMPT_TIMEOUT,
        help=f"Maximum seconds per try (default: {DEFAULT_ATTEMPT_TIMEOUT})",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print timed WebSocket message details for successful and failed tries",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    player = shutil.which("ffplay")
    if player is None:
        print("ffplay was not found; samples will be saved but cannot play automatically.")

    text = args.text
    if text is None:
        entered_text = input(
            f"What should the voice say? (Enter for default: {DEFAULT_TEXT})\n> "
        ).strip()
        text = entered_text or DEFAULT_TEXT
    else:
        text = text.strip() or DEFAULT_TEXT

    while True:
        print(f"\nCurrent sentence: {text}")
        for index, voice in enumerate(VOICES, start=1):
            print(f"  {index:2}. {voice}")
        choice = input(
            "\nChoose a voice number, [t] change the sentence, or [q] quit: "
        ).strip().lower()

        if choice in {"q", "quit", "exit"}:
            break
        if choice in {"t", "text"}:
            updated_text = input("Type the new sentence:\n> ").strip()
            if updated_text:
                text = updated_text
            else:
                print("Sentence unchanged.")
            continue

        try:
            voice_number = int(choice)
            if not 1 <= voice_number <= len(VOICES):
                raise ValueError
            voice = VOICES[voice_number - 1]
        except ValueError:
            print(f"Enter a number from 1 to {len(VOICES)}, t, or q.")
            continue

        print(f"Generating {voice}...")
        audio = await synthesize_with_retries(
            text,
            voice,
            attempts=args.attempts,
            attempt_timeout=args.attempt_timeout,
            debug=args.debug,
        )
        if audio is None:
            continue

        path = args.output_dir / f"{voice}.mp3"
        try:
            atomic_save(path, audio)
        except OSError as error:
            print(f"Could not save the sample: {error}")
            print("Any previous sample was left unchanged.")
            continue

        print(f"Saved: {path.resolve()}")
        if player is None:
            continue
        playback = subprocess.run(
            [player, "-nodisp", "-autoexit", "-loglevel", "error", str(path)],
            check=False,
        )
        if playback.returncode != 0:
            print(f"ffplay exited with code {playback.returncode}.")

    print("Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
