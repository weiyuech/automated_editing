"""Bounded FFmpeg encoding with runtime hardware detection and observable progress."""
from __future__ import annotations

import asyncio
import math
import logging
import platform
import weakref
from pathlib import Path
from typing import Callable

LOGGER = logging.getLogger(__name__)

Progress = Callable[[dict], None]
ENCODE_CONCURRENCY = 2
STALL_TIMEOUT = 120.0
_PROBES: dict[tuple, tuple[bool, str]] = {}
_ACTIVE_OUTPUTS: set[str] = set()
_LIMITERS = weakref.WeakKeyDictionary()
_PROBE_LOCKS = weakref.WeakKeyDictionary()


class EncodingError(RuntimeError):
    pass


class EncodingStalled(EncodingError):
    pass


def _emit(callback, **state):
    if callback is not None:
        callback(state)


def _binary_key(binary):
    try:
        stat = Path(binary).stat()
        identity = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        identity = None
    return (str(binary), identity, platform.system())


def _limiter():
    loop = asyncio.get_running_loop()
    if loop not in _LIMITERS:
        _LIMITERS[loop] = asyncio.Semaphore(ENCODE_CONCURRENCY)
    return _LIMITERS[loop]


async def _stop(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 3)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def run_process(args, *, duration=0.0, progress=None, encoder="libx264",
                      fallback_reason="", stall_timeout=STALL_TIMEOUT):
    """Drain both pipes continuously; repeated progress does not reset the watchdog."""
    process = await asyncio.create_subprocess_exec(
        args[0], "-nostdin", "-nostats", "-progress", "pipe:1", *args[1:],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    loop = asyncio.get_running_loop()
    advanced_at = loop.time()
    processed = 0.0
    stderr = bytearray()

    def report():
        _emit(progress, stage="encoding", encoder=encoder, processed_seconds=processed,
              fraction=min(1.0, processed / duration) if duration > 0 else None,
              fallback_reason=fallback_reason)

    async def read_progress():
        nonlocal processed, advanced_at
        while line := await process.stdout.readline():
            key, _, value = line.decode("utf-8", "replace").strip().partition("=")
            if key in {"out_time_us", "out_time_ms"}:
                try:
                    seconds = float(value) / 1_000_000
                except ValueError:
                    continue
                if math.isfinite(seconds) and seconds > processed:
                    processed = seconds
                    advanced_at = loop.time()
                    report()

    async def read_errors():
        while chunk := await process.stderr.read(4096):
            stderr.extend(chunk)
            del stderr[:-16000]

    readers = [asyncio.create_task(read_progress()), asyncio.create_task(read_errors())]
    waiter = asyncio.create_task(process.wait())
    try:
        report()
        while not waiter.done():
            remaining = stall_timeout - (loop.time() - advanced_at)
            if remaining <= 0:
                raise EncodingStalled(f"FFmpeg 超过 {stall_timeout:g} 秒没有编码进展，已停止")
            await asyncio.wait([waiter, *readers], timeout=min(remaining, 0.5),
                               return_when=asyncio.FIRST_EXCEPTION)
            for reader in readers:
                if reader.done() and not reader.cancelled() and reader.exception():
                    raise reader.exception()
        await asyncio.gather(*readers)
        if process.returncode != 0:
            raise EncodingError(stderr.decode("utf-8", "replace")[-8000:]
                                or f"FFmpeg exited with code {process.returncode}")
        if duration > 0:
            processed = max(processed, duration)
        report()
    finally:
        await _stop(process)
        for task in [*readers, waiter]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, waiter, return_exceptions=True)


async def nvenc_available(binary):
    key = _binary_key(binary)
    if key in _PROBES:
        return _PROBES[key]
    if platform.system() not in {"Windows", "Linux"}:
        _PROBES[key] = (False, "当前平台使用 CPU 编码")
        return _PROBES[key]
    loop = asyncio.get_running_loop()
    locks = _PROBE_LOCKS.setdefault(loop, {})
    lock = locks.setdefault(key, asyncio.Lock())
    async with lock:
        if key not in _PROBES:
            try:
                await run_process([
                    binary, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=128x128:r=30", "-frames:v", "2", "-an",
                    "-c:v", "h264_nvenc", "-preset", "p4", "-pix_fmt", "yuv420p",
                    "-f", "null", "-",
                ], encoder="h264_nvenc", stall_timeout=10)
                _PROBES[key] = (True, "")
                LOGGER.info("FFmpeg NVENC runtime probe available binary=%s", binary)
            except (EncodingError, OSError) as exc:
                _PROBES[key] = (False, f"NVENC 实测不可用，使用 CPU：{str(exc)[-500:]}")
                LOGGER.info("FFmpeg NVENC runtime probe unavailable binary=%s reason=%s", binary, str(exc)[-500:])
    return _PROBES[key]


def nvenc_args(args):
    converted = []
    index = 0
    while index < len(args):
        key = args[index]
        if key in {"-c:v", "-vcodec", "-preset", "-crf"} and index + 1 < len(args):
            value = args[index + 1]
            if key in {"-c:v", "-vcodec"} and value == "libx264":
                converted += [key, "h264_nvenc"]
            elif key == "-preset":
                converted += [key, "p4"]
            elif key == "-crf":
                converted += ["-rc", "vbr", "-cq", value, "-b:v", "0"]
            else:
                converted += [key, value]
            index += 2
        else:
            converted.append(key)
            index += 1
    return converted


def _hardware_failure(error):
    message = str(error).lower()
    explicit = any(marker in message for marker in (
        "cannot load libcuda", "cannot load nvcuda", "cannot load libnvidia-encode",
        "cannot load nvencodeapi",
        "no nvenc capable devices", "no capable devices found", "driver does not support",
        "minimum required nvidia driver", "failed to open nvenc", "openencodesessionex failed",
        "initializeencoder failed", "nvenc unavailable", "cuda_error",
    ))
    if explicit:
        return True
    # Some failures use generic words. Require the NVENC encoder context on that line;
    # an unrelated demuxer/filter saying "out of memory" must not trigger a retry.
    return any("nvenc" in line and any(marker in line for marker in (
        "out of memory", "no device", "unsupported pixel format", "not supported",
        "initialization failed", "failed to initialize", "device unavailable",
        "unsupported device", "unsupported resolution", "width exceeds", "height exceeds",
    )) for line in message.splitlines())


def _remove_partial(args):
    target = args[-1]
    if target != "-" and not target.startswith("pipe:"):
        Path(target).unlink(missing_ok=True)


async def encode(args, *, duration=0.0, progress=None, stall_timeout=STALL_TIMEOUT):
    target = args[-1]
    key = str(Path(target).resolve()) if target != "-" and not target.startswith("pipe:") else None
    if key is not None:
        if key in _ACTIVE_OUTPUTS:
            raise EncodingError(f"编码目标正在生成：{Path(target).name}")
        _ACTIVE_OUTPUTS.add(key)
    try:
        await _encode_owned(args, duration=duration, progress=progress, stall_timeout=stall_timeout)
    finally:
        if key is not None:
            _ACTIVE_OUTPUTS.discard(key)


async def _encode_owned(args, *, duration, progress, stall_timeout):
    target = args[-1]
    if target != "-" and not target.startswith("pipe:") and Path(target).exists():
        raise EncodingError(f"编码目标文件已存在：{Path(target).name}")
    _emit(progress, stage="waiting", encoder=None, processed_seconds=0.0,
          fraction=0.0, fallback_reason="")
    # Waiters are cancellable, and all RenderService instances share this capacity.
    async with _limiter():
        # A job may have waited behind other work; reject files that appeared meanwhile.
        if target != "-" and not target.startswith("pipe:") and Path(target).exists():
            raise EncodingError(f"编码目标文件已存在：{Path(target).name}")
        started = asyncio.get_running_loop().time()
        available, reason = (False, "")
        if "libx264" in args:
            available, reason = await nvenc_available(args[0])
        LOGGER.info("FFmpeg encode started encoder=%s output=%s duration_seconds=%.3f",
                    "h264_nvenc" if available else "libx264", Path(target).name, duration)
        try:
            await run_process(nvenc_args(args) if available else args, duration=duration,
                              progress=progress, encoder="h264_nvenc" if available else "libx264",
                              fallback_reason=reason, stall_timeout=stall_timeout)
        except EncodingStalled:
            LOGGER.error("FFmpeg encode stalled output=%s timeout_seconds=%s", Path(target).name, stall_timeout)
            _remove_partial(args)
            raise
        except EncodingError as exc:
            _remove_partial(args)
            if not available or not _hardware_failure(exc):
                raise
            reason = f"NVENC 编码失败，改用 CPU：{str(exc)[-500:]}"
            LOGGER.warning("FFmpeg CPU fallback output=%s reason=%s", Path(target).name, str(exc)[-500:])
            _PROBES[_binary_key(args[0])] = (False, reason)
            try:
                await run_process(args, duration=duration, progress=progress, encoder="libx264",
                                  fallback_reason=reason, stall_timeout=stall_timeout)
            except BaseException:
                _remove_partial(args)
                raise
        except asyncio.CancelledError:
            LOGGER.info("FFmpeg encode cancelled output=%s", Path(target).name)
            _remove_partial(args)
            raise
        except BaseException:
            _remove_partial(args)
            raise
        LOGGER.info("FFmpeg encode completed encoder=%s output=%s elapsed_seconds=%.3f",
                    "h264_nvenc" if available and not reason else "libx264", Path(target).name,
                    asyncio.get_running_loop().time() - started)
