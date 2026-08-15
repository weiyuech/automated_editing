"""Local, optional alignment between a narration and robot point descriptions.

The embedding model answers one narrow question: whether two pieces of Chinese text describe
the same subject. It never chooses a duration, a cut, a source timestamp, or a point order.
Those remain deterministic planner decisions, which is what lets this layer disappear without
changing the established edit whenever evidence or model assets are unavailable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Protocol

import numpy as np

from automated_video_editing_backend.core.models import MediaItem
from automated_video_editing_backend.services import subtitles
from automated_video_editing_backend.services.capture import read_sidecar, sidecar_path

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "semantic" / "bge-small-zh-v1.5"
MODEL_PATH = ASSET_DIR / "model_int8.onnx"
TOKENIZER_PATH = ASSET_DIR / "tokenizer.json"

# BGE v1.5 deliberately has a wider similarity distribution than its predecessor. On the
# bundled INT8 model, unrelated short Chinese descriptions usually sit around .3-.48 while a
# faithful paraphrase is around .7-.85. A threshold is still an engineering prior, not a claim
# of learned taste; developer diagnostics retain every accepted score.
BGE_MATCH_THRESHOLD = 0.62
LEXICAL_MATCH_THRESHOLD = 0.46
MAX_TOKENS = 256
_HARD_STOPS = "。！？.!?…"
_PAUSE_SECONDS = 0.7


@dataclass(frozen=True)
class PointDescription:
    label: str
    ordinal: int
    description: str


@dataclass(frozen=True)
class NarrationUnit:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class SemanticChapter:
    start: float
    end: float
    text: str
    label: str | None
    score: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class SemanticAlignment:
    chapters: tuple[SemanticChapter, ...] = ()
    evidence: str = "none"
    reason: str = ""
    narration_duration: float = 0.0
    point_descriptions: tuple[PointDescription, ...] = ()

    @property
    def active(self) -> bool:
        return any(chapter.label for chapter in self.chapters)

    @property
    def mandatory_labels(self) -> set[str]:
        return {chapter.label for chapter in self.chapters if chapter.label}

    @property
    def matched_seconds(self) -> float:
        return sum(chapter.duration for chapter in self.chapters if chapter.label)

    def diagnostics(self) -> dict:
        total = self.narration_duration
        return {
            "evidence": self.evidence,
            "active": self.active,
            "reason": self.reason,
            "narration_seconds": round(total, 3),
            "matched_seconds": round(self.matched_seconds, 3),
            "matched_ratio": round(self.matched_seconds / total, 4) if total > 0 else 0.0,
            "points": [
                {
                    "label": item.label,
                    "ordinal": item.ordinal,
                    "description": item.description,
                }
                for item in self.point_descriptions
            ],
            "chapters": [
                {
                    "start": round(item.start, 3),
                    "end": round(item.end, 3),
                    "text": item.text,
                    "label": item.label,
                    "score": round(item.score, 4),
                }
                for item in self.chapters
            ],
        }


class EmbeddingBackend(Protocol):
    @property
    def evidence(self) -> str: ...

    def similarity(self, left: Sequence[str], right: Sequence[str]) -> np.ndarray | None: ...


class BgeOnnxEmbedder:
    """Lazy CPU inference for the bundled 24 MB INT8 BGE model.

    Imports are deliberately lazy. An unpackaged development tree has no model asset, and an
    older installation may not contain ONNX Runtime. Both cases are an ordinary semantic
    fallback, not a backend-startup failure.
    """

    evidence = "bge-small-zh-v1.5-int8"

    def __init__(
        self,
        model_path: Path = MODEL_PATH,
        tokenizer_path: Path = TOKENIZER_PATH,
    ) -> None:
        self.model_path = model_path
        self.tokenizer_path = tokenizer_path
        self._session = None
        self._tokenizer = None
        self._cache: dict[str, np.ndarray] = {}
        self.problem = ""

    def similarity(self, left: Sequence[str], right: Sequence[str]) -> np.ndarray | None:
        if not left or not right or not self._load():
            return None
        vectors = self._encode(list(dict.fromkeys([*left, *right])))
        if vectors is None:
            return None
        return np.asarray([vectors[text] for text in left]) @ np.asarray(
            [vectors[text] for text in right]
        ).T

    def _load(self) -> bool:
        if self._session is not None and self._tokenizer is not None:
            return True
        if self.problem:
            return False
        if not self.model_path.is_file() or not self.tokenizer_path.is_file():
            self.problem = "semantic model assets are missing"
            return False
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(self.tokenizer_path))
            tokenizer.enable_truncation(max_length=MAX_TOKENS)
            tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            self._session = ort.InferenceSession(
                str(self.model_path), providers=["CPUExecutionProvider"]
            )
            self._tokenizer = tokenizer
            return True
        except Exception as exc:  # noqa: BLE001 - optional native runtime may fail many ways
            self.problem = f"semantic model unavailable: {exc.__class__.__name__}"
            return False

    def _encode(self, texts: list[str]) -> dict[str, np.ndarray] | None:
        missing = [text for text in texts if text not in self._cache]
        if missing:
            try:
                encodings = self._tokenizer.encode_batch(missing)
                arrays = {
                    "input_ids": np.asarray([item.ids for item in encodings], dtype=np.int64),
                    "attention_mask": np.asarray(
                        [item.attention_mask for item in encodings], dtype=np.int64
                    ),
                    "token_type_ids": np.asarray(
                        [item.type_ids for item in encodings], dtype=np.int64
                    ),
                }
                wanted = {item.name for item in self._session.get_inputs()}
                output = self._session.run(
                    None, {name: value for name, value in arrays.items() if name in wanted}
                )[0][:, 0, :]
                norm = np.linalg.norm(output, axis=1, keepdims=True)
                output = output / np.maximum(norm, 1e-12)
                self._cache.update({text: vector for text, vector in zip(missing, output)})
            except Exception as exc:  # noqa: BLE001 - inference must degrade, not stop editing
                self.problem = f"semantic inference failed: {exc.__class__.__name__}"
                return None
        return {text: self._cache[text] for text in texts}


class SemanticService:
    def __init__(self, embedder: EmbeddingBackend | None = None) -> None:
        self.embedder = embedder or BgeOnnxEmbedder()
        self._cache: dict[tuple, SemanticAlignment] = {}

    def align(
        self,
        source: MediaItem,
        voiceover: MediaItem | None,
        voiceover_duration: float | None,
    ) -> SemanticAlignment:
        """Build a monotonic narration-to-point schedule, or an inert explanation."""
        if voiceover is None or not voiceover_duration or voiceover_duration <= 0:
            return SemanticAlignment(reason="no voiceover")
        key = self._cache_key(source, voiceover, voiceover_duration)
        if key in self._cache:
            return self._cache[key]

        labels, notes = self._source_evidence(source.path)
        points = parse_point_descriptions(notes, labels)
        if not points:
            result = SemanticAlignment(
                reason="capture notes contain no point descriptions",
                narration_duration=voiceover_duration,
            )
            self._cache[key] = result
            return result

        payload = self._voiceover_payload(voiceover)
        units = narration_units(payload, voiceover_duration)
        if not units:
            result = SemanticAlignment(
                reason="voiceover has no readable text or timing",
                narration_duration=voiceover_duration,
                point_descriptions=tuple(points),
            )
            self._cache[key] = result
            return result

        chapters, evidence = self._match(units, points)
        result = SemanticAlignment(
            chapters=tuple(_coalesce(chapters)),
            evidence=evidence if any(item.label for item in chapters) else "none",
            reason="" if any(item.label for item in chapters) else "no confident monotonic match",
            narration_duration=voiceover_duration,
            point_descriptions=tuple(points),
        )
        self._cache[key] = result
        return result

    def compatibility(self, source: MediaItem, voiceover: MediaItem | None) -> float | None:
        """Cheap source/voiceover affinity for future pool dealing.

        It deliberately returns ``None`` without both kinds of text. Unknown means generic,
        not incompatible, so existing pool behaviour can remain unchanged.
        """
        if voiceover is None:
            return None
        labels, notes = self._source_evidence(source.path)
        points = parse_point_descriptions(notes, labels)
        payload = self._voiceover_payload(voiceover)
        text = str(payload.get("text") or "").strip()
        if not points or not text:
            return None
        matrix = self.embedder.similarity([text], [item.description for item in points])
        if matrix is None:
            return max((_lexical_similarity(text, item.description) for item in points), default=0.0)
        return float(np.max(matrix))

    def source_description_count(self, source: MediaItem) -> int:
        """Return only explicit point descriptions; never infer meaning from bare notes."""
        labels, notes = self._source_evidence(source.path)
        return len(parse_point_descriptions(notes, labels))

    def _match(
        self, units: list[NarrationUnit], points: list[PointDescription]
    ) -> tuple[list[SemanticChapter], str]:
        left = [unit.text for unit in units]
        right = [point.description for point in points]
        embedded = self.embedder.similarity(left, right)
        lexical = np.asarray([
            [_lexical_similarity(a, b) for b in right]
            for a in left
        ])
        if embedded is None:
            scores = lexical
            threshold = LEXICAL_MATCH_THRESHOLD
            evidence = "lexical"
        else:
            scores = 0.9 * embedded + 0.1 * lexical
            threshold = BGE_MATCH_THRESHOLD
            evidence = self.embedder.evidence
        assignment = _monotonic_assignment(scores, threshold)
        return [
            SemanticChapter(
                unit.start,
                unit.end,
                unit.text,
                points[index].label if index is not None else None,
                float(scores[row, index]) if index is not None else 0.0,
            )
            for row, (unit, index) in enumerate(zip(units, assignment))
        ], evidence

    def _source_evidence(self, path: str) -> tuple[list[str], list[str]]:
        payload = read_sidecar(path) or {}
        labels: list[str] = []
        for segment in payload.get("segments") or []:
            if not isinstance(segment, dict) or segment.get("status") != "arrived":
                continue
            path_name = str(segment.get("path_name") or "").strip()
            goal_id = segment.get("goal_id")
            label = f"{path_name}#{goal_id}" if path_name and goal_id is not None else path_name
            if label and label not in labels:
                labels.append(label)
        if not labels:
            for marker in payload.get("markers") or []:
                if not isinstance(marker, dict):
                    continue
                label = str(marker.get("label") or "").strip()
                if label and "失败" not in label and label not in labels:
                    labels.append(label)
        notes = [str(note).strip() for note in payload.get("notes") or [] if str(note).strip()]
        return labels, notes

    def _voiceover_payload(self, voiceover: MediaItem) -> dict:
        metadata = voiceover.metadata or {}
        path = metadata.get("metadata_path") or str(Path(voiceover.path).with_suffix(".json"))
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _cache_key(self, source: MediaItem, voiceover: MediaItem, duration: float) -> tuple:
        return (
            source.id,
            _mtime(sidecar_path(source.path)),
            voiceover.id,
            _mtime(Path((voiceover.metadata or {}).get("metadata_path") or Path(voiceover.path).with_suffix(".json"))),
            round(duration, 3),
        )


def parse_point_descriptions(notes: Sequence[str], labels: Sequence[str]) -> list[PointDescription]:
    """Read a compact recording-level map without demanding one field per robot point.

    Accepted examples include ``点位1：展示区``, ``第一点：展示区`` and the robot label
    ``path1#1：展示区``. Bare prose is deliberately not guessed onto a point.
    """
    text = "\n".join(str(note).strip() for note in notes if str(note).strip())
    if not text or not labels:
        return []
    label_alternatives = "|".join(sorted((re.escape(label) for label in labels), key=len, reverse=True))
    number = r"[0-9零〇一二三四五六七八九十百两]+"
    pattern = re.compile(
        rf"(?P<marker>(?:点位|点)\s*(?P<n1>{number})|第\s*(?P<n2>{number})\s*(?:个)?点(?:位)?"
        + (rf"|(?P<label>{label_alternatives})" if label_alternatives else "")
        + r")\s*(?:是|为)?\s*[：:=\-]\s*",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    descriptions: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        description = text[match.end():end].strip(" \t\r\n；;，,")
        if not description:
            continue
        explicit = match.groupdict().get("label")
        if explicit:
            label = explicit
        else:
            ordinal = _chinese_integer(match.group("n1") or match.group("n2") or "")
            if ordinal is None or ordinal < 1 or ordinal > len(labels):
                continue
            label = labels[ordinal - 1]
        descriptions.setdefault(label, []).append(description)
    return [
        PointDescription(label, labels.index(label) + 1, "；".join(descriptions[label]))
        for label in labels if label in descriptions
    ]


def narration_units(payload: dict, duration: float) -> list[NarrationUnit]:
    """Sentence-like units covering the complete narration clock without gaps."""
    words = subtitles.normalise_words(payload.get("words") or [])
    raw: list[tuple[str, float, float]] = []
    if words:
        start = words[0][1]
        text: list[str] = []
        for index, (word, word_start, word_end) in enumerate(words):
            text.append(word)
            next_start = words[index + 1][1] if index + 1 < len(words) else None
            paused = next_start is not None and next_start - word_end >= _PAUSE_SECONDS
            if any(char in _HARD_STOPS for char in word) or paused or next_start is None:
                raw.append((_join_narration_text(text), start, word_end))
                text = []
                if next_start is not None:
                    start = next_start
    else:
        text = str(payload.get("text") or "").strip()
        stops = re.escape(_HARD_STOPS)
        sentences = [
            item.strip()
            for item in re.findall(rf"[^{stops}]+[{stops}]?", text)
            if item.strip()
        ]
        if sentences and duration > 0:
            weights = [max(1, len(item.strip(_HARD_STOPS))) for item in sentences]
            cursor = 0.0
            for index, (sentence, weight) in enumerate(zip(sentences, weights)):
                end = duration if index + 1 == len(sentences) else cursor + duration * weight / sum(weights)
                raw.append((sentence, cursor, end))
                cursor = end
    raw = [item for item in raw if item[0] and item[2] >= item[1]]
    if not raw:
        return []

    boundaries = [0.0]
    for first, second in pairwise(raw):
        boundaries.append(max(boundaries[-1], min(duration, (first[2] + second[1]) / 2.0)))
    boundaries.append(duration)
    units = [
        NarrationUnit(boundaries[index], boundaries[index + 1], item[0])
        for index, item in enumerate(raw)
        if boundaries[index + 1] - boundaries[index] > 1e-6
    ]
    return _merge_tiny_units(units)


def _monotonic_assignment(scores: np.ndarray, threshold: float) -> list[int | None]:
    """Viterbi alignment: narration may skip points, but may never walk the source backwards."""
    if scores.ndim != 2 or not scores.size:
        return [None] * (scores.shape[0] if scores.ndim else 0)
    rows, columns = scores.shape
    # State is the most recent matched point, plus -1 for no match yet.
    states: dict[int, tuple[float, list[int | None]]] = {-1: (0.0, [])}
    for row in range(rows):
        next_states: dict[int, tuple[float, list[int | None]]] = {}
        for last, (value, path) in states.items():
            _keep_best(next_states, last, value, [*path, None])
            for point in range(max(0, last), columns):
                score = float(scores[row, point])
                if score < threshold:
                    continue
                # Threshold-centred utility means a weak accepted match cannot crowd out a
                # later, much stronger one merely by being first.
                _keep_best(next_states, point, value + score - threshold, [*path, point])
        states = next_states
    return max(states.values(), key=lambda item: item[0])[1]


def _keep_best(states: dict, key: int, value: float, path: list[int | None]) -> None:
    current = states.get(key)
    if current is None or value > current[0] + 1e-9:
        states[key] = (value, path)


def _coalesce(chapters: Sequence[SemanticChapter]) -> list[SemanticChapter]:
    out: list[SemanticChapter] = []
    for item in chapters:
        if out and out[-1].label == item.label:
            previous = out[-1]
            weight = previous.duration + item.duration
            score = (
                (previous.score * previous.duration + item.score * item.duration) / weight
                if weight > 0 else 0.0
            )
            out[-1] = SemanticChapter(
                previous.start,
                item.end,
                _join_narration_text([previous.text, item.text]),
                item.label,
                score,
            )
        else:
            out.append(item)
    return out


def _merge_tiny_units(units: list[NarrationUnit], minimum: float = 0.6) -> list[NarrationUnit]:
    out: list[NarrationUnit] = []
    for item in units:
        if item.duration >= minimum or not out:
            out.append(item)
            continue
        previous = out[-1]
        out[-1] = NarrationUnit(
            previous.start,
            item.end,
            _join_narration_text([previous.text, item.text]),
        )
    if len(out) > 1 and out[0].duration < minimum:
        first, second = out[0], out[1]
        out[:2] = [NarrationUnit(
            first.start,
            second.end,
            _join_narration_text([first.text, second.text]),
        )]
    return out


def _join_narration_text(parts: Sequence[str]) -> str:
    """Keep provider-supplied spacing and restore a missing space between Latin words."""
    result = ""
    for value in parts:
        part = str(value)
        if not part:
            continue
        if (
            result
            and not result[-1].isspace()
            and not part[0].isspace()
            and result[-1].isascii()
            and result[-1].isalnum()
            and part[0].isascii()
            and part[0].isalnum()
        ):
            result += " "
        result += part
    return result.strip()


def _lexical_similarity(first: str, second: str) -> float:
    def grams(text: str) -> set[str]:
        clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text.lower())
        if len(clean) < 2:
            return {clean} if clean else set()
        return {clean[index:index + 2] for index in range(len(clean) - 1)}

    left, right = grams(first), grams(second)
    if not left or not right:
        return 0.0
    jaccard = len(left & right) / len(left | right)
    clean_first = re.sub(r"\s+", "", first.lower())
    clean_second = re.sub(r"\s+", "", second.lower())
    containment = 1.0 if clean_first in clean_second or clean_second in clean_first else 0.0
    return min(1.0, 0.75 * jaccard + 0.25 * containment)


def _chinese_integer(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value in digits:
        return digits[value]
    if "百" in value:
        left, right = value.split("百", 1)
        return digits.get(left or "一", 1) * 100 + (_chinese_integer(right) or 0)
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left or "一", 1) * 10 + digits.get(right, 0)
    return None


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0
