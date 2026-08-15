from pathlib import Path

import numpy as np
import soundfile as sf

from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.analysis import AnalysisService


def test_librosa_beat_detection_is_available():
    sr = 22050
    audio_path = generated_path("cache", "test-beat-detection.wav")
    y = np.zeros(sr * 8, dtype=np.float32)
    for t in np.arange(0.5, 8.0, 0.5):
        idx = int(t * sr)
        y[idx:idx + 200] = 0.9
    sf.write(audio_path, y, sr)

    warnings = []
    beats = AnalysisService().detect_beats(Path(audio_path), warnings)

    assert warnings == []
    assert len(beats) >= 4


def test_only_strong_onsets_become_dynamic_edit_accents():
    class FakeLibrosa:
        @staticmethod
        def frames_to_time(frame, **_kwargs):
            return float(frame) / 10.0

    service = AnalysisService()
    frames = np.asarray([1, 2, 3, 4, 5])
    envelope = np.asarray([0.0, 0.1, 0.2, 0.9, 0.3, 1.0])

    accents = service._accent_times(frames, envelope, 100, 10, FakeLibrosa())

    assert accents == [0.3, 0.5]


def test_music_sections_are_clustered_on_the_beat_clock():
    calls = {"sync": 0}

    class Feature:
        @staticmethod
        def chroma_cqt(**_kwargs):
            return np.ones((12, 10))

        @staticmethod
        def mfcc(**_kwargs):
            return np.ones((13, 10))

    class Util:
        @staticmethod
        def normalize(values, **_kwargs):
            return values

        @staticmethod
        def fix_frames(_frames, **_kwargs):
            return np.asarray([0, 2, 4, 6, 8, 9])

        @staticmethod
        def sync(values, indexes, **_kwargs):
            calls["sync"] += 1
            return values[:, :len(indexes)]

    class Segment:
        @staticmethod
        def agglomerative(_data, **_kwargs):
            return np.asarray([0, 2, 4])

    class FakeLibrosa:
        feature = Feature()
        util = Util()
        segment = Segment()

        @staticmethod
        def frames_to_time(values, **_kwargs):
            return np.asarray(values, dtype=float) * 0.5

    boundaries = AnalysisService()._music_sections(
        FakeLibrosa(), np.zeros(100), 100, 5.0, [], np.asarray([1, 3, 5]), 10,
    )

    assert calls["sync"] == 1
    assert boundaries == [0.0, 2.0, 4.0, 5.0]
