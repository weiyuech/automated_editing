"""Guard against tests writing into the real data/ directory.

This has bitten twice: SeedanceService burned the live daily quota counter, and
CaptureService wrote 34 junk sessions into data/capture-sessions.json. Both services
default their storage path to the project's data directory, which is correct for the app
and wrong for a test, so every construction in a test must pass an explicit path.
"""

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent

# service -> the keyword argument a test must supply to stay out of data/
ISOLATION_REQUIRED = {
    "CaptureService": "path",
    "CruiseRouteStore": "path",
    "MediaService": "path",
    "SeedanceService": "root",
    "SettingsService": "path",
}


def _constructions(source: str, service: str) -> list[str]:
    """Every `Service(...)` call, with balanced parentheses so nested calls survive."""
    found = []
    for match in re.finditer(rf"\b{service}\(", source):
        depth, index = 0, match.end() - 1
        while index < len(source):
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
                if depth == 0:
                    found.append(source[match.start() : index + 1])
                    break
            index += 1
    return found


@pytest.mark.parametrize("service,keyword", sorted(ISOLATION_REQUIRED.items()))
def test_no_test_constructs_a_storage_service_without_isolation(service, keyword):
    offenders = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        for call in _constructions(source, service):
            if f"{keyword}=" not in call:
                offenders.append(f"{path.name}: {call}")

    assert not offenders, (
        f"{service} must be constructed with {keyword}=... in tests, or it writes into the "
        f"real data/ directory:\n" + "\n".join(offenders)
    )
