"""Durable reading and writing of the small JSON files the app keeps its state in.

Capture notes and markers, saved cruise routes, Seedance's paid quota, the daily output
allowance — each is a modest file, and each was written with a plain `write_text` and read back
with a `try/except` returning an empty collection.

That pairing loses data quietly. A write interrupted part-way — a crash, a full disk, a laptop
lid — leaves a truncated file. The next read cannot parse it, answers "empty", and empty is a
perfectly legitimate state: no sessions yet, no routes saved, nothing generated today. The
caller has no way to tell an empty store from a destroyed one, so nothing reports it, and the
next save overwrites whatever was left.

Two rules fix it, and both belong here rather than in each store:

- **Write whole, then move.** A rename is atomic, so what is on disk is always one complete
  version or the previous one. A torn file cannot be produced.
- **Never overwrite something unreadable.** A file that fails to parse is moved aside rather
  than left to be replaced, so the bytes survive for a human to look at, and the failure has a
  name instead of looking like emptiness.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> bool:
    """Write `payload` so that no interruption can leave a half-file. True when it landed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        temporary.replace(path)
        return True
    except OSError:
        Path(str(path) + ".tmp").unlink(missing_ok=True)
        return False


def read_json(path: Path) -> tuple[Any | None, str]:
    """Read `path`, returning `(data, problem)`.

    `(None, "")` means the file is simply not there — a new install, an empty store, nothing
    wrong. `(None, reason)` means it existed and could not be read, which is a different thing
    and has to be said out loud: it is someone's notes or saved routes, not an absence.

    An unreadable file is moved aside before returning, so the store that owns it can save
    again without destroying the evidence.
    """
    if not path.exists():
        return None, ""
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except json.JSONDecodeError as exc:
        moved = _quarantine(path)
        where = f"，原文件已保留为 {moved.name}" if moved else ""
        return None, f"{path.name} 损坏，无法读取（{exc.msg}）{where}"
    except OSError as exc:
        return None, f"{path.name} 无法读取：{exc}"


def _quarantine(path: Path) -> Path | None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        path.rename(target)
        return target
    except OSError:
        return None
