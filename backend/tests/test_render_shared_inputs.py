"""A recording is decoded once even when disjoint selected ranges are concatenated."""
import array
import subprocess

import pytest

from automated_video_editing_backend.core.models import EditTimeline, TimelineClip
from automated_video_editing_backend.services.render import RenderService


def clip(path, start, at, duration=.4, kind='video', **kwargs):
    return TimelineClip(media_id='recording', source_path=str(path), kind=kind,
                        start=start, duration=duration, timeline_start=at, **kwargs)


@pytest.mark.asyncio
async def test_disjoint_ranges_share_input_with_correct_picture_and_audio(tmp_path):
    renderer = RenderService()
    binary = renderer.ffmpeg_binary()
    source = tmp_path / 'source.mp4'
    output = tmp_path / 'result.mp4'
    args = [binary, '-v', 'error', '-y']
    for color in ('red', 'green', 'blue'):
        args += ['-f', 'lavfi', '-i', f'color=c={color}:s=64x64:r=30:d=1']
    for frequency in (400, 800, 1200):
        args += ['-f', 'lavfi', '-i', f'sine=frequency={frequency}:sample_rate=48000:duration=1']
    args += ['-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a][4:a][5:a]concat=n=3:v=0:a=1[a]',
             '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-output_ts_offset', '5', str(source)]
    subprocess.run(args, check=True, capture_output=True, timeout=15)
    start = subprocess.run([renderer.ffprobe_binary(), '-v', 'error', '-show_entries',
                            'format=start_time', '-of', 'default=noprint_wrappers=1:nokey=1', str(source)],
                           check=True, capture_output=True, text=True, timeout=10)
    assert float(start.stdout.strip()) > 4.9
    timeline = EditTimeline(title='selected ranges', output_path=str(output),
                            output_width=64, output_height=64, include_original_audio=True,
                            clips=[clip(source, 0, 0), clip(source, 2, .4)])
    encoding_args = renderer.build_ffmpeg_args(timeline)
    assert encoding_args.count(str(source)) == 1
    graph = encoding_args[encoding_args.index('-filter_complex') + 1]
    assert '[0:v]setpts=PTS-STARTPTS,trim=start=2.000' in graph
    assert '[0:a]aresample=async=1:first_pts=0,atrim=start=2.000' in graph
    await renderer.render(timeline)
    assert renderer.probe_duration(str(output)) == pytest.approx(.8, abs=.07)
    for at, channel, frequency in ((.15, 0, 400), (.55, 2, 1200)):
        picture = subprocess.run([binary, '-v', 'error', '-ss', str(at), '-i', str(output),
                                  '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                                 check=True, capture_output=True, timeout=10).stdout
        pixel = picture[:3]
        assert pixel[channel] > 200 and sum(pixel) - pixel[channel] < 60
        audio = subprocess.run([binary, '-v', 'error', '-ss', str(at), '-i', str(output),
                                '-t', '0.1', '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', '-'],
                               check=True, capture_output=True, timeout=10).stdout
        samples = array.array('h', audio)
        # Count positive crossings in the extracted 100ms window. This verifies that the
        # later video range uses the corresponding later sound, not the removed middle.
        crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
        assert crossings / .1 == pytest.approx(frequency, rel=.1)


def test_still_loops_stay_separate_and_extra_audio_indices_follow_deduplication():
    renderer = RenderService()
    timeline = EditTimeline(title='inputs', output_path='/out.mp4',
        clips=[clip('/a.mp4', 0, 0, include_audio=True),
               clip('/a.mp4', 2, .4, include_audio=True),
               clip('/still.png', 0, .8, kind='image'),
               clip('/still.png', 0, 1.2, duration=.8, kind='image')],
        music_path='/music.mp3', voiceover_path='/voice.mp3')
    args = renderer.build_ffmpeg_args(timeline)
    assert args.count('/a.mp4') == 1
    assert args.count('/still.png') == 2
    graph = args[args.index('-filter_complex') + 1]
    assert '[0:a:0]aresample=async=1:first_pts=0,atrim=start=2.000' in graph
    assert '[3:a:0]volume=' in graph
    assert '[4:a:0]volume=1.0' in graph
