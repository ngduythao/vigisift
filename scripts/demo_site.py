"""Build the static demo: the real console page answering from recorded alerts and clips.

    # 1. With a VigiSift stack running and some summarised alerts, record them
    VIGISIFT_USER=... VIGISIFT_PASSWORD=... uv run python scripts/demo_site.py capture
    # 2. Optionally drop alerts you don't want to show, by review id
    uv run python scripts/demo_site.py drop 1793937504.946921-i8b7rc
    # 3. Build the site (index.html, demo.js, fonts, data, clips) into samples/demo/site
    uv run python scripts/demo_site.py build
    # 4. Publish the folder, for example: npx vercel deploy --prod samples/demo/site

Recordings and the built site live under samples/, so clips never enter git. Only alerts
that ended and have a clip are kept: the demo plays every alert it lists.
"""

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "src" / "vigisift" / "web"
SHIM = ROOT / "demo" / "demo.js"
WORK = ROOT / "samples" / "demo"
DATA = WORK / "data"
SITE = WORK / "site"
REPO = "https://github.com/ngduythao/vigisift"

BANNER = f"""<div id="demo-banner">
  Demo with recorded alerts. The AI text is real output from qwen3-vl:8b-instruct on staged clips
  (Shoplifting Dataset, MNNIT, CC BY 4.0) and a public store video (Intel, CC BY 4.0).
  Verdicts stay in this tab. <a href="{REPO}">Source on GitHub</a>
</div>
<style>
  body {{ display: flex; flex-direction: column; }}
  #app {{ flex: 1; min-height: 0; height: auto; }}
  #demo-banner {{ flex: none; padding: 6px 16px; background: var(--bg-raised);
    border-bottom: 1px solid var(--line); color: var(--text-2); font-size: 12px;
    line-height: 1.5; }}
  #demo-banner a {{ color: var(--text); }}
</style>
"""


def _opener(base: str) -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    body = json.dumps(
        {"username": os.environ["VIGISIFT_USER"], "password": os.environ["VIGISIFT_PASSWORD"]}
    ).encode()
    request = urllib.request.Request(
        f"{base}/auth/login", body, {"Content-Type": "application/json"}
    )
    opener.open(request, timeout=10)
    return opener


def _get(opener: urllib.request.OpenerDirector, url: str) -> Any:
    with opener.open(url, timeout=30) as response:
        return json.load(response)


def capture(args: argparse.Namespace) -> None:
    opener = _opener(args.vigisift)
    query = urllib.parse.urlencode({"since_minutes": 7 * 24 * 60, "limit": 200})
    alerts = _get(opener, f"{args.vigisift}/alerts?{query}")
    (DATA / "clips").mkdir(parents=True, exist_ok=True)
    kept = []
    for alert in alerts:
        if alert["ended_at"] is None or alert["severity"] != "alert":
            continue
        site, review = alert["site_id"], alert["review_id"]
        quoted = f"{urllib.parse.quote(site)}/{urllib.parse.quote(review)}"
        clip = f"{args.vigisift}/alerts/{quoted}/clip"
        try:
            with opener.open(clip, timeout=120) as response:
                (DATA / "clips" / f"{review}.mp4").write_bytes(response.read())
        except urllib.error.HTTPError as error:
            print(f"skipped {review}: clip {error.code}")
            continue
        # The demo has no Frigate behind it, and no operator has judged anything yet.
        kept.append(alert | {"link": "", "verdict": None, "verdict_by": None, "verdict_at": None})
    sites = _get(opener, f"{args.vigisift}/sites")
    devices = _get(opener, f"{args.vigisift}/devices")
    me = {"id": 0, "username": "demo", "display_name": "Demo", "role": "operator"}
    recorded = {"me": me, "sites": sites, "devices": devices, "alerts": kept}
    (DATA / "demo-data.json").write_text(json.dumps(recorded, ensure_ascii=False, indent=2))
    print(f"recorded {len(kept)} alerts with clips → {DATA.relative_to(ROOT)}")


def drop(args: argparse.Namespace) -> None:
    recorded_path = DATA / "demo-data.json"
    recorded = json.loads(recorded_path.read_text())
    unknown = set(args.review_ids) - {a["review_id"] for a in recorded["alerts"]}
    if unknown:
        sys.exit(f"not recorded: {', '.join(sorted(unknown))}")
    recorded["alerts"] = [a for a in recorded["alerts"] if a["review_id"] not in args.review_ids]
    for review in args.review_ids:
        (DATA / "clips" / f"{review}.mp4").unlink(missing_ok=True)
    recorded_path.write_text(json.dumps(recorded, ensure_ascii=False, indent=2))
    print(f"dropped {len(args.review_ids)}, {len(recorded['alerts'])} alerts left")


def build(_: argparse.Namespace) -> None:
    recorded = DATA / "demo-data.json"
    if not recorded.exists():
        sys.exit("nothing recorded yet: run `demo_site.py capture` first")
    page = (WEB / "index.html").read_text()
    if page.count("<body>") != 1 or page.count("<script>") != 1:
        sys.exit("index.html changed shape: update where the demo injects its script")
    page = page.replace("<body>", "<body>\n" + BANNER, 1)
    page = page.replace("<script>", '<script src="demo.js"></script>\n<script>', 1)
    # Start clean, but keep `.vercel`: it links the folder to its Vercel project.
    SITE.mkdir(parents=True, exist_ok=True)
    for entry in SITE.iterdir():
        if entry.name == ".vercel":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    (SITE / "index.html").write_text(page)
    shutil.copy(SHIM, SITE / "demo.js")
    shutil.copytree(WEB / "fonts", SITE / "fonts")
    shutil.copy(recorded, SITE / "demo-data.json")
    shutil.copytree(DATA / "clips", SITE / "clips")
    size = sum(
        f.stat().st_size for f in SITE.rglob("*") if f.is_file() and ".vercel" not in f.parts
    )
    print(f"built {SITE.relative_to(ROOT)}, {size / 1e6:.1f} MB")


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
sub = parser.add_subparsers(dest="command", required=True)
p_capture = sub.add_parser("capture")
p_capture.add_argument("--vigisift", default="http://127.0.0.1:8000")
p_drop = sub.add_parser("drop")
p_drop.add_argument("review_ids", nargs="+")
sub.add_parser("build")

if __name__ == "__main__":
    parsed = parser.parse_args()
    {"capture": capture, "drop": drop, "build": build}[parsed.command](parsed)
