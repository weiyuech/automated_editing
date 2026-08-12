from __future__ import annotations

from datetime import datetime
from pathlib import Path

# ':' is legal on macOS but not on Windows, and this app ships to both.
ILLEGAL_IN_FILENAME = set('/\\:*?"<>|')


def stamped_name(prefix: str, suffix: str, taken: set[str] | None = None) -> str:
    """A readable, unique-per-minute filename: '图片特效 08-05 17-30.png'.

    Uses '-' rather than ':' in the time because a colon is not a legal filename character
    on Windows.
    """
    stamp = datetime.now().astimezone().strftime("%m-%d %H-%M")
    base = f"{prefix} {stamp}"
    candidate = f"{base}{suffix}"
    if not taken or candidate not in taken:
        return candidate
    index = 2
    while f"{base}-{index}{suffix}" in taken:
        index += 1
    return f"{base}-{index}{suffix}"


def validate_filename(name: str, fallback_suffix: str) -> str:
    """Turn operator input into a safe filename, or explain why it cannot be used."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("A name is required")
    if cleaned in {".", ".."} or any(char in ILLEGAL_IN_FILENAME for char in cleaned):
        # A filename is a place to write, so path characters are how a rename would
        # accidentally write outside the folder.
        raise ValueError("A name cannot contain / \\ : * ? \" < > |")
    if len(cleaned) > 120:
        raise ValueError("That name is too long")
    if not Path(cleaned).suffix:
        cleaned = f"{cleaned}{fallback_suffix}"
    return cleaned


def safe_stem(value: str, fallback: str) -> str:
    """Keep the operator's own words in a filename, dropping only what is illegal.

    The old rule stripped every non-ASCII character, so a Chinese title became empty and
    was silently replaced by a generic name.
    """
    cleaned = "".join(ch for ch in (value or "") if ch not in ILLEGAL_IN_FILENAME)
    cleaned = cleaned.strip().strip(".")
    return cleaned[:60].strip() or fallback
