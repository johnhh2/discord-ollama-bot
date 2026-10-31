"""Audio for !adminannounce that isn't text-to-speech: an attached file, or a
YouTube link. ffmpeg turns either into the 48 kHz 16-bit stereo PCM Discord
plays, the same bytes the text-to-speech path hands to the player.

An upload is written under the temp dir (/tmp, the container's one writable
path) rather than piped in: an m4a's index can sit at the end of the file,
which ffmpeg can't reach through a pipe. A YouTube link is resolved by the
standalone yt-dlp binary (plus Deno, its JS runtime, for the player
challenge) to a direct media URL that ffmpeg then reads, cut at the clip
cap, so only that much is downloaded.
"""
import asyncio
import os
import re
import shutil
import tempfile

from src.config import YTDLP_PATH, YTDLP_TMPDIR
from src.tts import DISCORD_RATE, DISCORD_CHANNELS

FFMPEG = "ffmpeg"
YTDLP = YTDLP_PATH
DECODE_TIMEOUT = 60.0
RESOLVE_TIMEOUT = 60.0  # yt-dlp fetching the page and solving the challenge
STREAM_TIMEOUT = 180.0  # ffmpeg pulling the clip from Google's CDN
UPDATE_TIMEOUT = 180.0  # yt-dlp -U downloading its ~40 MB replacement
# yt-dlp -U replaces the binary in place; a resolve that starts mid-rename
# would exec a half-written file, so updates and resolves share a lock.
_ytdlp_lock = asyncio.Lock()
# Accepted by extension when Discord reports no audio or video content type
# (it often doesn't for voice messages and some mobile uploads). A video
# plays its soundtrack: ffmpeg runs with `-vn`.
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".wma", ".aiff",
    ".mp4", ".m4v", ".mov", ".webm", ".mkv",
})
# YouTube only — yt-dlp speaks to a thousand sites, but a bot that fetches
# any URL an admin pastes is a proxy into the LAN it runs on.
YOUTUBE_RE = re.compile(
    r"<?(https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?[^\s>]*v=|shorts/|live/)[\w-]{11}[^\s>]*"
    r"|youtu\.be/[\w-]{11}[^\s>]*))>?",  # <url> is Discord's no-embed form
    re.IGNORECASE,
)
_OUTPUT_ARGS = ("-vn", "-f", "s16le", "-ar", str(DISCORD_RATE), "-ac", str(DISCORD_CHANNELS), "pipe:1")


class DecodeError(Exception):
    pass


def ffmpeg_available() -> bool:
    return shutil.which(FFMPEG) is not None


def ytdlp_available() -> bool:
    return shutil.which(YTDLP) is not None


def is_audio_attachment(attachment) -> bool:
    ctype = (getattr(attachment, "content_type", None) or "").split(";")[0].strip().lower()
    if ctype.startswith(("audio/", "video/")):
        return True
    return _suffix(getattr(attachment, "filename", None) or "") != ""


def youtube_url_in(text: str) -> "tuple[str | None, str]":
    """`(url, rest)`: the first YouTube link in `text` (None if there is
    none) and the text with it taken out."""
    m = YOUTUBE_RE.search(text)
    if m is None:
        return None, text
    rest = " ".join((text[:m.start()] + " " + text[m.end():]).split())
    return m.group(1), rest


def _suffix(filename: str) -> str:
    """The upload's extension if it's a known audio one, else empty — the
    filename is user-supplied, so nothing else from it reaches the disk."""
    ext = os.path.splitext(filename.lower())[1]
    return ext if ext in AUDIO_EXTENSIONS else ""


def _ytdlp_env() -> "dict | None":
    """The standalone yt-dlp unpacks itself into TMPDIR and execs from
    there, which the container's noexec /tmp refuses; YTDLP_TMPDIR is the
    exec-allowed tmpfs the compose file mounts for it. None: inherit."""
    if not YTDLP_TMPDIR:
        return None
    return {**os.environ, "TMPDIR": YTDLP_TMPDIR}


async def _run(args: list, *, timeout: float, what: str, env: "dict | None" = None) -> "tuple[bytes, str]":
    """Run a tool; `(stdout, stderr text)`. DecodeError if it's missing,
    times out or exits non-zero (with its last stderr line)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
    except FileNotFoundError:
        raise DecodeError(f"{what} isn't installed") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DecodeError(f"{what} took too long") from None
    err_text = err.decode(errors="replace")
    if proc.returncode != 0:
        lines = err_text.strip().splitlines()
        raise DecodeError(lines[-1] if lines else f"{what} failed")
    return out, err_text


async def _ffmpeg_pcm(input_args: list, *, max_secs: float, timeout: float, scrub: str = "") -> bytes:
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", *input_args, "-t", f"{max_secs:g}", *_OUTPUT_ARGS]
    try:
        pcm, _ = await _run(args, timeout=timeout, what="ffmpeg")
    except DecodeError as e:
        raise DecodeError(str(e).replace(scrub, "the file") if scrub else str(e)) from None
    if not pcm:
        raise DecodeError("ffmpeg produced no audio")
    return pcm


async def decode_audio(data: bytes, *, max_secs: float, filename: str = "") -> bytes:
    """PCM for Discord from the bytes of an audio file, cut at `max_secs`
    (ffmpeg's `-t`, so a long upload isn't decoded whole). Raises
    DecodeError when ffmpeg is missing, fails, times out or yields nothing."""
    fd, path = tempfile.mkstemp(prefix="announce-", suffix=_suffix(filename) or ".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return await _ffmpeg_pcm(["-i", path], max_secs=max_secs, timeout=DECODE_TIMEOUT, scrub=path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def parse_ytdlp_output(out: str) -> "tuple[str, bool, str]":
    """`(title, is_live, media url)` from the four `--print` lines
    `resolve_youtube` asks for."""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) < 4 or not lines[-1].startswith(("http://", "https://")):
        raise DecodeError("yt-dlp gave no media URL")
    title, _duration, live, url = lines[-4:]
    return title, live.lower() == "true", url


async def resolve_youtube(url: str) -> "tuple[str, str]":
    """`(title, direct media url)` for a YouTube link, best audio-only
    format (falling back to the best combined one). Live streams are
    refused: there is no end to cut at."""
    args = [
        YTDLP, "--no-config", "--no-playlist", "--no-cache-dir", "--no-warnings", "-q",
        "-f", "ba/b",
        "--print", "%(title)s", "--print", "%(duration)s", "--print", "%(is_live)s", "--print", "%(url)s",
        "--", url,
    ]
    async with _ytdlp_lock:
        out, _ = await _run(args, timeout=RESOLVE_TIMEOUT, what="yt-dlp", env=_ytdlp_env())
    title, live, media = parse_ytdlp_output(out.decode(errors="replace"))
    if live:
        raise DecodeError("that's a live stream")
    return title, media


async def update_ytdlp() -> str:
    """Run `yt-dlp -U` (it fetches and verifies the latest release from
    GitHub, replacing its own binary) and return its last line, e.g.
    "yt-dlp is up to date (2026.10.26)". DecodeError when it can't — a
    read-only install, no network — with the seed left as it was."""
    async with _ytdlp_lock:
        out, err = await _run([YTDLP, "--no-config", "-U"], timeout=UPDATE_TIMEOUT, what="yt-dlp -U", env=_ytdlp_env())
    lines = (out.decode(errors="replace") + "\n" + err).strip().splitlines()
    return lines[-1].strip() if lines else "yt-dlp -U said nothing"


async def fetch_youtube(url: str, *, max_secs: float) -> "tuple[bytes, str]":
    """`(pcm, title)` for a YouTube link, cut at `max_secs`."""
    title, media = await resolve_youtube(url)
    # Reconnects: the CDN closes idle connections mid-clip now and then.
    pcm = await _ffmpeg_pcm(
        ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-i", media],
        max_secs=max_secs, timeout=STREAM_TIMEOUT, scrub=media,
    )
    return pcm, title
