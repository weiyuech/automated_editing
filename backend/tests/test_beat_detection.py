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
