"""Render one narration track and its review video from the same timing plan."""

import asyncio
from pathlib import Path


async def ffmpeg(renderer, *args):
    process = await asyncio.create_subprocess_exec(
        renderer.ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), 180)
    except (asyncio.CancelledError, TimeoutError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError("旁白预览处理失败：" + stderr.decode(errors="replace")[-800:])


async def render_track(renderer, source, destination, blocks):
    filters, labels = [], []
    # Split once so each branch has an explicit source. Output WAV avoids encoder padding
    # changing the measured duration of a tightly fitted narration.
    if len(blocks) > 1:
        filters.append(
            f"[0:a]asplit={len(blocks)}" + "".join(f"[in{i}]" for i in range(len(blocks)))
        )
    for i, block in enumerate(blocks):
        label = f"in{i}" if len(blocks) > 1 else "0:a"
        filters.append(
            f"[{label}]atrim=start={block['source_start']:.9f}:end={block['source_end']:.9f},"
            f"asetpts=PTS-STARTPTS,atempo={block['rate']:.9f},"
            f"apad,atrim=duration={block['end'] - block['start']:.9f},"
            f"adelay={round(block['start'] * 1000)}:all=1[a{i}]"
        )
        labels.append(f"[a{i}]")
    filters.append(
        "".join(labels) + f"amix=inputs={len(blocks)}:normalize=0:dropout_transition=0[out]"
    )
    await ffmpeg(
        renderer,
        "-i",
        source,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        destination,
    )


async def render_review(renderer, video, audio, destination, duration):
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    await ffmpeg(
        renderer,
        "-i",
        video,
        "-i",
        audio,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-af",
        "apad",
        "-t",
        f"{duration:.9f}",
        "-movflags",
        "+faststart",
        destination,
    )
