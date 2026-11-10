"""Measure only the VLM's LEVEL SCORING step, quickly, without Frigate.

The whole-system eval (`eval_alerts.py`) takes an hour each run and mixes faults from
every layer. This script sends Ollama exactly what Frigate 0.18.2 sends per review:
the same prompt (`evals/frigate-review-prompt.txt`), 8 frames spread evenly over the clip
at 480 height, the same model, JSON schema and parameters. One request per clip; a set of 40
clips takes about 45 minutes with the 8B model.

Use only for fixing on the `dev` set. Reported numbers come from the whole-system eval on
the `test` set, after the fix is settled.

    uv run python scripts/eval_alerts.py prepare --zip samples/eval/Dataset.zip --split dev
    uv run python scripts/eval_grading.py --rules evals/shoplifting/rules.json

`--frames` changes the frame count (Frigate adjusts it through `num_ctx`, at most 1 frame per
review second). Temperature defaults to 0 like the Frigate config: at 0.5 (Frigate's default) the
model answers randomly; one config run twice on dev gave 7/20 and 4/20, so no comparing configs.
"""

import argparse
import asyncio
import base64
import json
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from eval_alerts import ROOT, RULES, TIMELINE, TIMELINE_VIDEO

from vigisift.config import DEFAULT_VLM_MODEL
from vigisift.domain.evaluation import Clip, Observed, assign, score
from vigisift.domain.rules import (
    activity_context,
    check_prompt,
    from_dict,
    indicator_choices,
    match_level,
    review_report,
    validated,
)
from vigisift.infra.llm.ollama_picker import OllamaIndicatorPicker

PROMPT = ROOT / "evals" / "frigate-review-prompt.txt"
FRAMES = 8
HEIGHT = 480
# Frigate's `ReviewMetadata` without `time`, `other_concerns` (no concerns)
# and metadata keys, as `_clean_schema_for_ollama` does.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "items": {"type": "string", "minLength": 20, "maxLength": 200},
            "minItems": 3,
            "maxItems": 8,
        },
        "scene": {"type": "string", "minLength": 150, "maxLength": 600},
        "title": {"type": "string", "maxLength": 80},
        "shortSummary": {"type": "string", "minLength": 70, "maxLength": 140},
        "confidence": {"type": "number"},
        "potential_threat_level": {"type": "integer"},
    },
    "required": [
        "observations",
        "scene",
        "title",
        "shortSummary",
        "confidence",
        "potential_threat_level",
    ],
}
# As in deploy/frigate/config.template.yml.
NUM_CTX = 16384
OPTIONS = {
    "num_ctx": NUM_CTX,
    "num_predict": 1024,
    "temperature": 0,
    "repeat_penalty": 1.05,
    "presence_penalty": 0.3,
}


def _template() -> str:
    lines = PROMPT.read_text().splitlines(keepends=True)
    return "\n" + "".join(line for line in lines if not line.startswith("# "))


def _prompt(context: str, duration: float, language: str, frames: int) -> str:
    return _template().format(
        activity_context_prompt=context,
        concerns="",
        camera="Eval Cam",
        frames=frames,
        # Fix the time at opening hours: the real time at run (Frigate uses the review's) would
        # make numbers vary with when you press run, as the prompt is warier late at night.
        start="Saturday, 10:00 AM",
        duration=round(duration),
        zones="None",
        objects="\n- person",
        # `review.genai.preferred_language`; Frigate omits this line when it's unset.
        language=f"Provide your answer in {language}" if language else "",
    )


def _frames(clip: Clip, workdir: Path, count: int) -> list[str]:
    step = clip.duration_sec / (count - 1)
    images = []
    for index in range(count):
        # Pull the last frame back slightly: exactly at the end it's already the quiet scene.
        at = clip.offset_sec + min(index * step, clip.duration_sec - 0.2)
        target = workdir / f"{index}.jpg"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-nostats", "-loglevel", "error", "-y",
                "-ss", f"{at:.2f}", "-i", str(TIMELINE_VIDEO),
                "-frames:v", "1", "-vf", f"scale=-2:{HEIGHT}", "-q:v", "3", str(target),
            ],
            check=True,
        )  # fmt: skip
        images.append(base64.b64encode(target.read_bytes()).decode())
    return images


def _ask(args: argparse.Namespace, prompt: str, images: list[str]) -> dict[str, Any]:
    body = {
        "model": args.model,
        "prompt": prompt,
        "images": images,
        "format": SCHEMA,
        "think": False,
        "stream": False,
        "keep_alive": "10m",
        "options": OPTIONS | {"temperature": args.temperature},
    }
    request = urllib.request.Request(
        f"{args.ollama}/api/generate",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        reply: dict[str, Any] = json.load(response)
    return reply


async def main(args: argparse.Namespace) -> None:
    timeline = json.loads(TIMELINE.read_text())
    clips = [Clip(**c) for c in timeline["clips"]]
    raw = json.loads(args.rules.read_text())
    # Build the prompt with vigisift's own code: a GUIDANCE edit is measured right away.
    rules = validated(from_dict(raw))
    context = activity_context(rules)
    # Step 2, as VigiSift does on receiving a summary: compare the description with the rules.
    picker = OllamaIndicatorPicker(args.rule_model, args.ollama, num_ctx=NUM_CTX)
    observed, checked, combined, rows = [], [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        for index, clip in enumerate(clips):
            started = time.monotonic()
            prompt = _prompt(context, clip.duration_sec, args.language, args.frames)
            images = _frames(clip, Path(tmp), args.frames)
            reply = _ask(args, prompt, images)
            seconds = time.monotonic() - started
            try:
                answer = json.loads(reply.get("response") or "")
                level: int | None = int(answer["potential_threat_level"])
            except (ValueError, KeyError, TypeError):
                answer, level = {}, None
            description = review_report(
                answer.get("title"),
                answer.get("shortSummary"),
                answer.get("scene"),
                tuple(answer.get("observations") or ()),
            )
            match = match_level(
                rules, await picker.pick(check_prompt(rules, description), indicator_choices(rules))
            )
            observed.append(Observed(clip.name, clip.offset_sec, level))
            checked.append(Observed(clip.name, clip.offset_sec, match.level))
            combined.append(Observed(clip.name, clip.offset_sec, max(level or 0, match.level)))
            rows.append(
                {
                    "file": clip.name,
                    "suspicious": clip.suspicious,
                    "level": level,
                    "rule_level": match.level,
                    "rule_match": match.indicator,
                    "seconds": round(seconds),
                    "answer": answer,
                }
            )
            mark = f"{'?' if level is None else level}/{match.level}"
            label = "THEFT " if clip.suspicious else "normal"
            summary = str(answer.get("shortSummary", ""))[:90]
            print(
                f"{index + 1:2}/{len(clips)} {label} → {mark}  {seconds:4.0f}s  {summary}",
                flush=True,
            )
    await picker.aclose()

    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:.0%}"

    print(
        f"\nset {timeline.get('split', 'dev')}, rules {args.rules.name}, model {args.model} "
        f"(re-check: {args.rule_model}), "
        f"answers in {args.language or 'default'}, {args.frames} frames, "
        f"temperature {args.temperature}"
    )
    print(
        f"time per request: median {statistics.median(r['seconds'] for r in rows)}s, "
        f"longest {max(r['seconds'] for r in rows)}s (Frigate cuts at 120s)"
    )
    print(f"no answer {sum(r['level'] is None for r in rows)}")
    for name, levels in (
        ("Frigate (scoring from the images)", observed),
        ("VigiSift (re-check by rules)", checked),
        ("effective level (higher of the two)", combined),
    ):
        result = score(assign(clips, levels, tolerance_sec=0), levels)
        caught = f"{result.true_positives}/{result.suspicious_clips}"
        print(
            f"{name:32} recall {pct(result.recall)} ({caught})"
            f"  precision {pct(result.precision)}  specificity {pct(result.specificity)}"
        )
    out = ROOT / "evals" / "results" / f"grading-{int(time.time())}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "rules": raw,
                "model": args.model,
                "rule_model": args.rule_model,
                "language": args.language,
                "frames": args.frames,
                "temperature": args.temperature,
                "clips": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"details: {out.relative_to(ROOT)}")


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
parser.add_argument("--rules", type=Path, default=RULES)
parser.add_argument("--model", default=DEFAULT_VLM_MODEL, help="model Frigate describes with")
parser.add_argument("--rule-model", default=DEFAULT_VLM_MODEL, help="model VigiSift re-checks with")
parser.add_argument("--ollama", default="http://127.0.0.1:11434")
parser.add_argument("--frames", type=int, default=FRAMES)
parser.add_argument("--temperature", type=float, default=OPTIONS["temperature"])
parser.add_argument(
    "--language", default="", help="like Frigate's preferred_language; unset by default"
)

if __name__ == "__main__":
    if not TIMELINE_VIDEO.exists():
        sys.exit("no video yet: run `eval_alerts.py prepare` first")
    asyncio.run(main(parser.parse_args()))
