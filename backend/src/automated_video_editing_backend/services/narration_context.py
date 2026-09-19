"""Select narration policy from original recordings, not the number of current files."""


def original_recording_ids(tree):
    def visit(node):
        if "source_recording_ids" in node:
            return set(node["source_recording_ids"]) or {"unknown:" + node["id"]}
        nested = set().union(*(visit(child) for child in node.get("children", [])))
        if nested:
            return nested
        if node.get("kind") == "recording" and node.get("id"):
            # Legacy manifests retain recording nodes but lack stable capture identities.
            # Keep distinct identities rather than guessing from a place/name label.
            return {"legacy:" + node["id"]}
        return set()

    return sorted(set().union(*(visit(root) for root in tree)))


def narration_windows(tree):
    def visit(node, notes):
        notes = node.get("notes") or notes
        if node.get("kind") in {"dwell", "transit"} or not node.get("children"):
            return [
                {
                    **{key: node[key] for key in ("id", "label", "start", "end", "duration")},
                    "kind": node.get("kind"),
                    "notes": notes,
                }
            ]
        return [window for child in node["children"] for window in visit(child, notes)]

    return [window for root in tree for window in visit(root, [])]


def capture_notes(tree):
    """Preserve whole operator paragraphs once; punctuation is not a point binding."""
    result = []

    def visit(node):
        values = node.get("notes") or []
        for value in [values] if isinstance(values, str) else values:
            if isinstance(value, str) and value.strip() and value.strip() not in result:
                result.append(value.strip())
        for child in node.get("children", []):
            visit(child)

    for node in tree:
        visit(node)
    return result


def narration_context(metadata):
    tree = metadata.get("composition_tree", [])
    ids = sorted(set(metadata.get("source_recording_ids") or original_recording_ids(tree)))
    mode = (
        "unknown"
        if not ids or any(key.startswith("unknown:") for key in ids)
        else "single_recording"
        if len(ids) == 1
        else "multiple_recordings"
    )
    return {
        "mode": mode,
        "source_count": len(ids),
        "source_recording_ids": ids,
        "duration_seconds": metadata["duration_seconds"],
        "capture_notes": capture_notes(tree) if mode == "single_recording" else [],
        "windows": [{**window, "notes": []} for window in narration_windows(tree)]
        if mode == "single_recording"
        else [],
    }
