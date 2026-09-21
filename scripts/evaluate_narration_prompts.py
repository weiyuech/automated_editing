"""Explicitly invoked live evaluation of the app's configured LLM (never TTS).

Inputs and raw outputs are saved for human review. Credentials stay in SettingsService;
the artifact contains neither settings nor request headers. No automatic provider retry.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from automated_video_editing_backend.core.composition import NarrationAllocateRequest
from automated_video_editing_backend.services.llm import LLMService
from automated_video_editing_backend.services.mapped_narration import (
    MappedNarrationService,
)
from automated_video_editing_backend.services.settings import SettingsService


class EvaluationLLM(LLMService):
    """Capture exactly the provider response before production parsing."""

    async def _chat(self, cfg, **kwargs):
        self.raw = await super()._chat(cfg, **kwargs)
        if not hasattr(self, "calls"):
            self.calls = []
        self.calls.append({**kwargs, "model": cfg["model"], "raw_response": self.raw})
        return self.raw


class EvaluationNarration(MappedNarrationService):
    def __init__(self, llm, metadata, whole_draft=False):
        # Allocation needs metadata and LLM only; no media library or audio is modified.
        self.llm = llm
        self.metadata = metadata
        self.whole_draft = whole_draft

    def _material(self, key):
        return {}, None, self.metadata

    def prompt(self, key, request):
        if self.whole_draft:
            from automated_video_editing_backend.services.narration_context import (
                narration_context,
            )
            from automated_video_editing_backend.services.narration_prompts import (
                composition_prompt,
            )

            return {
                **composition_prompt(narration_context(self.metadata), request),
                "title": self.metadata["title"],
            }
        return super().prompt(key, request)


def text_units(text):
    """A rough length screen, NOT an audio duration measurement."""
    return len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", text))


def prompt_recipe(prompt):
    return {key: prompt[key] for key in ("system", "user", "writing_system", "writing_options") if key in prompt}


def prepare_case(case, settings, whole_draft=False):
    llm = EvaluationLLM(settings)
    request = NarrationAllocateRequest.model_validate(case["request"])
    if case.get("ordinary"):
        prompt = llm.voiceover_prompt(
            request.text,
            instructions=request.instructions,
            system_prompt=request.system_prompt,
            narration_style=request.narration_style,
        )
        call = lambda: llm.draft_voiceover(
            request.text,
            instructions=request.instructions,
            system_prompt=request.system_prompt,
            narration_style=request.narration_style,
        )
    else:
        service = EvaluationNarration(llm, case["metadata"], whole_draft=whole_draft)
        prompt = service.prompt(case["id"], request)
        call = lambda: service.allocate(case["id"], request)
    return llm, prompt, call


async def evaluate(case, settings, whole_draft=False):
    llm, prompt, call = prepare_case(case, settings, whole_draft)
    row = {
        "id": case["id"],
        "scenario": case["scenario"],
        "input": case,
        "prompt": {key: prompt[key] for key in ("system", "user")},
        "prompt_recipe": prompt_recipe(prompt),
        "model": settings.llm_config()["model"],
        "evaluation_strategy": "whole_draft" if whole_draft else "production",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    row["prompt_sha256"] = hashlib.sha256(
        json.dumps(row["prompt_recipe"], ensure_ascii=False).encode()
    ).hexdigest()
    started = time.monotonic()
    try:
        result = await call()
        row["result"] = {"text": result} if isinstance(result, str) else result
        row["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - preserve per-case failures for the evaluation report
        # Provider errors are deliberately sanitized by LLMService; don't dump traceback/config.
        row["status"] = "error"
        row["error"] = type(exc).__name__ + ": " + str(exc)
    row["elapsed_seconds"] = round(time.monotonic() - started, 2)
    row["raw_response"] = getattr(llm, "raw", None)
    row["calls"] = getattr(llm, "calls", [])
    spoken = row.get("result", {}).get("text", "")
    row["spoken_units"] = text_units(spoken)
    windows = {window["id"]: window for window in prompt.get("windows", [])}
    row["length_risks"] = [
        {
            "node_id": section["node_id"],
            "units": text_units(section["text"]),
            "seconds": windows[section["node_id"]]["duration"],
        }
        for section in row.get("result", {}).get("sections", [])
        if text_units(section["text"]) > windows[section["node_id"]]["duration"] * 4.5
    ]
    row["flagged_phrases"] = [
        word for word in case.get("forbidden", []) if word in spoken
    ]
    return row


async def main(args):
    if args.prompt_overrides:
        from automated_video_editing_backend.services import (
            grounded_narration,
            llm,
            mapped_narration,
            narration_prompts,
            narration_styles,
        )

        overrides = json.loads(args.prompt_overrides.read_text(encoding="utf-8"))
        for name in (
            "MAPPED_NARRATION_SYSTEM_PROMPT",
            "SIMPLE_COMPOSITION_SYSTEM_PROMPT",
        ):
            if name in overrides:
                setattr(narration_prompts, name, overrides[name])
        if "VOICEOVER_SYSTEM_PROMPT" in overrides:
            llm.VOICEOVER_SYSTEM_PROMPT = overrides["VOICEOVER_SYSTEM_PROMPT"]
        if "STYLE_RULES" in overrides:
            narration_styles.STYLE_RULES = overrides["STYLE_RULES"]
        for name in ("ROUTING_PROMPT", "WHOLE_WRITING_PROMPT"):
            if name in overrides:
                setattr(grounded_narration, name, overrides[name])
                setattr(mapped_narration, name, overrides[name])
        if overrides.get("MAPPED_WINDOW_BUDGETS"):
            original_builder = narration_prompts.mapped_narration_prompt

            def budgeted_prompt(*args, **kwargs):
                prompt = original_builder(*args, **kwargs)
                if kwargs.get("system_prompt") is None:
                    payload = json.loads(prompt["user"])
                    for window in payload["画面时间表"]:
                        window["max_chars"] = max(0, int(window["duration"] * 3))
                    prompt["user"] = json.dumps(payload, ensure_ascii=False, indent=2)
                return prompt

            narration_prompts.mapped_narration_prompt = budgeted_prompt
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.ids:
        ids = set(args.ids.split(","))
        cases = [case for case in cases if case["id"] in ids]
    if not cases:
        raise SystemExit("No matching evaluation cases")
    settings = SettingsService()
    cfg = settings.llm_config()
    if not cfg.get("enabled") or not cfg.get("api_key") or not cfg.get("model"):
        raise SystemExit("Configure and enable the application's LLM first")
    args.output.mkdir(parents=True, exist_ok=True)
    gate = asyncio.Semaphore(2)

    async def run(case):
        target = args.output / (case["id"] + ".json")
        if target.exists():
            previous = json.loads(target.read_text(encoding="utf-8"))
            _, prompt, _ = prepare_case(case, settings, args.whole_draft)
            if (
                previous.get("input") != case
                or previous.get("model") != cfg["model"]
                or previous.get("prompt_recipe", previous.get("prompt")) != prompt_recipe(prompt)
            ):
                raise ValueError(
                    f"{case['id']}: input, prompt or model changed; use a new output directory"
                )
            print(case["id"], "already recorded; skipped", flush=True)
            return
        async with gate:
            row = await evaluate(case, settings, args.whole_draft)
            target.write_text(
                json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                case["id"],
                row["status"],
                row["elapsed_seconds"],
                "length_risks=" + str(len(row["length_risks"])),
                flush=True,
            )

    await asyncio.gather(*(run(case) for case in cases))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ids", help="Optional comma-separated case IDs")
    parser.add_argument(
        "--whole-draft",
        action="store_true",
        help="Compare the former single-call mapped flow",
    )
    parser.add_argument(
        "--prompt-overrides",
        type=Path,
        help="Evaluate candidate rules without editing production",
    )
    asyncio.run(main(parser.parse_args()))
