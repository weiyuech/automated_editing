"""Encode lifecycle tests use local subprocesses, no GPU or paid service required."""
import asyncio
import os
from pathlib import Path
import sys

import pytest

from automated_video_editing_backend.services import encoding
from automated_video_editing_backend.services.render import RenderService


@pytest.fixture(autouse=True)
def isolated_cache():
    encoding._PROBES.clear()
    encoding._LIMITERS.clear()
    encoding._PROBE_LOCKS.clear()


@pytest.fixture
def executable(tmp_path, monkeypatch):
    script = tmp_path / 'fake_ffmpeg'
    script.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
args = sys.argv
mode = next((v for v in args if v.startswith("mode=")), "mode=progress")
Path(args[-1] + ".pid").write_text(str(os.getpid()))
Path(args[-1]).write_bytes(b"partial")
if mode == "mode=stall":
    while True:
        print("out_time_us=0", flush=True)
        time.sleep(.02)
if mode == "mode=wait":
    time.sleep(60)
sys.stderr.write("x" * 100000)
sys.stderr.flush()
print("out_time_us=100000", flush=True)
time.sleep(.03)
print("out_time_us=500000", flush=True)
print("progress=end", flush=True)
''')
    spawned = []
    real_spawn = asyncio.create_subprocess_exec
    async def spawn(binary, *args, **kwargs):
        process = await real_spawn(sys.executable, binary, *args, **kwargs)
        spawned.append(process)
        return process
    monkeypatch.setattr(encoding.asyncio, "create_subprocess_exec", spawn)
    yield str(script)
    assert all(process.returncode is not None for process in spawned)


@pytest.mark.asyncio
async def test_real_process_reports_clock_and_drains_stderr(executable, tmp_path):
    updates = []
    await encoding.run_process([executable, str(tmp_path / 'out.mp4')],
                               duration=.5, progress=updates.append, stall_timeout=2)
    assert any(u['processed_seconds'] == .1 for u in updates)
    assert updates[-1]['fraction'] == 1
    assert updates[-1]['encoder'] == 'libx264'


@pytest.mark.asyncio
async def test_repeated_progress_without_advancement_stalls_and_reaps(executable, tmp_path):
    output = tmp_path / 'stall.mp4'
    with pytest.raises(encoding.EncodingStalled):
        await encoding.encode([executable, 'mode=stall', str(output)],
                              duration=1, stall_timeout=1.0)
    assert not output.exists()
    pid = int(Path(str(output) + '.pid').read_text())
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.asyncio
async def test_cancel_terminates_subprocess_and_cleans_partial(executable, tmp_path):
    output = tmp_path / 'cancel.mp4'
    task = asyncio.create_task(encoding.encode([executable, 'mode=wait', str(output)], duration=1))
    for _ in range(100):
        if output.exists():
            break
        await asyncio.sleep(.01)
    assert output.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not output.exists()
    pid = int(Path(str(output) + '.pid').read_text())
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('success', [True, False])
async def test_runtime_probe_is_cached_and_shared(monkeypatch, success):
    calls = []
    monkeypatch.setattr(encoding.platform, 'system', lambda: 'Windows')
    async def run(args, **kwargs):
        calls.append(args)
        assert 'h264_nvenc' in args and 'color=c=black:s=128x128:r=30' in args
        await asyncio.sleep(.01)
        if not success:
            raise encoding.EncodingError('Cannot load nvcuda.dll')
    monkeypatch.setattr(encoding, 'run_process', run)
    results = await asyncio.gather(*(encoding.nvenc_available('ffmpeg') for _ in range(3)))
    assert [r[0] for r in results] == [success] * 3
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_hardware_failure_retries_once_then_caches_cpu(monkeypatch, tmp_path):
    monkeypatch.setattr(encoding.platform, 'system', lambda: 'Linux')
    calls = []
    async def run(args, **kwargs):
        calls.append((args, kwargs))
        if '-f' in args and 'lavfi' in args:
            return  # successful runtime probe
        if 'h264_nvenc' in args:
            Path(args[-1]).write_bytes(b'partial')
            raise encoding.EncodingError('OpenEncodeSessionEx failed: out of memory')
        assert not Path(args[-1]).exists()
    monkeypatch.setattr(encoding, 'run_process', run)
    args = ['ffmpeg', '-y', '-i', 'input.mp4', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', str(tmp_path / 'result.mp4')]
    await encoding.encode(args, duration=10)
    await encoding.encode(args, duration=10)
    assert len(calls) == 4  # one probe, one failed GPU, two CPU jobs
    assert calls[2][0] == args
    assert calls[2][1]['fallback_reason']
    assert '-cq' in calls[1][0] and '-crf' not in calls[1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [encoding.EncodingError('Invalid data found when processing input'), encoding.EncodingStalled('stalled')])
async def test_input_failure_and_stall_do_not_retry(monkeypatch, tmp_path, error):
    calls = []
    async def available(_):
        return True, ''
    async def run(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b'partial')
        raise error
    monkeypatch.setattr(encoding, 'nvenc_available', available)
    monkeypatch.setattr(encoding, 'run_process', run)
    output = tmp_path / 'broken.mp4'
    with pytest.raises(type(error)):
        await encoding.encode(['ffmpeg', '-c:v', 'libx264', str(output)], duration=1)
    assert len(calls) == 1
    assert not output.exists()


@pytest.mark.asyncio
async def test_global_capacity_wait_is_cancellable_across_renderers(monkeypatch):
    monkeypatch.setattr(encoding, 'ENCODE_CONCURRENCY', 1)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    async def run(args, **kwargs):
        calls.append(args)
        started.set()
        await release.wait()
    monkeypatch.setattr(encoding, 'run_process', run)
    first = asyncio.create_task(RenderService().encode(['ffmpeg', '-'], duration=1))
    await started.wait()
    updates = []
    second = asyncio.create_task(RenderService().encode(['ffmpeg', '-'], duration=2, progress=updates.append))
    await asyncio.sleep(.02)
    assert updates == [{'stage': 'waiting', 'encoder': None, 'processed_seconds': 0.0, 'fraction': 0.0, 'fallback_reason': ''}]
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release.set()
    await first
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_existing_output_is_never_started_or_deleted(monkeypatch, tmp_path):
    output = tmp_path / 'keep.mp4'
    output.write_bytes(b'original')
    async def forbidden(*args, **kwargs):
        raise AssertionError('must not start FFmpeg')
    monkeypatch.setattr(encoding, 'run_process', forbidden)
    with pytest.raises(encoding.EncodingError, match='已存在'):
        await encoding.encode(['ffmpeg', str(output)], duration=1)
    assert output.read_bytes() == b'original'


@pytest.mark.parametrize('message,expected', [
    ('[h264_nvenc @ 0x1] out of memory', True),
    ('[h264_nvenc @ 0x1] No device available', True),
    ('[h264_nvenc @ 0x1] unsupported pixel format', True),
    ('[demuxer] out of memory', False),
    ('[scale] unsupported pixel format', False),
])
def test_generic_error_needs_hardware_context(message, expected):
    assert encoding._hardware_failure(message) is expected

@pytest.mark.asyncio
async def test_same_output_cannot_be_encoded_concurrently(monkeypatch, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    async def run(args, **kwargs):
        started.set()
        await release.wait()
    monkeypatch.setattr(encoding, 'run_process', run)
    output = str(tmp_path / 'same.mp4')
    first = asyncio.create_task(encoding.encode(['ffmpeg', output], duration=1))
    await started.wait()
    with pytest.raises(encoding.EncodingError, match='正在生成'):
        await encoding.encode(['ffmpeg', output], duration=1)
    release.set()
    await first
    assert str(Path(output).resolve()) not in encoding._ACTIVE_OUTPUTS
