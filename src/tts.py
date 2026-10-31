"""Wyoming text-to-speech client for !announce, and the PCM conversion
Discord's voice encoder wants.

Wyoming (the protocol Piper and the other Rhasspy/Home Assistant voice
services speak) is one TCP connection carrying events: a JSON header line —
`type`, optional `data_length` and `payload_length` — then that many bytes of
JSON data and raw payload. A `synthesize` event is answered with
`audio-start`, `audio-chunk`s (16-bit PCM payloads) and `audio-stop`. The
protocol is small enough that a client here beats a dependency to audit.

Older servers put `data` inline in the header instead of after it;
`read_event` accepts both.
"""
from __future__ import annotations

import asyncio
import audioop
import json
from dataclasses import dataclass

DISCORD_RATE = 48000
DISCORD_CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes: 16-bit

CONNECT_TIMEOUT = 5.0
SYNTH_TIMEOUT = 30.0


class TTSError(Exception):
    """The server was unreachable, answered with an error, or sent no audio."""


@dataclass
class Audio:
    pcm: bytes
    rate: int
    width: int
    channels: int

    def to_discord(self) -> bytes:
        return to_discord_pcm(self.pcm, self.rate, self.width, self.channels)


def to_discord_pcm(pcm: bytes, rate: int, width: int, channels: int) -> bytes:
    """Any-rate mono/stereo PCM → 48 kHz 16-bit stereo, what `discord.PCMAudio`
    expects. stdlib `audioop` rather than ffmpeg: nothing to install, and
    nothing written to disk (the container's rootfs is read-only)."""
    if channels not in (1, 2):
        raise TTSError(f"unsupported channel count {channels}")
    if width != SAMPLE_WIDTH:
        pcm = audioop.lin2lin(pcm, width, SAMPLE_WIDTH)
    if rate != DISCORD_RATE:
        pcm, _ = audioop.ratecv(pcm, SAMPLE_WIDTH, channels, rate, DISCORD_RATE, None)
    if channels == 1:
        pcm = audioop.tostereo(pcm, SAMPLE_WIDTH, 1, 1)
    return pcm


def encode_event(type_: str, data: dict | None = None, payload: bytes = b"") -> bytes:
    header: dict = {"type": type_}
    data_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else b""
    if data_bytes:
        header["data_length"] = len(data_bytes)
    if payload:
        header["payload_length"] = len(payload)
    return json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n" + data_bytes + payload


async def read_event(reader: asyncio.StreamReader) -> tuple[str, dict, bytes]:
    """One event as `(type, data, payload)`. Raises `TTSError` at EOF."""
    line = await reader.readline()
    if not line:
        raise TTSError("the TTS server closed the connection")
    try:
        header = json.loads(line)
    except ValueError as e:
        raise TTSError(f"malformed event header: {e}") from e
    data = dict(header.get("data") or {})
    data_length = int(header.get("data_length") or 0)
    if data_length:
        data.update(json.loads(await reader.readexactly(data_length)))
    payload_length = int(header.get("payload_length") or 0)
    payload = await reader.readexactly(payload_length) if payload_length else b""
    return str(header.get("type", "")), data, payload


class WyomingTTS:
    def __init__(self, host: str, port: int, voice: str = ""):
        self.host = host
        self.port = port
        self.voice = voice  # empty: the server's default

    async def synthesize(self, text: str) -> Audio:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as e:
            raise TTSError(f"can't reach the TTS server at {self.host}:{self.port} ({e or 'timed out'})") from e
        try:
            data: dict = {"text": text}
            if self.voice:
                data["voice"] = {"name": self.voice}
            writer.write(encode_event("synthesize", data))
            await writer.drain()
            try:
                return await asyncio.wait_for(self._collect(reader), SYNTH_TIMEOUT)
            except asyncio.TimeoutError as e:
                raise TTSError("the TTS server took too long") from e
            except asyncio.IncompleteReadError as e:
                raise TTSError("the TTS server closed the connection mid-reply") from e
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass  # we're done with it either way

    @staticmethod
    async def _collect(reader: asyncio.StreamReader) -> Audio:
        chunks: list[bytes] = []
        fmt: tuple[int, int, int] | None = None
        while True:
            type_, data, payload = await read_event(reader)
            if type_ in ("audio-start", "audio-chunk"):
                if fmt is None and "rate" in data:
                    fmt = (int(data["rate"]), int(data.get("width") or SAMPLE_WIDTH), int(data.get("channels") or 1))
                if type_ == "audio-chunk" and payload:
                    chunks.append(payload)
            elif type_ == "audio-stop":
                break
            elif type_ == "error":
                raise TTSError(data.get("text") or "the TTS server reported an error")
            # Anything else (info, pings) is ignored.
        if not chunks or fmt is None:
            raise TTSError("the TTS server returned no audio")
        return Audio(b"".join(chunks), *fmt)
