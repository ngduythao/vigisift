"""Measure the whole system (camera → Frigate → VLM → VigiSift) on labelled clips.

Dataset: "Shoplifting Dataset (2022)", MNNIT, Mendeley Data,
doi:10.17632/r3yjf35hzr, CC BY 4.0. Actors in a staged store, one fixed
camera: not real CCTV, so these numbers are a baseline for comparing
configurations, not real-world accuracy.

    # 1. Once: pick clips (the manifest is committed, the videos aren't). The `dev` set
    #    is for finding faults and fixing the prompt; the `test` set (no shared clips) is only
    #    for measuring after a fix, so the numbers aren't "memorising" the dev set.
    uv run python scripts/eval_alerts.py select --zip samples/eval/Dataset.zip --split dev
    uv run python scripts/eval_alerts.py select --zip samples/eval/Dataset.zip --split test --seed 8
    uv run python scripts/eval_alerts.py select --zip samples/eval/Dataset.zip \
        --split test2 --seed 9
    uv run python scripts/eval_alerts.py select --zip samples/eval/Dataset.zip \
        --split test3 --seed 10
    uv run python scripts/eval_alerts.py select --zip samples/eval/Dataset.zip \
        --split test4 --seed 11 --per-label 10
    # 2. Extract, normalise and join into samples/eval-cam.mp4
    uv run python scripts/eval_alerts.py prepare --zip samples/eval/Dataset.zip \
        --split test4 --seed 11
    # 3. Set the eval rules on the camera, play, then record the run's start mark
    VIGISIFT_USER=admin VIGISIFT_PASSWORD=... uv run python scripts/eval_alerts.py rules
    docker compose restart mediamtx
    uv run python scripts/eval_alerts.py mark
    # 4. Wait for the video to end plus a few minutes for summaries, then score
    VIGISIFT_USER=admin VIGISIFT_PASSWORD=... uv run python scripts/eval_alerts.py score

Alerts are assigned to the clip playing when they started; a clip counts as "flagged
by AI" if an alert scored suspicion level ≥ 1.
"""

import argparse
import base64
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path

from vigisift.domain.evaluation import Clip, Observed, assign, score, spills

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals" / "shoplifting"
# The "suspicious" rules the eval uses for the eval_cam camera, committed with the clips.
RULES = EVALS / "rules.json"
# Each test set is reported once: `test` for run 4, `test2` after fixing the detection layer,
# `test3` after moving to 10 frames and temperature 0, `test4` (10 + 10, nearly all remaining
# clips) after moving to the 8B model.
SPLITS = ["dev", "test", "test2", "test3", "test4"]
WORK = ROOT / "samples" / "eval"
TIMELINE_VIDEO = ROOT / "samples" / "eval-cam.mp4"
TIMELINE = WORK / "timeline.json"
# This run's playback start mark. Written to a file because restarting MediaMTX changes
# `readyTime`, and every older alert would be measured from the wrong mark.
RUN = WORK / "run.json"
LABELS = {"Shoplifting": True, "Normal": False}
LEAD_IN_SEC = 30
# Quiet gap between clips: the previous clip's review must close before the next clip
# starts. Frigate closes an alert `cutoff_time` (40 seconds) after the person disappears,
# and holds a "disappeared" person a few more seconds; 75 seconds leaves room.
GAP_SEC = 75
SIZE = (640, 480)
FPS = 15


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(split: str) -> Path:
    return EVALS / f"{split}.jsonl"


def select(args: argparse.Namespace) -> None:
    # Don't reuse clips already in another set.
    taken = {
        json.loads(line)["file"]
        for other in EVALS.glob("*.jsonl")
        if other != _manifest(args.split)
        for line in other.read_text().splitlines()
    }
    with zipfile.ZipFile(args.zip) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".mp4") and n not in taken]
        rows = []
        for folder, suspicious in LABELS.items():
            pool = sorted(n for n in names if f"/{folder}/" in n)
            chosen = sorted(random.Random(args.seed).sample(pool, args.per_label))
            rows += [
                {"file": n, "suspicious": suspicious, "sha256": _sha(archive.read(n))}
                for n in chosen
            ]
    target = _manifest(args.split)
    target.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"picked {len(rows)} clips → {target.relative_to(ROOT)}")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostats", "-loglevel", "error", "-y", *args], check=True)


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def _normalise(source: Path, target: Path) -> None:
    w, h = SIZE
    fit = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
    _ffmpeg(
        "-i", str(source), "-an",
        "-vf", f"{fit},fps={FPS},setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-g", str(FPS * 2), "-pix_fmt", "yuv420p",
        str(target),
    )  # fmt: skip


def _quiet(seconds: int, target: Path) -> None:
    w, h = SIZE
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c=0x404040:s={w}x{h}:r={FPS}:d={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-g", str(FPS * 2), "-pix_fmt", "yuv420p",
        str(target),
    )  # fmt: skip


def prepare(args: argparse.Namespace) -> None:
    rows = [json.loads(line) for line in _manifest(args.split).read_text().splitlines()]
    # Interleave the two labels in a fixed order: don't let all theft clips bunch together.
    random.Random(args.seed).shuffle(rows)
    WORK.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(args.zip) as archive:
        tmpdir = Path(tmp)
        lead, gap = tmpdir / "lead.mp4", tmpdir / "gap.mp4"
        _quiet(LEAD_IN_SEC, lead)
        _quiet(GAP_SEC, gap)
        parts, clips, offset = [lead], [], float(LEAD_IN_SEC)
        for index, row in enumerate(rows):
            data = archive.read(row["file"])
            if _sha(data) != row["sha256"]:
                sys.exit(f"{row['file']}: sha256 doesn't match the manifest")
            raw, clean = tmpdir / f"raw-{index}.mp4", tmpdir / f"clip-{index}.mp4"
            raw.write_bytes(data)
            _normalise(raw, clean)
            duration = _duration(clean)
            clips.append(Clip(row["file"], row["suspicious"], offset, duration))
            parts += [clean, gap]
            offset += duration + GAP_SEC
        playlist = tmpdir / "playlist.txt"
        playlist.write_text("".join(f"file '{p}'\n" for p in parts))
        _ffmpeg(
            "-f", "concat", "-safe", "0", "-i", str(playlist), "-c", "copy", str(TIMELINE_VIDEO)
        )
    TIMELINE.write_text(
        json.dumps(
            {"split": args.split, "duration_sec": offset, "clips": [asdict(c) for c in clips]},
            indent=2,
        )
    )
    print(f"{len(clips)} clips, video {offset / 60:.1f} min → {TIMELINE_VIDEO.relative_to(ROOT)}")


def _path_state(args: argparse.Namespace) -> dict[str, object]:
    request = urllib.request.Request(f"{args.mediamtx}/v3/paths/get/{args.path}")
    token = base64.b64encode(f"{args.mediamtx_user}:{args.mediamtx_password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=10) as response:
        state: dict[str, object] = json.load(response)
    return state


def _time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def mark(args: argparse.Namespace) -> None:
    """Wait for the fake camera and Frigate to start, then record `readyTime` as the run's mark."""
    while True:
        try:
            state = _path_state(args)
        except OSError:
            state = {}
        if state.get("ready") and state.get("readers"):
            break
        time.sleep(2)
    ready = str(state["readyTime"])
    RUN.write_text(json.dumps({"ready_time": ready}))
    finish = _time(ready) + json.loads(TIMELINE.read_text())["duration_sec"]
    print(f"playback started {ready}; video ends {datetime.fromtimestamp(finish).isoformat()}")


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds).astimezone().isoformat()


def _signed_in(args: argparse.Namespace) -> urllib.request.OpenerDirector:
    """Sign in to VigiSift as an operator: the eval takes the same path operators use."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    user, password = os.environ["VIGISIFT_USER"], os.environ["VIGISIFT_PASSWORD"]
    login = urllib.request.Request(
        f"{args.vigisift}/auth/login",
        json.dumps({"username": user, "password": password}).encode(),
        {"Content-Type": "application/json"},
    )
    opener.open(login, timeout=10)
    return opener


def rules(args: argparse.Namespace) -> None:
    """Set the eval rules on the eval camera through the API (needs an admin account)."""
    request = urllib.request.Request(
        f"{args.vigisift}/sites/{args.site}/cameras/{args.camera}/rules",
        RULES.read_bytes(),
        {"Content-Type": "application/json"},
        method="PUT",
    )
    with _signed_in(args).open(request, timeout=60) as response:
        view = json.load(response)
    print(f"rules {args.site}/{args.camera}: applied={view['applied']} error={view['apply_error']}")


def _alerts(args: argparse.Namespace, start: float, end: float) -> list[dict[str, object]]:
    """Every eval camera alert starting in [start, end), fetched by page."""
    opener = _signed_in(args)
    rows: dict[str, dict[str, object]] = {}
    before = end
    while True:
        query = urllib.parse.urlencode(
            {
                "site_id": args.site,
                "camera": args.camera,
                "since_minutes": 7 * 24 * 60,
                "started_after": _iso(start),
                "started_before": _iso(before),
                "limit": 200,
            }
        )
        with opener.open(f"{args.vigisift}/alerts?{query}", timeout=30) as response:
            page: list[dict[str, object]] = json.load(response)
        for row in page:
            rows[str(row["review_id"])] = row
        if len(page) < 200:
            return list(rows.values())
        oldest = min(_time(str(r["started_at"])) for r in page)
        if oldest >= before:
            sys.exit("can't paginate: over 200 alerts at the same instant")
        before = oldest


def _optional_time(value: object) -> float | None:
    return _time(str(value)) if value else None


def score_run(args: argparse.Namespace) -> None:
    timeline = json.loads(TIMELINE.read_text())
    clips = [Clip(**c) for c in timeline["clips"]]
    print(f"set {timeline.get('split', 'dev')}")
    if not RUN.exists():
        sys.exit("no start mark yet: run `eval_alerts.py mark` right after playback starts")
    start = _time(json.loads(RUN.read_text())["ready_time"])
    # First playback loop only: the fake camera loops. Score by effective level (what
    # operators see); keep Frigate's own level apart to see what VigiSift adds.
    observed: list[Observed] = []
    frigate_only: list[Observed] = []
    for alert in _alerts(args, start, start + timeline["duration_sec"]):
        started = _time(str(alert["started_at"]))
        ended = _optional_time(alert["ended_at"])
        summarised = _optional_time(alert["summarised_at"])
        delay = summarised - ended if summarised is not None and ended is not None else None
        for target, key in ((observed, "level"), (frigate_only, "threat_level")):
            level = alert[key]
            target.append(
                Observed(
                    str(alert["review_id"]),
                    started - start,
                    int(str(level)) if level is not None else None,
                    delay,
                    ended - start if ended is not None else None,
                )
            )
    results = assign(clips, observed, tolerance_sec=args.tolerance)
    frigate_report = score(assign(clips, frigate_only, tolerance_sec=args.tolerance), frigate_only)
    report = score(results, observed)
    matched = {rid for r in results for rid in r.review_ids}
    spilled = spills(results, observed)
    unfinished = [
        o.review_id
        for o in observed
        if o.review_id in matched and (o.end_sec is None or o.threat_level is None)
    ]
    print(f"{len(observed)} alerts in the video, {len(observed) - len(matched)} outside any clip")
    if unfinished:
        # Counted as "AI didn't flag". If it's from scoring too early, wait and score again.
        print(f"Note: {len(unfinished)} alerts not closed or without a summary: {unfinished}")
    if spilled:
        print(f"WARNING: {len(spilled)} alerts spilled into the next clip, unreliable: {spilled}")

    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:.0%}"

    clips_line = f"{report.clips} ({report.suspicious_clips} theft, {report.normal_clips} normal)"
    caught = f"{report.true_positives}/{report.suspicious_clips}"
    fah = report.false_alarms_per_hour
    delay = report.median_summary_delay_sec
    print()
    print(f"| Clip | {clips_line} |")
    print("|---|---|")
    frigate_caught = f"{frigate_report.true_positives}/{frigate_report.suspicious_clips}"
    print(f"| Caught (recall) | {pct(report.recall)} ({caught}) |")
    print(
        f"| ... with Frigate's own level only | {pct(frigate_report.recall)} ({frigate_caught}) |"
    )
    print(f"| Correct flags (precision) | {pct(report.precision)} |")
    print(f"| Normal clips not flagged (specificity) | {pct(report.specificity)} |")
    print(
        f"| Normal clips falsely flagged per video hour | {'-' if fah is None else f'{fah:.1f}'} |"
    )
    print(f"| Per-clip outcomes | {report.outcomes} |")
    delay_text = "-" if delay is None else f"{delay:.0f} seconds"
    print(f"| Summary delay after the review closes (median) | {delay_text} |")
    out = ROOT / "evals" / "results" / f"eval-{int(time.time())}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "split": timeline.get("split", "dev"),
                "ready_time": json.loads(RUN.read_text())["ready_time"],
                "spilled": spilled,
                "clips": [
                    {
                        "file": r.clip.name,
                        "suspicious": r.clip.suspicious,
                        "outcome": r.outcome,
                        "max_threat": r.max_threat,
                        "reviews": r.review_ids,
                    }
                    for r in results
                ],
                "summary": {
                    "frigate_only_recall": frigate_report.recall,
                    "frigate_only_precision": frigate_report.precision,
                    "recall": report.recall,
                    "precision": report.precision,
                    "specificity": report.specificity,
                    "false_alarm_clips_per_hour": report.false_alarms_per_hour,
                    "outcomes": report.outcomes,
                    "median_summary_delay_sec": report.median_summary_delay_sec,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"\ndetails: {out.relative_to(ROOT)}")


def _mediamtx(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mediamtx", default="http://127.0.0.1:9997")
    parser.add_argument("--mediamtx-user", default="vigisift")
    parser.add_argument("--mediamtx-password", default="vigisift")
    parser.add_argument("--path", default="eval-cam")


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
sub = parser.add_subparsers(dest="command", required=True)
p_select = sub.add_parser("select")
p_select.add_argument("--zip", type=Path, required=True)
p_select.add_argument("--split", choices=SPLITS, default="dev")
p_select.add_argument("--per-label", type=int, default=20)
p_select.add_argument("--seed", type=int, default=7)
p_prepare = sub.add_parser("prepare")
p_prepare.add_argument("--zip", type=Path, required=True)
p_prepare.add_argument("--split", choices=SPLITS, default="dev")
p_prepare.add_argument("--seed", type=int, default=7)
_mediamtx(sub.add_parser("mark"))
p_rules = sub.add_parser("rules")
p_score = sub.add_parser("score")
for p in (p_rules, p_score):
    p.add_argument("--vigisift", default="http://127.0.0.1:8000")
    p.add_argument("--site", default="demo")
    p.add_argument("--camera", default="eval_cam")
p_score.add_argument("--tolerance", type=float, default=10)

if __name__ == "__main__":
    parsed = parser.parse_args()
    {"select": select, "prepare": prepare, "mark": mark, "rules": rules, "score": score_run}[
        parsed.command
    ](parsed)
