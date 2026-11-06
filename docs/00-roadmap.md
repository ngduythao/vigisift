# Roadmap: from VigiSift to an assistant that filters suspicious behavior on CCTV

Context: CCTV monitoring centres still watch largely by eye, and each site needs to watch for a
different kind of "suspicious": at a bakery warehouse, staff taking money or eating stock; at a
watch store, customers stealing. Watching cameras for a whole shift is both boring and easy to
miss things.

Goal: **AI that filters for you**. It marks clips worth watching, and the operator decides. The work
goes stage by stage, each with done criteria and measurements.

**Don't rewrite what others already do well and run for free.** The comparison table is in
[`02-open-source.md`](02-open-source.md). In short: Frigate handles cameras, detection, recording
and the VLM's scene summaries; VigiSift handles what a multi-site monitoring center needs.

## 0. What exists

The foundation stage is done:

| Area | Already in VigiSift |
|---|---|
| Video | MediaMTX, an FFmpeg fake camera, viewing over WebRTC/HLS, the server pulling a frame from RTSP |
| MQTT | auth + ACL, Last Will, QoS 1 + ack + timeout, shared subscription |
| Redis | presence, `SET NX EX` cooldown, token budget, WebSocket fanout over pub/sub |
| LLM | a neutral `ChatModel` (Ollama, Claude), retry, cost ledger, 2 model tiers, an image eval harness |
| Agent, MCP | a hand-written tool loop, commands needing human confirmation, the same toolbox over MCP |
| Operations | Dockerfile, full-stack compose, CI, Dependabot |

Still missing for the new problem, and who handles it:

| Missing | Who handles it |
|---|---|
| Real detection on the video stream (currently fake labels from `simulate_device.py`) | Frigate |
| Recording, clips, playback | Frigate |
| A VLM viewing many frames instead of 1 image | Frigate review summaries |
| Suspicious rules per site | VigiSift generates, Frigate uses |
| Gathering alerts across sites, operators judging them | VigiSift |
| An eval set for behavior: how much it catches, how often it's wrong | VigiSift |
| `redis:7.4` is no longer open source (still free) | Valkey, also free and fully open |

## 1. Target architecture

```text
Sample video (MNNIT, Intel, self-shot) ───FFmpeg──> MediaMTX (fake camera, RTSP)
                                                        │
                                                        ▼
                                              Frigate (one per site)
                                   motion → object detection → zones, recording, Review page
                                   review summary: Ollama/llama.cpp + Qwen, up to 20 frames
                                                        │
                                   MQTT frigate/reviews, frigate/<zone>/person
                                                        │
                                                        ▼
                                                VigiSift (FastAPI)
                       ┌──────────────┬─────────────────┼─────────────────┐
                    Postgres        Valkey          Per-site rules     Review queue
                  (alerts, reviews,  (cooldown,      → activity_context  (operators judge
                   site profiles)   budget)           _prompt camera)    verdict → eval labels)
```

Rules not to break (kept from before):

- Business logic lives in `domain/` and `features/`, without importing paho, redis or LLM SDKs.
  Infrastructure goes through Protocols in `ports/`, tests run on fakes; `tests/test_architecture.py` blocks
  wrong-way imports. Frigate too is just an adapter behind a port.
- LLM output is untrusted data: validate with a schema before storing or alerting.
  The same goes for results from Frigate.
- Every LLM call has limits: timeout, retry count, a daily token ceiling.
- Don't send every frame to the VLM. Send only on a trigger and past the cost policy.

New rules for the CCTV problem:

- **AI suggests, the operator decides.** "Suspicious" is only a reason for an operator to review a clip, not a
  conclusion about a person. The system takes no action on anyone it marks.
- **Describe only visible actions.** Don't guess identity or intent or use appearance as a
  signal. Don't turn on Frigate's face recognition.
- **Measure false alarms before boasting about catches.** A system alerting 200 times a shift is worse than watching yourself.
  Every change to a prompt, model or rule must rerun the eval.
- **No monthly service fees on the default path.** Everything can be self-hosted;
  paid things (Claude API, Frigate+) are only options.
- **No workplace video without written consent.** Use public or
  self-recorded data. Video files aren't committed to git.

## 2. Stages

Each stage: code, run the eval, record it in the log in §4.

### Stage 0: Clean up the stack

- [x] Change `redis:7.4-alpine` to Valkey 9.1.2 in `compose.yml`, `Makefile` and CI; the `redis`
      library and `REDIS_URL` stay.
- [x] README and docs state clearly: the default path runs locally (Ollama), Claude is optional.

Done when: `make check` and CI are green on Valkey.

### Stage A: Bring Frigate into the stack

Do:

- [x] Add Frigate 0.18.2 to `compose.yml`, configured in `deploy/frigate/config/`. CPU detector
      by default; notes on enabling `apple-silicon-detector` on a Mac.
- [x] A fake camera looping **real video** through MediaMTX (`deploy/mediamtx/fake-camera.sh`,
      sample files in `.gitignore`, downloaded by `scripts/fetch_samples.sh` with sha256 checks), with 60
      quiet seconds so reviews can end.
- [x] One Mosquitto user per site (`frigate-<site>`), an ACL allowing writes only under `frigate/<site>/#`;
      VigiSift only reads `frigate/+/reviews`. A real broker test pins both directions.
- [x] Review summaries through Ollama (now `qwen3-vl:8b-instruct`, see log C8–C9),
      `image_source: recordings`, 8 frames, temperature 0, real context 16,384; ~65 seconds per
      summary.
- [x] VigiSift receives `frigate/<site>/reviews`, validates, merges independent of order, pushes to the
      WebSocket; a reconcile loop re-reads the Frigate API to recover missed messages.
- [x] Remove the old path: fake motion labels, `FfmpegSnapshotSource`, the one-image VLM, `/cameras`.
- [ ] `get_snapshot` for the assistant through Frigate's snapshot API: not done, left until needed.

Done when: playing a sample clip → Frigate creates a review alert with a summary → VigiSift stores and
pushes it to the WebSocket; with tests on fakes and an integration test against real Frigate.

### Stage B: Rules per site

- [x] Per-camera rules in Postgres (`camera_rules`): what the camera sees, what counts as
      normal, suspicious, critical, plus concerns. Ready templates for a store, a bakery warehouse, a watch store.
- [x] VigiSift builds `activity_context_prompt` in Frigate's Level 0/1/2 frame, always
      ending with a fixed rule (not based on appearance), then pushes through `PUT /api/config/set`
      with the camera's `update_topic`: takes effect immediately, with no restart.
- [x] With Frigate off the rules stay saved and reconcile pushes again; if someone edits Frigate's config,
      reconcile detects the drift and pushes again.
- [ ] Per-camera zones (cash drawer, shelves) and `loitering_time`: not done, needs a real camera to draw.

Done when: adding a new site only needs new rules, with no code change. ✔

### Stage C: An eval set measuring the whole system

Do:

- [x] 40 labelled clips (20 theft, 20 normal, randomly picked with a seed) from "Shoplifting
      Dataset (2022)" by MNNIT on Mendeley, CC BY 4.0. Chosen over UCF-Crime for its clear
      license (UCF-Crime is research-only) and no account needed to download. The manifest with sha256 is in
      `evals/shoplifting/`; videos aren't committed.
- [x] `scripts/eval_alerts.py`: joins clips with quiet gaps, plays them through the fake camera `eval_cam`,
      gets alerts from the VigiSift API, scores recall, precision, false alarms per hour of normal
      video, summary delay. Results in `docs/03-eval.md`.
- [ ] A measured comparison: `qwen3-vl` vs Qwen3.5 (through llama.cpp), `preview` vs `recordings`,
      with and without per-site rules.

Done when: there's a table of numbers from before and after at least one rule change, in the log.

### Stage D: A review queue for operators

Do:

- [x] An alert page gathered from several Frigates: clip (link to Frigate), `title`, `scene`,
      suspicion level; operators click **Suspicious / Normal / Unclear** and add a note.
- [x] The `alert_verdicts` table stores verdicts; export as labels for the eval set (the feedback loop).
- [ ] Group duplicate alerts (same camera, within N minutes) into one incident; sort by suspicion level.
- [x] Measurements: correct rate per camera and per site; time to handle an alert.

Done when: operators handle alerts from the UI, and review labels run in the eval.

### Stage E: Store numbers (a basic feature)

Detecting theft and suspicious behavior is the core feature; counting customers is what every store
needs. A store manager's three questions:

1. How many customers came today, and what time was busiest?
2. How long after a customer enters does staff come to help?
3. How many customers came and left with nobody helping them (missed customers)?

Frigate does the hard part; VigiSift gathers and calculates:

- **Tracking each person**: Frigate pushes each person's lifecycle to `frigate/<site>/events` (id,
  `current_zones`, `entered_zones`, entry time, exit time, `sub_label`). Checked in the 0.18.2 source
  (`track/object_processing.py`). VigiSift receives, merges independent of order and recovers
  through `GET /api/events`, the same way as reviews.
- **Staff or customer**: use Frigate 0.18's *custom object classification* (`sub_label`
  type): train a small model on person crops from the camera, with two classes `staff` and
  `customer` (a uniform is the easiest signal). Write no model ourselves; no face recognition
  for customers.

Do:

- [ ] E1. Receive `frigate/<site>/events` (the ACL gains read access), a table of tracked people and
      time spent in each zone; recover like reviews.
- [ ] E2. Count customers: non-`staff` people entering the camera's door zone (the zone name configured
      per camera), by site, by day and by hour.
- [ ] E3. Time to greet: the first moment a `staff` is in the same service zone as the customer for
      N seconds; a customer leaving before that is missed.
- [ ] E4. A numbers page per site: customers today, by hour, time to greet (median),
      missed customers.
- [ ] E5. Eval: hand-count on sample video as ground truth, measure the counting error; measure the time-to-greet
      error on self-recorded clips with staff.

Done when: customer counts are within 10% of a hand count on sample video; there's a measurement for
time to greet, with how it's measured.

### Stage F: Behavior signals before the VLM

In Paza's tiered approach: a cheap tier runs continuously, the VLM runs only on a signal.

- [ ] Use signals Frigate already has: loitering in a zone, entering a zone off-hours, counting people.
- [ ] If the eval shows a need: add a pose signal (hand from shelf to pocket or mouth) with an open source
      pose model, running as a filter before the review summary.

Done when: VLM calls per hour fall with no fall in recall.

### Stage G: Fine-tuning, only when the numbers demand it

Entry condition: after D and F, one specific behavior still gives many false alarms, and there are a few hundred
labelled clips from reviews.

- [ ] Try a small action classification model on clips or pose sequences (PoseLift), compared on
      the **same** eval set; keep it only if it clearly wins on both false alarms and cost.

## 3. Not doing

- Don't rewrite what Frigate already does: object detection, tracking, recording, frame selection
  for the VLM.
- No face recognition or identifying people.
- No Frigate+ or any paid service on the default path.
- No connecting to workplace cameras or using workplace video without written consent.
- No Kubernetes; Docker Compose is enough.
- No model training before there's an eval set and numbers showing prompts aren't enough.

## 4. Log

| Stage | Done | Commands/tests run | Notes |
|---|---|---|---|
| 0 | Valkey 9.1.2 replaces `redis:7.4` in compose, `Makefile`, CI | ruff, mypy, 120 tests on real Valkey; `docker compose up --build` with the full stack, seed, `GET /devices` 200 | No code changes needed: Valkey reports `redis_version:7.2.4` to clients |
| A | Frigate + fake camera + Ollama in compose; `camera_alerts` merging independent of order; reconcile through the API; per-site ACL; a web page with a Camera alerts panel | 135 tests (added 24 permutations of built payloads, 120 permutations of real payloads recorded from Frigate, a real broker for the ACL); real run: every Frigate review reached VigiSift (14/14, then 7/7), summaries arrived over MQTT with exact times; `api` stopped for 11 minutes → back on, reconcile recovered all 6/6 reviews (with 2 summaries) in under 12 seconds; a fake alert sent with another site's user and with VigiSift → the broker blocked it, 0 rows got in | Caught in the real run: `detect` is OFF by default in 0.18; without `version` Frigate "migrates" the config into a broken state; Frigate estimates ~330 tokens/image but `qwen3-vl` on Ollama uses ~1,050 (measured), so requests always overflow context; the VLM timeout is a hard 120 seconds, and `qwen3-vl:4b` thinks by default (can't be turned off through Ollama's OpenAI endpoint): Frigate's real requests take 80–120 seconds even with 6 frames, so only about half of reviews get a summary on an M4 Pro; tests once deleted dev data by sharing a database |
| A+ | Fast summaries: back to the `ollama` provider (sends `think: false`), split `num_ctx` so Frigate computes 6 frames while Ollama gets a 16,384 context; removed the Modelfile alias | Measured per request in Ollama's log: through the OpenAI endpoint 80–120 seconds and ~50% of reviews time out; `ollama` provider with 10 frames: 6/6 summarised but 80–110 seconds; 6 frames: 3/3 full reviews summarised, 57–60 seconds per request, 0 errors | No new downloads. Measured apart: ~3.5 seconds per frame (300 tokens/second) + ~25 seconds generating JSON. Capacity ≈ 1 summary/minute/machine |
| B, C, D | Per-camera rules pushed to Frigate and re-pushed on drift; operator sign-in, review queue, verdict metrics, label export; whole-system eval on 40 labelled clips | ruff, mypy, pytest (real Postgres, Valkey, Mosquitto); pushed real rules to Frigate then restored an old config.yml → VigiSift detected the drift and pushed again; eval on 57.8 minutes of video through real Frigate | Eval (`docs/03-eval.md`): recall 15%, 0 false alarms per hour, summary delay 116 seconds. 17/29 alerts had no summary because the `qwen3-vl:4b` tag is now the Thinking build (can't be turned off), on 11/40 clips Frigate saw no person. Two independent reviews ran in parallel; every fault found was fixed with a test |
| C+ | Moved the summary model to `qwen3-vl:4b-instruct` | second 40-clip eval through real Frigate | 29/29 alerts summarised, 0 errors, median delay 81 seconds (run 1: 12/29, 116 seconds). But recall 0%: the model describes the goods-in-bag scene yet still scores level 0. Next: the scoring step (prompt, 8B model) measured on new clips |
| C++ | VigiSift re-checks summaries against camera rules (JSON schema enum, level derived from the indicator); split dev/test sets; a harness that quickly measures the scoring step | offline on dev: Frigate's own scoring 0/20, re-check 7/20, 0 false alarms; test set through real Frigate | Test set: 0/20, 0 false alarms, 26/26 summarised. The bottleneck is the detection layer: 14/40 clips had no alert, reviews covered only ~49% of a clip. Next: YOLOv9 on the Neural Engine |
| C+++ | Found the real cause of low coverage: the eval video joins clips with a grey screen, and Frigate treats each scene change as lightning and skips detection while recalibrating the background. The eval camera sets `lightning_threshold: 1.0`; measured the YOLOv9 detector on the Neural Engine | 4 runs × 12 dev clips through real Frigate, CPU from `docker stats` | Clips with no alert 3/12 → 0/12, coverage 35% → 87%, with both detectors. Neural Engine: CPU ~99% → ~43% of a core. Next: measure recall on a new test set |
| C4 | Measured the whole system again on the test2 set (seed 9, no overlap with dev/test) with the fix and the Neural Engine detector | 40-clip eval through real Frigate, 57.9 minutes | Recall 0/20 → 3/20, 0 false alarms; 38/40 clips had alerts, coverage 88%. The bottleneck moves to the 4B VLM's description step. Next: frames/model, measured on dev |
| C5 | Offline harness with `--frames`; tried 6/10 frames and person crops on dev | 4 runs × 40 dev clips | 6 frames 4/20 (previously 7/20), person crops 5/20, 10 frames 7/20, 0 false alarms. Randomness (temperature 0.5) is larger than the differences: only 2 clips were caught in all 4 runs. Next: measure with temperature 0 |
| C6 | Measured 6 and 10 frames again with temperature 0 (stable descriptions) on dev | 2 runs × 40 dev clips, ~1 hour of GPU | 6 frames 2/20, 10 frames 8/20 (1 false alarm). 10 frames catch every clip 6 frames catch, plus 6 more: 10 frames really are better. Temperature 0 alone doesn't raise recall. Requests 64 seconds offline, longest 78. Next: set 10 frames + temperature 0 in Frigate, measure on the test3 set |
| C7 | Set Frigate to 10 frames + temperature 0, measured the whole system on the test3 set (seed 10) | 40-clip eval through real Frigate, 58 minutes; plus 1 offline run on test3 | 3/20, 0 false alarms, 40/40 clips summarised, no timeouts. Offline on test3 is also 3/20: the path through Frigate isn't what's broken; 8/20 on dev is the number from the set used for fixing. The bottleneck is the 4B model's descriptions. Next needs approval: the 8B model or a "needs a look" level |
| C8 | Checked frames by eye (shuffled, names hidden) and tried the 8B model on test3, then measured 8B through Frigate on test4 | 1 offline 40-clip run, 1 eval of 20 clips through Frigate | The frames clearly show 10/20 theft clips, the 4B model catches only 3: the bottleneck is the model. 8B offline 7/20 (9/20 when re-checking with 8B), 0 false alarms, 86 seconds per request. Through Frigate: 4/8 summarised theft clips, 1 false alarm, but 7/20 reviews timed out because the machine keeps up with only ~35 alerts/hour. Next: choose between fewer frames, a counter zone, or a stronger machine |
| C9 | 8B with 8 frames: offline on test3, then through real Frigate on test3 and test4 | 1 offline run, 2 evals through Frigate (60 clips) | 8 frames catch as much as 10 (7/20 offline) and run 25% faster. Through Frigate: 13/30 theft clips (43%), 1/30 false alarms, 59/60 summarised, 0 timeouts, a stable ~105-second delay. Meets the goal of ≥ 8/20, false alarms ≤ 1/20. Next: tighten the "looking around" rule on dev |
| D+ | Console redesign: a collapsible sidebar with sections and a site filter, the queue beside the selected alert (clip next to the AI details), History as a full-width table, keyboard shortcuts, one pane at a time on phones, a Frappe-inspired look with bundled Inter. Moved everything in the repo to English | ruff, mypy, pytest; driven in a real browser at 2000x1000, 1280x800 and 375x812, in dark and light | The clip still opens in Frigate: playing it inline needs a backend route to fetch it from Frigate. The queue shows `50+` when a page is full; an exact count needs an API change |
| D++ | The clip plays in the console: `GET /alerts/{site}/{review}/clip` fetches it from the site's Frigate, so operators need no Frigate account | ruff, mypy, pytest with a fake Frigate; a real Frigate alert played and scrubbed in Chrome, an old alert and a stopped Frigate each showing their message | Frigate streams clips as fragmented MP4 with no length or byte ranges, so a `<video>` reading the stream can't seek (it saw 5 seconds of a 62-second clip). The page downloads the whole clip first; a very long review costs memory and a wait. Not tried in Safari, and Frigate's docs warn it doesn't handle this format reliably |
| C10 | Free improvements before buying a machine: re-measured after the stutter fix, then tried a tighter "looking around" rule (docs/03-eval.md run 14). Also fixed two identifiers the rename had capitalized: the MediaMTX API user and `POSTGRES_DB` | 2 offline runs on dev, 4 evals through Frigate (test3 and test4, old and tighter rules) | The stutter fix left the total at 13/30, 1/30 false alarms. The tighter rule got 8/30, 1/30 false alarms, so rules v2 stay. A second active camera (~74 alerts per hour) left about half the alerts without a summary. Next: measure on real video recorded with consent, before buying a machine |
| R1 | Computed the effective alert level in one place: a stored generated column and an `alert_list` view behind every screen; `camera_alerts` read and written only by the alerts module; dropped an index no query used | ruff, mypy, 244 tests; 4 instances applying the schema to an empty database at once | Found by auditing the code for things that must change together. The concurrency test failed 3/3 without the new advisory lock |
| D+++ | Alert detail: clip, AI summary, matched rule and verdict bar in the left column, scene and observations on the right; long lists show 3 items with "Show all" | ruff, mypy, pytest; headless Chrome at 1600x900, 1280x800 and 375x812 on sample alerts | Before, the clip left an empty column under it while the details beside it needed scrolling |
| D+++ fix | Frigate gets the host's time zone (`TZ` in compose, filled in by `make`) | A real demo summary before and after | Frigate writes the alert's local time into the prompt; on UTC a 9:30 AM alert in UTC+7 reached the model as 2:30 AM, and the prompt is warier at night |
| Demo | A static live demo at vigisift-demo.vercel.app: the real console page with its API answered in the browser (`demo/demo.js`) from 8 recorded alerts and their clips | `scripts/demo_site.py capture` against a clean stack, headless Chrome on the local build and the deployed site | Recorded through real Frigate with `qwen3-vl:8b-instruct`: 2 of 4 staged thefts flagged, 0 of 2 normal clips. The missed thefts stay in, so the demo shows the AI as it is |
| Demo fix | The demo keeps only the 4 alerts whose AI text is right: 2 normal staged clips and 2 store-aisle clips | Each clip's frames read against its dataset label | The model described all 4 staged thefts wrongly (into the bag read as out of it, a pocketed item missed), 2 of them flagged for the wrong reason. Frigate asks at temperature 0, so a rerun gives the same text |

### Foundation stage log

| Day | Done | Commands/tests run | Notes |
|---|---|---|---|
| 1 | MediaMTX + fake camera (testsrc2), FFmpeg `SnapshotSource`, motion carrying only labels, `/cameras`, `/cameras/{id}/snapshot` | `make check`; an integration test capturing a real frame from RTSP; e2e motion → frame → stub VLM → DB | WebRTC video confirmed in the browser (readyState 4, 1280x720). WebRTC/HLS latency not yet measured with a clock |
| 2 | Mosquitto auth + ACL, retained Last Will, QoS 1 + ack + overdue sweep, shared subscription, resubscribing on reconnect | Integration tests on a real broker: wrong password, ACL blocking spoofing, Last Will | The ACL test was verified to catch a loosened ACL. e2e: command ACKED, `kill -9` → OFFLINE |
| 3 | Presence sorted set, `SET NX EX` cooldown, token budget, WebSocket fanout over Redis pub/sub | 20 concurrent events → exactly 1 VLM call; a 2-instance test | e2e with 2 real instances: 6 telemetry messages stored exactly 6 times, the WS on A got all 6 |
| 4 | Retry (429/5xx/529/timeout, retry-after), `llm_calls` cost ledger, `/llm/usage`, low effort, cache breakpoint, 2 model tiers, a 20-scene eval harness | Tests for retry, stop_reason, cascade, cost, budget | Caught a fault: 529 isn't under InternalServerError in SDK 1.x. The eval NOT yet run and cache not measured with the real API (costs money) |
| 5 | A hand-written agent tool loop, SSE streaming, Pydantic tool validation, one-time confirmation (GETDEL), server-side fallback | 16 tests with a fake client keeping the SDK's shape | NOT yet run with the real API |
| 6 | MCP server sharing Toolbox, demo page, Dockerfile, full-stack compose, CI + Dependabot, English README | MCP over real stdio; `docker compose up` full stack; actionlint | Demo page checked in the browser: click OFF → ACKED in realtime |
| 5+ | Running local and free: a neutral `ChatModel` with 2 adapters (Ollama, Anthropic); `qwen3-vl:8b` replacing llava for both vision and agent; separating `online` (MQTT heartbeat) from `streaming` (MediaMTX has video); a real webcam publishing from Chrome over WHIP | 115 tests; the real agent answers in Vietnamese, calls `get_snapshot` itself and recognises the test pattern; commands only create a confirmation, 0 commands sent | Local vision ~1 s/image. Agent ~35 s/question because of the thinking variant (~400 hidden tokens/turn). Ollama 0.34 + qwen3-vl quirk: thinking off with `format` puts the answer in the `thinking` field |
