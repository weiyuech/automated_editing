from __future__ import annotations

import ast
from safety import APP_ROOT, assert_inside_root, ensure_runtime_dirs


def main() -> int:
    ensure_runtime_dirs()
    for area in ["data", ".cache", "logs", "exports", "previews"]:
        assert_inside_root(APP_ROOT / area)
    for py_file in (APP_ROOT / "backend" / "src").rglob("*.py"):
        ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    print("smoke check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
