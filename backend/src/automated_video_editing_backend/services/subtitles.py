from __future__ import annotations

import base64
import itertools
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

# Where the bundled fonts live. `scripts/prepare_assets.py` puts them here and checks that each
# one calls itself the family name below — libass matches on the family name a font declares
# internally, not on its filename, and a name that matches nothing is not an error to libass. It
# substitutes a system font and renders happily, so the export succeeds in the wrong typeface.
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
# Tiny WOFF2 subsets, built by scripts/prepare_assets.py, so the picker can show each font in
# its own typeface. Kept out of FONT_DIR because libass reads every file in there and cannot
# parse WOFF2. The full faces are 10–27 MB; these are under 10 KB each.
PREVIEW_DIR = Path(__file__).resolve().parent.parent / "assets" / "font_previews"

# Cue times are held relative to the start of the voiceover, never to the start of the timeline.
# The offset is applied once, when the .ass is written, from the same field the renderer uses to
# delay the audio. That is the whole reason picture and narration cannot drift apart: there is no
# second copy of the timing to fall out of date when someone drags the voiceover in 手动微调.


@dataclass(frozen=True)
class BundledFont:
    key: str
    label: str
    filename: str
    # The family name the ASS style asks for.
    family: str
    # Whether to set Bold=1 in the style. True only for faces that carry a real bold, because
    # libass synthesises a fake bold when it is asked for one the font does not have, and
    # emboldening an already-heavy face thickens it into mush.
    bold: bool
    note: str


BUNDLED_FONTS: dict[str, BundledFont] = {
    "noto_sans_sc": BundledFont(
        key="noto_sans_sc",
        label="思源黑体",
        filename="NotoSansSC.ttf",
        family="Noto Sans SC",
        bold=True,
        note="通用黑体，中英文都稳妥",
    ),
    "smiley_sans": BundledFont(
        key="smiley_sans",
        label="得意黑",
        filename="SmileySans.ttf",
        family="Smiley Sans Oblique",
        # Already a heavy oblique face; asking for bold on top would synthesise one.
        bold=False,
        note="紧凑倾斜，短视频常用",
    ),
    "noto_serif_sc": BundledFont(
        key="noto_serif_sc",
        label="思源宋体",
        filename="NotoSerifSC.ttf",
        family="Noto Serif SC",
        bold=True,
        note="宋体，偏纪录片气质",
    ),
}
DEFAULT_FONT = "noto_sans_sc"

# Named sizes, as a fraction of the frame's short side. See `SubtitleStyle.size` for why the
# short side rather than the height or the width.
SIZE_PRESETS = {"small": 0.052, "medium": 0.064, "large": 0.078}

# How a run of words is cut into cues.
MAX_CUE_SECONDS = 5.0
# Below this a cue flashes rather than reads. Short cues are extended into the gap that follows
# where there is one, and merged into their neighbour where there is not.
MIN_CUE_SECONDS = 1.0
# A silence at least this long is a natural break even mid-sentence — the speaker paused, so the
# subtitle should too rather than bridging the gap with a line that sits there over nothing.
GAP_BREAK_SECONDS = 0.7
# Cues are never separated by less than this, so consecutive lines visibly change.
CUE_GAP_SECONDS = 0.04
DEFAULT_MAX_LINES = 2

_HARD_STOPS = "。！？!?…"
_SOFT_STOPS = "，、,;；：:"
# Trailing punctuation is dropped from what is displayed: the cut between cues already conveys
# it, and a lone comma at the end of a line is visual noise. Anything paired (quotes, brackets)
# is kept because dropping half of a pair looks like a bug.
_TRAILING_TRIM = _HARD_STOPS + _SOFT_STOPS + " \t"


@dataclass(frozen=True)
class Cue:
    """One subtitle line, timed against the voiceover rather than against the timeline."""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class SubtitleStyle:
    """Everything about how subtitles look, expressed as fractions of the frame.

    Nothing here is in pixels. Outputs may be 1280x720 or 720x1280; the selected frame is passed
    to `to_ass`, and the layout follows without a second set of numbers to keep in step.
    """

    font: str = DEFAULT_FONT
    # As a fraction of min(width, height) — the short side. Scaling by height would make text on
    # a 720x1280 portrait frame almost twice the size it is on 1280x720; scaling by width would
    # shrink it to nothing. The short side keeps a glyph the same physical size in both, which
    # also means the number of characters that fit on a line adapts on its own.
    size: float = SIZE_PRESETS["medium"]
    # Fraction of width kept clear at each side, and of height kept clear below.
    side_margin: float = 0.07
    bottom_margin: float = 0.085
    # Outline thickness as a fraction of the font size, so it thickens with the text.
    #
    # Set from what real footage needed rather than from what looks right on a dark test card.
    # White text with a thin outline disappears into a bright, busy frame — and an exhibition
    # hall, which is most of what this app films, is exactly that: white walls, signage, and
    # other people's captions already on screen.
    outline: float = 0.09
    # A drop shadow under the outline. Belt and braces for the same reason: where the background
    # happens to be near-black the outline alone stops separating the text from it.
    shadow: float = 0.045
    primary_colour: str = "FFFFFF"
    outline_colour: str = "000000"
    max_lines: int = DEFAULT_MAX_LINES

    def font_spec(self) -> BundledFont:
        return BUNDLED_FONTS.get(self.font) or BUNDLED_FONTS[DEFAULT_FONT]


def available_fonts() -> list[dict[str, Any]]:
    """The bundled fonts, and whether each is actually on disk.

    `installed` is reported rather than filtered out. A font missing from the build is a
    packaging fault, and a list that silently omits it looks identical to a list that never
    offered it.
    """
    return [
        {
            "key": font.key,
            "label": font.label,
            "note": font.note,
            "installed": (FONT_DIR / font.filename).exists(),
            # Inline rather than a URL. A stylesheet cannot send the bridge token, so a
            # @font-face pointing at an authenticated endpoint would simply fail to load — and
            # fail silently, leaving the picker showing the system font and looking like the
            # substitution bug this feature already had once.
            "preview": preview_data_uri(font.key),
        }
        for font in BUNDLED_FONTS.values()
    ]


def preview_data_uri(key: str) -> str | None:
    """The picker's cut-down copy of a font, as a `data:` URI, or None if it was never built."""
    path = PREVIEW_DIR / f"{key}.woff2"
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    return "data:font/woff2;base64," + base64.b64encode(payload).decode("ascii")


def missing_fonts() -> list[str]:
    return [font.key for font in BUNDLED_FONTS.values() if not (FONT_DIR / font.filename).exists()]


# ── reading what the TTS reported ───────────────────────────────────────────────────────────


def load_words(metadata_path: str | Path) -> tuple[list[dict[str, Any]], str | None]:
    """Strict provider word timings from a TTS sidecar, and why they are unavailable.

    Returns `(words, problem)`. The two failures that matter are told apart: a sidecar that
    cannot be read is a different situation from a provider that answered without timestamps.
    Rendering uses :func:`load_words_or_estimate`, which may recover the latter from reviewed
    text and actual audio duration; this strict function remains useful for diagnostics.
    """
    path = Path(metadata_path)
    if not path.exists():
        return [], f"找不到配音时间戳文件：{path.name}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], f"配音时间戳文件无法读取（{exc.__class__.__name__}）：{path.name}"
    if not isinstance(data, dict):
        return [], f"配音时间戳文件格式不对：{path.name}"
    words = data.get("words")
    if not isinstance(words, list) or not words:
        return [], "该配音没有逐字时间戳，无法生成字幕"
    restored = restore_source_spelling(
        words,
        str(data.get("text") or ""),
        duration_ms=data.get("duration_ms"),
    )
    if not restored:
        return [], "该配音的逐字时间戳损坏，无法安全生成字幕"
    return restored, None


def load_words_or_estimate(
    metadata_path: str | Path,
    *,
    audio_duration_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], Literal["exact", "estimated"] | None, str | None]:
    """Load trustworthy provider timings, or estimate them from reviewed text and duration.

    The provider's labels never become subtitle text. When its word clock is missing or unsafe,
    the exact text saved before synthesis is divided across the complete narration duration.
    ``problem`` is the reason estimation was needed, or the reason even estimation was unsafe.
    """
    words, problem = load_words(metadata_path)
    path = Path(metadata_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], None, problem
    if not isinstance(data, dict):
        return [], None, problem

    audio_duration_ms = None
    if audio_duration_seconds is not None and not isinstance(audio_duration_seconds, bool):
        try:
            audio_duration_ms = _positive_duration_ms(float(audio_duration_seconds) * 1000.0)
        except (TypeError, ValueError):
            audio_duration_ms = None

    if words:
        # Only new sidecars can prove their clock is exact. Legacy files predate provenance and
        # may already contain a proportional repair, so describe them conservatively.
        _restored, observed_quality = restore_source_spelling_with_quality(
            data.get("words") or [],
            str(data.get("text") or ""),
            duration_ms=data.get("duration_ms"),
        )
        quality: Literal["exact", "estimated"] = (
            "exact"
            if data.get("timing_quality") == "exact" and observed_quality == "exact"
            else "estimated"
        )
        source_text = data.get("text")
        if (
            audio_duration_ms is not None
            and not _word_timing_covers_duration(words, audio_duration_ms)
            and isinstance(source_text, str)
            and source_text.strip()
        ):
            # A repaired provider interval may end before the actual file. Once FFprobe has the
            # complete duration, rebuild the estimate across that full clock rather than keeping
            # a truncated subtitle track merely because its individual records are well formed.
            estimated = estimate_source_timing(source_text, audio_duration_ms)
            if estimated:
                return estimated, "estimated", None
        return words, quality, None

    source_text = data.get("text")
    if not isinstance(source_text, str) or not source_text.strip():
        return [], None, f"{problem}；缺少已确认的旁白文字，无法估算字幕"

    duration_ms = audio_duration_ms
    if duration_ms is None:
        duration_ms = _positive_duration_ms(data.get("duration_ms"))
    if duration_ms is None:
        return [], None, f"{problem}；无法取得旁白时长，无法估算字幕"

    estimated = estimate_source_timing(source_text, duration_ms)
    if not estimated:
        return [], None, f"{problem}；旁白文字或时长不可用，无法估算字幕"
    return estimated, "estimated", problem


def estimate_source_timing(source_text: str, duration_ms: float) -> list[dict[str, Any]]:
    """Spread reviewed narration over its audio duration with punctuation-aware weights.

    This is deliberately deterministic and inexpensive. Punctuation receives some of the clock
    so sentence and clause endings remain visible through the pause; cue building then uses those
    same punctuation boundaries and the normal line-length limit to produce readable subtitles.
    """
    source = re.sub(r"\s+", " ", source_text).strip()
    duration = _positive_duration_ms(duration_ms)
    if not source or duration is None:
        return []

    def weight(char: str) -> float:
        if char.isspace():
            return 0.2
        if char in _HARD_STOPS:
            return 1.6
        if char in _SOFT_STOPS:
            return 0.8
        if unicodedata.category(char).startswith("P"):
            return 0.4
        return 1.0

    weights = [weight(char) for char in source]
    total = sum(weights)
    elapsed = 0.0
    estimated: list[dict[str, Any]] = []
    for char, char_weight in zip(source, weights, strict=True):
        start = duration * elapsed / total
        elapsed += char_weight
        end = duration * elapsed / total
        estimated.append({
            "word": char,
            "start_time": round(start, 3),
            "end_time": round(end, 3),
        })
    return estimated


def _word_timing_covers_duration(
    words: Iterable[dict[str, Any]], duration_ms: float,
) -> bool:
    """Whether a word clock plausibly covers the complete audio file."""
    duration = _positive_duration_ms(duration_ms)
    if duration is None:
        return False
    timed = normalise_words(words)
    if not timed:
        return False
    start_ms = min(item[1] for item in timed) * 1000.0
    end_ms = max(item[2] for item in timed) * 1000.0
    span_ms = end_ms - start_ms
    tolerance = max(300.0, duration * 0.2)
    return (
        span_ms >= duration * 0.2
        and start_ms <= tolerance
        and end_ms >= duration - tolerance
        and end_ms <= duration + tolerance
    )


@dataclass(frozen=True)
class _TimestampToken:
    record: dict[str, Any]
    key: str
    text: str
    start: float
    end: float


def restore_source_spelling(
    raw_words: list[dict[str, Any]],
    source_text: str,
    *,
    duration_ms: Any = None,
) -> list[dict[str, Any]]:
    restored, _quality = restore_source_spelling_with_quality(
        raw_words,
        source_text,
        duration_ms=duration_ms,
    )
    return restored


def restore_source_spelling_with_quality(
    raw_words: list[dict[str, Any]],
    source_text: str,
    *,
    duration_ms: Any = None,
) -> tuple[list[dict[str, Any]], Literal["exact", "estimated"] | None]:
    """Use the reviewed source as text and the provider response only as a timing guide.

    Provider labels can normalise spelling (``OPC`` → ``opc`` or ``六和桥`` → ``六合桥``),
    omit punctuation, or occasionally shift a character. A character-level sequence alignment
    maps the reviewed source back onto trustworthy timing tokens. Weak or reordered alignments
    fall back to inexpensive proportional timing across the provider's spoken interval, so the
    customer never sees text that differs from the version approved for narration.
    """
    source = re.sub(r"\s+", " ", source_text).strip()
    if not source or not raw_words:
        # Provider timing labels are not an authoritative transcript. Without the reviewed text
        # there is no safe spelling to restore (a place name may already have been normalised),
        # so callers must surface timestamps/subtitles as unavailable rather than leak those
        # labels into the finished video.
        return [], None

    tokens: list[_TimestampToken] = []
    valid_indices: list[int] = []
    discarded_token = False
    for index, record in enumerate(raw_words):
        if not isinstance(record, dict):
            discarded_token = True
            continue
        key = "word" if isinstance(record.get("word"), str) else "text"
        value = record.get(key)
        if not isinstance(value, str) or not value:
            discarded_token = True
            continue
        if isinstance(record.get("start_time"), bool) or isinstance(record.get("end_time"), bool):
            # bool is an int in Python; without this guard True becomes a plausible-looking 1 ms.
            discarded_token = True
            continue
        try:
            start = float(record["start_time"])
            end = float(record["end_time"])
        except (KeyError, TypeError, ValueError):
            discarded_token = True
            continue
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            discarded_token = True
            continue
        tokens.append(_TimestampToken(record, key, value, start, end))
        valid_indices.append(index)

    if not tokens:
        # With no trustworthy spoken interval there is nowhere safe to place even the reviewed
        # characters. Returning the provider labels here would reintroduce exactly the names the
        # operator corrected, so callers must treat this as "timestamps unavailable" instead.
        return [], None
    out_of_order = any(
        later.start < earlier.start or later.end < earlier.end
        for earlier, later in itertools.pairwise(tokens)
    )
    overlapping = any(
        later.start < earlier.end for earlier, later in itertools.pairwise(tokens)
    )
    if not _spoken_interval_is_plausible(tokens, duration_ms):
        # A provider may return one valid-looking token and then truncate the response. Stretching
        # an entire approved paragraph over that tiny fragment makes every later subtitle appear
        # at the start of the narration. The provider's total audio duration is the authority for
        # deciding whether the surviving interval is complete enough to reuse.
        return [], None
    if discarded_token or out_of_order or overlapping:
        boundary_missing = valid_indices[0] != 0 or valid_indices[-1] != len(raw_words) - 1
        if boundary_missing and _positive_duration_ms(duration_ms) is None:
            # With no trustworthy total duration and a damaged first/last record, there is no
            # evidence for where the approved sentence begins or ends. Internal damage remains
            # recoverable when both outer provider timestamps survived.
            return [], None
        # A partially damaged response cannot preserve token-by-token alignment: the missing
        # record may have contained any part of the approved sentence. Keep the real outer
        # spoken interval from the valid records and rebuild a monotonic clock for the complete
        # reviewed source. This sacrifices some word-level precision but never its spelling or
        # order, and is safer than silently splicing provider text back into customer subtitles.
        return _proportional_source_timing(tokens, source), "estimated"

    provider = "".join(token.text for token in tokens)
    provider_keys = [_alignment_key(char) for char in provider]
    source_keys = [_alignment_key(char) for char in source]
    if not provider_keys:
        return [], None

    # Exact normalised order is the common path and avoids running the sequence matcher.
    if provider_keys == source_keys:
        return _restore_by_position(tokens, source), "exact"

    matcher = SequenceMatcher(None, provider_keys, source_keys)
    opcodes = matcher.get_opcodes()
    if not _alignment_is_confident(matcher.ratio(), opcodes, len(provider), len(source)):
        return _proportional_source_timing(tokens, source), "estimated"

    provider_to_token = [
        token_index
        for token_index, token in enumerate(tokens)
        for _char in token.text
    ]
    source_to_provider: list[int | None] = [None] * len(source)
    for tag, provider_start, provider_end, source_start, source_end in opcodes:
        if tag == "equal":
            for provider_index, source_index in zip(
                range(provider_start, provider_end),
                range(source_start, source_end),
                strict=True,
            ):
                source_to_provider[source_index] = provider_index
        elif tag == "replace" and provider_end > provider_start:
            provider_count = provider_end - provider_start
            source_count = source_end - source_start
            for offset, source_index in enumerate(range(source_start, source_end)):
                relative = (offset + 0.5) / max(1, source_count)
                provider_offset = min(provider_count - 1, int(relative * provider_count))
                source_to_provider[source_index] = provider_start + provider_offset
        elif tag == "insert":
            # The provider omitted source characters. Attach them to the nearest surrounding
            # spoken token without extending the narration beyond its real start or end.
            left = provider_start - 1 if provider_start > 0 else None
            right = provider_start if provider_start < len(provider) else None
            for offset, source_index in enumerate(range(source_start, source_end)):
                if left is None:
                    source_to_provider[source_index] = right
                elif right is None:
                    source_to_provider[source_index] = left
                else:
                    halfway = (source_end - source_start) / 2
                    source_to_provider[source_index] = left if offset < halfway else right

    if any(index is None for index in source_to_provider):
        return _proportional_source_timing(tokens, source), "estimated"
    token_mapping = [provider_to_token[index] for index in source_to_provider if index is not None]
    if any(second < first for first, second in itertools.pairwise(token_mapping)):
        return _proportional_source_timing(tokens, source), "estimated"
    return _source_text_on_tokens(tokens, source, token_mapping), "exact"


def _positive_duration_ms(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def _spoken_interval_is_plausible(
    tokens: Sequence[_TimestampToken],
    duration_ms: Any,
) -> bool:
    """Reject a surviving provider fragment that cannot represent the complete audio."""
    duration = _positive_duration_ms(duration_ms)
    if duration is None:
        return True
    start = min(token.start for token in tokens)
    end = max(token.end for token in tokens)
    span = end - start
    tolerance = max(300.0, duration * 0.2)
    return (
        span >= duration * 0.2
        and start <= tolerance
        and end >= duration - tolerance
        and end <= duration + tolerance
    )


def _alignment_key(char: str) -> str:
    if char.isspace():
        return " "
    return unicodedata.normalize("NFKC", char).casefold()


def _alignment_is_confident(
    ratio: float,
    opcodes: Sequence[tuple[str, int, int, int, int]],
    provider_length: int,
    source_length: int,
) -> bool:
    if ratio < 0.55:
        return False
    tags = {opcode[0] for opcode in opcodes}
    # An insertion and a deletion together commonly indicate moved/reordered text. Sequence
    # matching is monotonic, so pretending that move has a trustworthy word time is unsafe.
    if "insert" in tags and "delete" in tags:
        return False
    longest_provider_change = max(
        (end - start for tag, start, end, _s, _e in opcodes if tag != "equal"),
        default=0,
    )
    longest_source_change = max(
        (end - start for tag, _p, _q, start, end in opcodes if tag != "equal"),
        default=0,
    )
    allowance = max(2, math.ceil(max(provider_length, source_length) * 0.35))
    return longest_provider_change <= allowance and longest_source_change <= allowance


def _restore_by_position(tokens: Sequence[_TimestampToken], source: str) -> list[dict[str, Any]]:
    if sum(len(token.text) for token in tokens) != len(source):
        return _proportional_source_timing(tokens, source)
    restored: list[dict[str, Any]] = []
    cursor = 0
    for token in tokens:
        next_cursor = cursor + len(token.text)
        copy = dict(token.record)
        copy[token.key] = source[cursor:next_cursor]
        restored.append(copy)
        cursor = next_cursor
    return restored


def _source_text_on_tokens(
    tokens: Sequence[_TimestampToken],
    source: str,
    token_mapping: Sequence[int],
) -> list[dict[str, Any]]:
    chunks: list[tuple[int, list[str]]] = []
    for char, token_index in zip(source, token_mapping, strict=True):
        if not chunks or chunks[-1][0] != token_index:
            chunks.append((token_index, [char]))
        else:
            chunks[-1][1].append(char)
    restored: list[dict[str, Any]] = []
    for token_index, characters in chunks:
        token = tokens[token_index]
        copy = dict(token.record)
        copy[token.key] = "".join(characters)
        restored.append(copy)
    return restored


def _proportional_source_timing(
    tokens: Sequence[_TimestampToken], source: str,
) -> list[dict[str, Any]]:
    """Low-confidence fallback: exact source characters spread over the real spoken interval."""
    start = min(token.start for token in tokens)
    end = max(token.end for token in tokens)
    weights = [
        0.2 if char.isspace() else 0.35 if unicodedata.category(char).startswith("P") else 1.0
        for char in source
    ]
    total = sum(weights) or 1.0
    elapsed = 0.0
    restored: list[dict[str, Any]] = []
    for char, weight in zip(source, weights, strict=True):
        char_start = start + (end - start) * elapsed / total
        elapsed += weight
        char_end = start + (end - start) * elapsed / total
        restored.append({
            "word": char,
            "start_time": round(char_start, 3),
            "end_time": round(char_end, 3),
        })
    return restored


def normalise_words(raw_words: Iterable[dict[str, Any]]) -> list[tuple[str, float, float]]:
    """Provider word records as `(text, start_seconds, end_seconds)`, in time order.

    Times arrive in milliseconds. Records that carry no text or no usable timing are dropped
    rather than defaulted to zero, because a word defaulted to 0.0 does not look broken — it
    looks like a word spoken at the very start, and it would drag a cue's start with it.
    """
    out: list[tuple[str, float, float]] = []
    for record in raw_words:
        if not isinstance(record, dict):
            continue
        text = record.get("word")
        if text is None:
            text = record.get("text")
        if not isinstance(text, str) or not text:
            continue
        try:
            start = float(record["start_time"]) / 1000.0
            end = float(record["end_time"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            continue
        out.append((text, start, end))
    out.sort(key=lambda item: item[1])
    return out


# ── cue building ────────────────────────────────────────────────────────────────────────────


def display_width(text: str) -> int:
    """Width in half-widths: a CJK glyph is square and takes two, Latin takes one.

    Counting characters instead would let a line of Chinese run twice as wide as a line of
    English at the same count, which is how a subtitle overflows the frame.
    """
    return sum(2 if _is_wide(char) else 1 for char in text)


def _is_wide(char: str) -> bool:
    code = ord(char)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x20000 <= code <= 0x3FFFD
    )


def line_capacity(width: int, height: int, style: SubtitleStyle) -> int:
    """How many half-widths fit on one line at this frame size and style.

    This is what makes the same narration produce longer lines on a landscape frame and shorter
    ones on a portrait frame without anybody configuring it twice.
    """
    font_px = max(1.0, style.size * min(width, height))
    usable = width * (1.0 - 2.0 * style.side_margin)
    # A CJK glyph is one em wide, and one em is the font size. Half-widths are half of that.
    return max(6, int((usable / font_px) * 2))


def build_cues(
    words: Sequence[tuple[str, float, float]],
    capacity: int,
    max_lines: int = DEFAULT_MAX_LINES,
    max_seconds: float = MAX_CUE_SECONDS,
) -> list[Cue]:
    """Group timed words into readable cues.

    Breaks are taken, in order of preference, at a full stop, at a pause in the speech, at a
    comma, and finally wherever the line would otherwise overflow. Times come straight from the
    words, so a cue is on screen exactly while its words are being spoken.
    """
    if not words or capacity <= 0:
        return []
    budget = capacity * max(1, max_lines)

    cues: list[Cue] = []
    buffer: list[tuple[str, float, float]] = []

    def flush() -> None:
        if not buffer:
            return
        text = "".join(item[0] for item in buffer)
        text = _clean_cue_text(text)
        if text:
            cues.append(Cue(start=buffer[0][1], end=buffer[-1][2], text=text))
        buffer.clear()

    for index, (text, start, end) in enumerate(words):
        if buffer:
            width_with = display_width("".join(item[0] for item in buffer) + text)
            span = end - buffer[0][1]
            gap = start - buffer[-1][2]
            if width_with > budget or span > max_seconds or gap >= GAP_BREAK_SECONDS:
                flush()
        buffer.append((text, start, end))

        stripped = text.rstrip()
        if stripped and stripped[-1] in _HARD_STOPS:
            flush()
            continue
        # A comma only ends a cue once the line has enough on it to be worth showing; breaking
        # at every comma makes a stutter of two-character flashes.
        if (
            stripped
            and stripped[-1] in _SOFT_STOPS
            and display_width("".join(item[0] for item in buffer)) >= budget * 0.6
        ):
            flush()
    flush()

    return _settle_timing(cues)


def _clean_cue_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(_TRAILING_TRIM)


def _settle_timing(cues: list[Cue]) -> list[Cue]:
    """Give every cue long enough to be read, without letting one overlap the next.

    A cue shorter than `MIN_CUE_SECONDS` is stretched into the silence after it where there is
    silence to take. Where there is not, it stays short rather than stealing time from the next
    line — a subtitle that outlives its own sentence is worse than a brief one.
    """
    if not cues:
        return []
    settled: list[Cue] = []
    for index, cue in enumerate(cues):
        end = cue.end
        if cue.duration < MIN_CUE_SECONDS:
            wanted = cue.start + MIN_CUE_SECONDS
            # Only stretch into a *known* gap before another timed cue. The previous final-cue
            # fallback used ``wanted`` as its own ceiling and could leave the last sentence on
            # screen after the final spoken word and even after the audio had ended.
            ceiling = (
                cues[index + 1].start - CUE_GAP_SECONDS
                if index + 1 < len(cues)
                else cue.end
            )
            end = max(cue.end, min(wanted, ceiling))
        if index + 1 < len(cues):
            end = min(end, cues[index + 1].start - CUE_GAP_SECONDS)
        settled.append(replace(cue, end=max(end, cue.start + 0.05)))
    return settled


def wrap_cue(text: str, capacity: int, max_lines: int) -> str:
    """Break a cue across lines at the widest point that still fits.

    Done here rather than left to libass because Chinese has no spaces to break at: libass's own
    wrapping has to guess, and the guess is not stable across the sizes and aspect ratios this
    app renders at. `\\N` is the ASS hard break. Automatic wrapping is still left switched on in
    the script header as a safety net, so a line this misjudges gets folded rather than running
    off the side of the frame.
    """
    if display_width(text) <= capacity or max_lines <= 1:
        return text
    lines: list[str] = []
    remaining = text
    while remaining and len(lines) < max_lines:
        if display_width(remaining) <= capacity:
            lines.append(remaining)
            remaining = ""
            break
        cut = _cut_point(remaining, capacity)
        lines.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        lines[-1] = (lines[-1] + remaining).strip()
    return "\\N".join(line for line in lines if line)


def _cut_point(text: str, capacity: int) -> int:
    """The last index at which the line still fits, preferring a space or a punctuation mark."""
    width = 0
    limit = len(text)
    for index, char in enumerate(text):
        width += 2 if _is_wide(char) else 1
        if width > capacity:
            limit = index
            break
    limit = max(1, limit)
    window = text[:limit]
    for mark in (" ", *_SOFT_STOPS):
        found = window.rfind(mark)
        # Only honour a break that is not right at the start, or a long line collapses into a
        # one-character first line and everything else on the second.
        if found >= limit // 2:
            return found + 1
    return limit


# ── ASS serialisation ───────────────────────────────────────────────────────────────────────


def _ass_colour(rgb: str, alpha: int = 0) -> str:
    """`&HAABBGGRR`. ASS stores colour backwards and alpha inverted — 00 is opaque."""
    value = rgb.lstrip("#")
    if len(value) != 6:
        value = "FFFFFF"
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}".upper()


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _ass_text(text: str) -> str:
    """Make a line safe to put in an ASS event.

    Braces open an override block in ASS and a backslash starts a tag, so narration containing
    either would not be shown as written — at best it disappears, at worst the file no longer
    parses. Full-width substitutes read the same in Chinese text and cannot be mistaken for
    markup. `\\N` breaks inserted by `wrap_cue` are put back afterwards.
    """
    safe = text.replace("\\N", "\x00")
    safe = safe.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    safe = re.sub(r"[\r\n\t]+", " ", safe)
    return safe.replace("\x00", "\\N")


def to_ass(
    cues: Sequence[Cue],
    style: SubtitleStyle,
    width: int,
    height: int,
    offset: float = 0.0,
) -> str:
    """Render cues as an ASS script sized for this frame.

    `offset` is where the voiceover starts on the timeline. Cues are stored against the
    voiceover, so this is the only place the two are related, and moving the narration is a
    matter of writing this file again — no re-encode, and nothing else to keep in step.
    """
    font = style.font_spec()
    short_side = min(width, height)
    font_px = max(8, round(style.size * short_side))
    margin_h = max(0, round(style.side_margin * width))
    margin_v = max(0, round(style.bottom_margin * height))
    outline = max(1, round(font_px * style.outline))
    shadow = max(0, round(font_px * style.shadow))
    capacity = line_capacity(width, height, style)

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        # PlayRes is set to the real frame, so a point in the script is a pixel in the output and
        # libass is not scaling anything behind our backs.
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Main,{font.family},{font_px},"
            f"{_ass_colour(style.primary_colour)},{_ass_colour(style.primary_colour)},"
            f"{_ass_colour(style.outline_colour)},{_ass_colour('000000', alpha=0x80)},"
            f"{1 if font.bold else 0},0,0,0,100,100,0,0,"
            # BorderStyle 1 is outline+shadow. Alignment 2 is bottom centre.
            f"1,{outline},{shadow},2,{margin_h},{margin_h},{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    events = []
    for cue in cues:
        start = cue.start + offset
        end = cue.end + offset
        if end <= 0:
            continue
        body = _ass_text(wrap_cue(cue.text, capacity, style.max_lines))
        events.append(
            f"Dialogue: 0,{_ass_time(max(0.0, start))},{_ass_time(end)},Main,,0,0,0,,{body}"
        )
    return "\n".join(header + events) + "\n"


def cues_from_words(
    raw_words: Iterable[dict[str, Any]],
    style: SubtitleStyle,
    width: int,
    height: int,
) -> list[Cue]:
    """The whole path from provider timings to cues, at this frame size."""
    words = normalise_words(raw_words)
    return build_cues(words, line_capacity(width, height, style), style.max_lines)
