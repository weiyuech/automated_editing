"""Regressions from real provider responses: empty notes are not empty narration."""

import json
from types import SimpleNamespace

import pytest

from automated_video_editing_backend.services.mapped_narration import MappedNarrationService


@pytest.mark.asyncio
async def test_allocate_omits_empty_note_placeholders_but_preserves_spoken_sections():
    windows = [{"id": "coffee/A"}, {"id": "coffee/transit"}]
    response = {
        "sections": [{"node_id": "coffee/A", "text": "机身宽18厘米。"}],
        "note_assignments": [
            {"node_id": "coffee/A", "text": "拍外观与尺寸"},
            {"node_id": "coffee/transit", "text": ""},
        ],
        "general_notes": [],
    }

    async def chat(*args, **kwargs):
        return json.dumps(response)

    service = object.__new__(MappedNarrationService)
    service.llm = SimpleNamespace(
        settings=SimpleNamespace(llm_config=lambda: {"enabled": True}),
        _configured=lambda config: True,
        _chat=chat,
    )
    service.prompt = lambda *args: dict(
        mode="single_recording",
        windows=windows,
        capture_notes=["拍外观与尺寸"],
        system="rules",
        user="context",
    )
    result = await service.allocate("coffee", SimpleNamespace(instructions="", text="机身宽18厘米。"))
    assert result["text"] == "机身宽18厘米。"
    assert result["sections"] == response["sections"]
    assert result["note_assignments"] == response["note_assignments"][:1]


@pytest.mark.parametrize(
    "entries",
    [
        [{"node_id": "unknown", "text": ""}],
        [{"node_id": "A", "text": ""}, {"node_id": "A", "text": "备注"}],
        [{"node_id": "A", "text": None}],
        [{"node_id": [], "text": ""}],
        [{"text": ""}],
        {},
        [{"node_id": "A", "text": ""}] * 101,
    ],
)
def test_empty_note_tolerance_never_hides_invalid_nodes_duplicates_or_shape(entries):
    with pytest.raises(ValueError):
        MappedNarrationService._validate_assignments(entries, [{"id": "A"}], omit_empty=True)


def test_user_saved_bindings_remain_strict():
    entries = [{"node_id": "A", "text": ""}]
    with pytest.raises(ValueError):
        MappedNarrationService._validate_assignments(entries, [{"id": "A"}])
    assert (
        MappedNarrationService._validate_assignments(entries, [{"id": "A"}], omit_empty=True) == []
    )
    assert (
        MappedNarrationService._validate_assignments(
            [{"node_id": "A", "text": "  \n"}], [{"id": "A"}], omit_empty=True
        )
        == []
    )
