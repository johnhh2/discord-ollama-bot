"""Decoding an attached audio file for !announce: ffmpeg turns whatever was
uploaded (mp3, ogg, m4a, wav, flac, ...) into the 48 kHz 16-bit stereo PCM
Discord plays, the same bytes the text-to-speech path hands to the player.

The upload is written under the temp dir (/tmp, the container's one
writable path) rather than piped in: an m4a's index can sit at the end of
the file, which ffmpeg can't reach through a pipe.
"""
import asyncio
import os
import shutil
import tempfile

from src.tts import DISCORD_RATE, DISCORD_CHANNELS

FFMPEG = "ffmpeg"
DECODE_TIMEOUT = 60.0
# Accepted by extension when Discord reports no audio or video content type
# (it often doesn't for voice messages and some mobile uploads). A video
# plays its soundtrack: ffmpeg runs with `-vn`.
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".wma", ".aiff",
    ".mp4", ".m4v", ".mov", ".webm", ".mkv",
})


class DecodeError(Exception):
    pass


def ffmpeg_available() -> bool:
    return shutil.which(FFMPEG) is not None


def is_audio_attachment(attachment) -> bool:
    ctype = (getattr(attachment, "content_type", None) or "").split(";")[0].strip().lower()
    if ctype.startswith(("audio/", "video/")):
        return True
    return _suffix(getattr(attachment, "filename", None) or "") != ""


def _suffix(filename: str) -> str:
    """The upload's extension if it's a known audio one, else empty — the
    filename is user-supplied, so nothing else from it reaches the disk."""
    ext = os.path.splitext(filename.lower())[1]
    return ext if ext in AUDIO_EXTENSIONS else ""


async def decode_audio(data: bytes, *, max_secs: float, filename: str = "") -> bytes:
    """PCM for Discord from the bytes of an audio file, cut at `max_secs`
    (ffmpeg's `-t`, so a long upload isn't decoded whole). Raises
    DecodeError when ffmpeg is missing, fails, times out or yields nothing."""
    fd, path = tempfile.mkstemp(prefix="announce-", suffix=_suffix(filename) or ".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            proc = await asyncio.create_subprocess_exec(
                FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", path, "-t", f"{max_secs:g}", "-vn",
                "-f", "s16le", "-ar", str(DISCORD_RATE), "-ac", str(DISCORD_CHANNELS), "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise DecodeError("ffmpeg isn't installed, so I can't decode audio files") from None
        try:
            pcm, err = await asyncio.wait_for(proc.communicate(), timeout=DECODE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise DecodeError("decoding took too long") from None
        if proc.returncode != 0 or not pcm:
            lines = err.decode(errors="replace").replace(path, "the file").strip().splitlines()
            raise DecodeError(lines[-1] if lines else "ffmpeg couldn't decode it")
        return pcm
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
