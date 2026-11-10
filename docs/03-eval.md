# Whole-system eval: camera → Frigate → VLM → VigiSift

Measures what operators really receive: which alerts arrive, which the AI scores "suspicious",
and whether that's right. It doesn't measure a prompt or a model alone, because a fault can be in any
layer: Frigate doesn't see a person, the VLM times out, the VLM sees it and scores it wrong.

## Data

"Shoplifting Dataset (2022)", MNNIT, Mendeley Data,
[doi:10.17632/r3yjf35hzr](https://doi.org/10.17632/r3yjf35hzr),
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Actors in a staged store,
one fixed camera looking down at a counter.

- Four sets of 40 clips (20 theft, 20 normal), with no shared clips, lists with sha256 in
  `evals/shoplifting/`, videos not committed:
  - **dev** (`dev.jsonl`, seed 7): for finding faults and fixing them. Runs 1–3 and 5 used this set.
  - **test** (`test.jsonl`, seed 8): for reporting only, after a fix is settled (run 4).
    Used once, so not reused for reporting.
  - **test2** (`test2.jsonl`, seed 9): the reporting set after fixing the detection layer (run 6).
  - **test3** (`test3.jsonl`, seed 10): the reporting set after moving to 10 frames and temperature 0
    (run 9).
  - **test4** (`test4.jsonl`, seed 11, 10 theft and 10 normal): the reporting set after moving to
    the 8B model (run 11). 2 theft clips remain unused; later runs must reuse old sets.
- The eval camera's "suspicious" rules live with the dataset: `evals/shoplifting/rules.json`.
- This is staged data, not real CCTV: one scene, one camera angle, clear actions.
  These numbers are for **comparing configurations with each other**, not real-world accuracy.

## How to run

```bash
# Keep eval_cam the only camera creating alerts (see run 14): set samples/shop-aisle.mp4 aside.
uv run python scripts/eval_alerts.py prepare --zip samples/eval/Dataset.zip --split test4 --seed 11
VIGISIFT_USER=admin VIGISIFT_PASSWORD=... uv run python scripts/eval_alerts.py rules
docker compose restart mediamtx
uv run python scripts/eval_alerts.py mark
# wait for the video to end plus a few minutes
VIGISIFT_USER=admin VIGISIFT_PASSWORD=... uv run python scripts/eval_alerts.py score
```

- `prepare` normalises each clip (640x480, 15 fps) and joins them into one long video, with
  75 quiet seconds between clips. The gap must exceed how long Frigate keeps a review open after
  the person disappears (`cutoff_time` 40 seconds plus a few tracking seconds), so each clip gets its own review.
- The video plays through the fake camera `eval_cam` (MediaMTX → RTSP) into real Frigate, with "suspicious" rules
  for a counter set from VigiSift. Summaries come from a VLM on Ollama (M4 Pro);
  each run's model is noted in its results.
- `mark` records the playback start. `score` fetches `eval_cam` alerts in the playback window through
  the VigiSift API (signed in as an operator), so the eval takes the path operators use. It scores
  by **effective level** (the higher of Frigate's level and VigiSift's re-check by rules), and
  also prints the number "with Frigate's own level only".

### Quickly measuring only the scoring step (offline)

`scripts/eval_grading.py` sends Ollama exactly what Frigate 0.18.2 sends per review
(Frigate's prompt, 8 evenly spread frames, the same schema and parameters), then runs VigiSift's
re-check. A 40-clip run on the 8B model takes ~45 minutes rather than an hour, so many fixes can be tried. The only difference from
the real system: frames are spread over the **whole clip**, while Frigate takes only the span where it
still tracks a person. So offline numbers are the **ceiling** for the look-and-score step, with perfect
detection.

## How it's scored

- Each alert belongs to the clip playing when the alert **started** (with 10 seconds at both ends for detection
  delay). Alerts still open when the next clip has started are printed as warnings: if any exist, the gap
  is too short and the numbers can't be trusted.
- A clip is "flagged by AI" when at least one of its alerts scored level ≥ 1.
- No alert, or an alert with no summary, both count as the AI not flagging: the operator
  sees nothing, whatever the reason. The "outcome" column separates the two to show which layer failed.
- False alarms per hour: normal clips flagged, divided by hours of normal video. This number
  decides whether operators turn the system off.
- Summary delay: from the review closing to VigiSift receiving the first summary
  (`summarised_at`, unchanged when a message arrives again).

## Run 1: `qwen3-vl:4b` (Thinking build), 2026-10-31

Config: Frigate 0.18.2, CPU detector, `eval_cam` 640x480 5 fps; `qwen3-vl:4b` summaries on
Ollama 0.40.13 (M4 Pro), 6 frames from recordings, `num_predict` 2048; "counter" rules set from
VigiSift. Joined video 57.8 minutes. Per-clip details: `evals/results/` (not committed).

| Metric | Result |
|---|---|
| Clips | 40 (20 theft, 20 normal) |
| Caught (recall) | **15%** (3/20) |
| Correct flags (precision) | 100% (3/3) |
| Normal clips falsely flagged, per hour of video | **0** |
| Summary delay after the review closes (median) | 116 seconds |
| Alerts spilling into the next clip | 0 |

Which layer failed (one outcome per clip):

| | No alert | Alert, no summary | Summary, level 0 | Summary, level ≥ 1 |
|---|---|---|---|---|
| Theft clips (20) | 7 | 8 | 2 | 3 |
| Normal clips (20) | 4 | 9 | 7 | 0 |

Reading the table:

- **The VLM layer is the main bottleneck.** 17/29 alerts had no summary: 9 hit Frigate's hard 120-second
  timeout, 8 returned empty after hitting the token ceiling, 1 had broken JSON. The cause: the `qwen3-vl:4b` tag
  is now the **Thinking** build; `think: false` can't turn thinking off (measured directly: it still
  thinks 250–1,000 tokens), so a request runs nearly as long as the timeout.
- **When the VLM can answer it scores fairly well**: 3/5 summarised theft clips flagged, 0/7 normal
  clips falsely flagged. Too small a sample to conclude, but it shows fixing the VLM layer is the thing
  to do first, without touching the rules.
- **11/40 clips had no alert**: Frigate didn't see a person long enough in a 640x480 frame
  with the CPU detector. That's for the detection layer (resolution, `min_score`, detector), not
  the VLM.
- **0 false alarms per hour** sounds nice, but here it's mostly because the AI rarely could answer, not because
  it discriminates well. Read it together with recall.

Two earlier runs were discarded: in the first `num_predict` 1024 made every summary empty (thinking ate
the whole ceiling), and the second changed config midway. The eval itself caught that the machine's model had
been replaced with the Thinking build: unit tests and trying a few reviews didn't show it.

## Run 2: moving to `qwen3-vl:4b-instruct`, 2026-10-31

The same 40 clips, rules and Frigate; only the model changed (and the token ceiling back to 1024).

| Metric | Thinking (run 1) | Instruct (run 2) |
|---|---|---|
| Alerts with a summary | 12/29 | **29/29** |
| Timeout / empty / broken JSON | 9 / 8 / 1 | **0 / 0 / 0** |
| Summary delay (median) | 116 seconds | **81 seconds** (71–110) |
| Caught (recall) | 15% (3/20) | **0%** (0/20) |
| False alarms per hour | 0 | 0 |
| Clips with no alert | 11 | 11 |

Reading the table:

- **The pipeline is clean now**: every alert has a Vietnamese summary within about 1–2 minutes, with no
  faults left. Run 1's VLM layer faults really were from the Thinking build.
- **But the instruct build never scores suspicious**: all 29 alerts are level 0, confidence
  0.95. It still **sees** the behavior in some theft clips ("put a small object into a hand
  bag", "put some objects into a handbag", "put a hand in a trouser pocket"), though the rules state that's level 1.
  The fault is now in the scoring step, not in looking.
- Suspicion: the 4B model clings to the fixed sentence at the end of the prompt ("if unsure, score level 0"), and it
  reads the staged scene as an "office", not a store.
- The 11 clips with no alert match run 1 exactly: the detection layer is stable and needs its own fix.

Lesson: a nice number (0 false alarms) may only mean the system never alerts. Read it
together with recall, and split results by layer.

## Run 3: fixing the scoring step, measured offline on the dev set

The same 40 dev clips, `qwen3-vl:4b-instruct`, measured with `eval_grading.py`. One change each time.

| Try | Frigate's own score, theft flagged | False alarms |
|---|---|---|
| Baseline: rules v1, old guidance ("if unsure, level 0"), answers in Vietnamese | 2/20 | 0/20 |
| New guidance ("a level 1 indicator means at least level 1") | 4/20 | 0/20 |
| Rules v2 (stating "putting it in **their own** bag, even calmly") + English | 1/20 | 0/20 |

With 20 clips each clip is 5%, and the model runs at temperature 0.5: differences of 1–4 clips are mostly
noise. Conclusion: **prompt fixes don't change how the 4B model scores.** It still writes "places items from
the table into their backpack" and scores level 0, even with rules stating exactly that behavior.

So split the two jobs: the VLM describes, and VigiSift **re-checks from text**: asks the same model "which
indicator in the rules does this description match?", with the answer constrained by a JSON schema `enum` to the rule
indicators, and code deriving the level from where that indicator sits (`features/rules/check.py`).

| Scoring method (rules v2, same descriptions) | Theft flagged | False alarms | Time |
|---|---|---|---|
| Frigate's own score | 0/20 | 0/20 | ~40 seconds (with the looking step) |
| **VigiSift re-check** | **7/20 (35%)** | **0/20** | ~1 second |
| VigiSift re-check, rules v1 | 4/20 | 0/20 | ~1 second |

How rules are written really matters at the re-check (v1 → v2: 4 → 7), so rules v2 stay.
The summary's language doesn't: re-checking the second try's Vietnamese summaries (rules
v2, written in English) gave 8/20, 0 false alarms, level with 7/20 for English summaries.

## Run 4: test set, through real Frigate, 2026-10-31

The config settled after run 3: `qwen3-vl:4b-instruct`, the new guidance, rules v2, English
summaries, VigiSift re-check. 40 **never used** clips, video 57.9 minutes.

| Metric | Result |
|---|---|
| Caught (recall), effective level | **0%** (0/20) |
| ... with Frigate's own level only | 0% (0/20) |
| Normal clips falsely flagged | 0/20 |
| Alerts with a summary | 26/26; timeout, empty, broken JSON: 0 |
| Summary delay (median) | 76 seconds; re-check done 0–5 seconds after |

| | No alert | Summary, level 0 | Level ≥ 1 |
|---|---|---|---|
| Theft clips (20) | 8 | 12 | 0 |
| Normal clips (20) | 6 | 14 | 0 |

Reading the table: **on the real system the bottleneck is the person detection layer, not the AI layer.**

- On 14/40 clips Frigate created no alert.
- On clips with alerts, the review lasted a **median 6 seconds of an 11-second clip** (covering ~49% of it),
  so the 6 frames the VLM got often missed the moment goods went into a bag. 11/12 theft clip summaries
  only described "looking at goods and putting them back". At the time I guessed the CPU detector lost track of the person; **a wrong guess**,
  the real cause was how the eval video was built (run 5).
- Run 3 reached 35% because frames covered the whole clip: that's the ceiling with perfect detection, not the
  system's number.
- One summary wrote "placing it in his pocket **or** adjusting his clothing": an ambiguous sentence, so
  the re-check didn't match. Noted for later, not fixed (a sample of one).

Lesson: a nice offline number (35%) and the system number (0%) differ because offline skips
a layer. Measure the whole real path, on data not used for fixing.

## Run 5: detection layer, the first 12 dev clips, 2026-11-01

Measures only person detection, so VLM summaries were off to lighten the machine. Each pass plays the first 12
dev clips (~17 minutes). Coverage = the share of a clip's time inside a Frigate review. CPU comes
from `docker stats` (100% = one core), sampled every 30 seconds, median.

| Detector | `lightning_threshold` | Clips with no alert | Median coverage | CPU |
|---|---|---|---|---|
| CPU (SSD MobileNet) | 0.8 (default) | 3/12 | 32% | (earlier pass, CPU not measured) |
| YOLOv9-t, Neural Engine | 0,8 | 3/12 | 35% | Frigate 25% + detector 12% |
| YOLOv9-t, Neural Engine | **1,0** | **0/12** | **87%** | Frigate 27% + detector 16% |
| CPU (SSD MobileNet) | **1.0** | **0/12** | **87%** | Frigate 99% (peak 224%) |

**Changing the detector doesn't change coverage; changing how Frigate handles a scene change does.**

Reading tracked objects over time: in pass 2, Frigate only starts seeing a person at second 4–8 of an
11-second clip. Clips 3 and 5 have a large, clear person at the counter from the first frame and Frigate
never sees them. The reason, read in 0.18.2's `frigate/motion/improved_motion.py` and
`frigate/video/detect.py`:

1. The eval video joins clips, separated by a grey screen. Each clip opens with a full
   frame change.
2. When over `lightning_threshold` (default 80%) of the frame changes, Frigate treats it as lightning or a camera
   switching to infrared. It enters background calibration, and **during calibration, motion regions
   aren't sent to detection**.
3. Calibration ends when motion falls under 5%; by then a person standing still is part of the background. Frigate
   sees them only when they move sharply, or never.

Real cameras run continuously and people walk into frame, so they don't hit exactly this. So only the eval
camera sets `lightning_threshold: 1.0` (`deploy/frigate/config.template.yml`); real cameras keep the
default. With the fix, every clip's person is tracked from second 0.3.

About detectors: YOLOv9-t and SSD MobileNet see people equally on this set, but the Neural Engine
lowers total CPU from ~99% to ~43% of a core, and the CPU detector at times jumps to 224%. The
Neural Engine detector stays optional (it needs one more process outside Docker); on a Mac it's worth enabling.

Lesson: in run 4 I blamed the detector before checking from which second Frigate saw a person. Looking at
tracked object times and a few frames first would have shown at once that the fault was in the eval video.

## Run 6: test2 set, through real Frigate, 2026-11-01

Config as in run 4, plus run 5's fix (`lightning_threshold: 1.0` for the eval camera) and
the YOLOv9-t detector on the Neural Engine. 40 **never used** clips (seed 9, no overlap with dev
or test), video 57.9 minutes.

| Metric | Run 4 (test) | **Run 6 (test2)** |
|---|---|---|
| Caught (recall), effective level | 0/20 | **3/20 (15%)** |
| ... with Frigate's own level only | 0/20 | 1/20 |
| Normal clips falsely flagged | 0/20 | **0/20** |
| Clips with no alert | 14/40 | **2/40** |
| Median review coverage | ~49% | **88%** |
| Summary delay (median) | 76 seconds | 77 seconds |
| Frigate CPU (median, including while the VLM runs) | | 29% of a core |

The detection layer is fine now: 38/40 clips have alerts, and reviews cover nearly the whole clip. The bottleneck moves to
the **VLM's description step**. Reading the 20 theft clip summaries:

- 3 describe the goods-in-backpack scene correctly ("placing items from a table into his backpack"); the
  re-check catches all 3, Frigate's own score only 1.
- Most others describe a person who "interacts with a backpack", "inspects a blue backpack", "picking
  up a backpack": the model sees the backpack and counter but doesn't describe goods going into it. With 6
  480p frames over ~11 seconds, that moment often falls between frames or is too small in the frame.
- Some describe it nearly right but the rules don't match: "placing a hand in their pocket" (no mention of
  goods), "exits the frame while carrying [a bag]".

The offline ceiling measured on dev is 7/20 with frames covering the whole clip; this is 3/20 on another set, so it can't
yet be said whether the gap comes from the clips or from the frames Frigate picks.

## Run 7: trying frames, offline on dev, 2026-11-01

`scripts/eval_grading.py`, config as in run 6 (`qwen3-vl:4b-instruct`, rules v2, English
summaries, VigiSift re-check), each pass 40 dev clips, frames spread over the whole clip.

| Frames | Caught (effective level) | False alarms | Time per request |
|---|---|---|---|
| 6 frames, whole scene (run 3) | 7/20 | 0/20 | ~40 seconds |
| 6 frames, whole scene (rerun) | 4/20 | 0/20 | 31 seconds (longest 34) |
| 6 frames, cropped to the person | 5/20 | 0/20 | 32 seconds |
| 10 frames, whole scene | 7/20 | 0/20 | 52 seconds (longest 62) |

Frigate's own score stays 0/20 in every pass; every clip caught is thanks to the re-check.

"Cropped to the person": YOLOv9-t's person box (the detector's model) merged over 11 frames
per clip, widened 12% per side to keep hands and the counter, zoomed about 1.4 times. Frigate has no
such option (review frames are always the whole scene at 480p), so it's worth doing only with a clear win. The script
finding person boxes is a draft and isn't committed.

**Conclusion: the model's randomness is larger than the differences between configs.** Clip by clip:

- With the same 6-frame config, run 3 got 7/20 and the rerun 4/20.
- Only 2 clips were caught in all 4 passes. Over 4 passes, 10/20 theft clips were caught at least once.
- So 10 frames (7/20) or person crops (5/20) are **not yet proven** better. With 20
  theft clips and one pass per config, a 3-clip gap is within the variation.

A diagnostic pass planned on test2 (offline 3/20 or 7/20, to tell whether the fault is the clips or the frames
Frigate picks) hits this same problem, so it hasn't run.

The model "sees" the behavior in about half of theft clips, but each generated description only captures
part of it. Temperature 0.5 is the default Frigate sets for local models, and can be changed in
`runtime_options`.

### Next steps

1. Measure with **temperature 0** (stable descriptions, reruns give the same result): one pass for 6 frames,
   one for 10. Only then does comparing configs mean something. If temperature 0 is also better,
   change it in Frigate too.
2. If 10 frames win: set `num_ctx` so Frigate sends 10 frames. Requests take ~52 seconds offline, and through
   Frigate there's also frame cutting time, so measure whether they pass 120 seconds.
3. A larger model (`qwen3-vl:8b`, a 6.2 GB download): slower, may pass the timeout.
4. The re-check: handle ambiguous sentences ("A or B") and sentences missing the object ("hand in pocket").

Reporting a new number will need yet another test set (test2 has been used once; 62 clips remain unused).

## Run 8: temperature 0, offline on dev, 2026-11-01

Like run 7, but `--temperature 0`: the model always picks the most likely word, so descriptions no longer depend
on a pass's luck. One pass per config, 40 dev clips.

| Frames | Caught (effective level) | False alarms | Time per request |
|---|---|---|---|
| 6 frames | 2/20 | 0/20 | 38 seconds (longest 44) |
| 10 frames | **8/20** | 1/20 | 64 seconds (longest 78) |

Frigate's own score stays 0/20; every clip caught is thanks to the re-check.

Clip by clip: 10 frames catch both clips 6 frames caught, plus 6 more, missing none that 6 frames
caught. Same clips, same model, only the frame count differs, so this 6-clip gap says something:
**10 frames are better than 6.** The temperature 0.5 passes in run 7 also lean this way (10 frames
7/20, 6 frames 4/20, 5/20 and 7/20).

Temperature 0 alone doesn't improve things: 6 frames at temperature 0 got 2/20, below all three
6-frame passes at 0.5. What it brings is stable results for comparing configs. Not yet rerun to confirm
temperature 0 gives exactly the same result.

Details:

- 7/8 caught clips have summaries saying outright that goods go in the backpack ("packs items from a table into his
  backpack"). The other, Shoplifting (27), only describes "selecting an item … while holding a backpack,
  likely preparing to … take something": the re-check was a little lenient matching it to the goods-in-bag
  rule.
- The false alarm on Normal (2): the model writes the person "looks around the room, possibly to check if anyone is
  watching", matching the "looking around to see if anyone notices" rule. The model says "possibly", meaning a guess.
- 10 frames take 64 seconds per request offline, longest 78. Through Frigate there's also time cutting
  frames from recordings, and Frigate cuts hard at 120 seconds, so it must be measured through real Frigate.

### Next steps

1. Set Frigate to send 10 frames (`provider_options.options.num_ctx` ≈ 7400) and `temperature: 0`
   in `runtime_options`, then measure the whole system on a new test set (test3, from the 62 unused
   clips): recall, false alarms, and whether any review passes 120 seconds.
2. The re-check: handle ambiguous and guessing sentences ("possibly", "likely"), to reduce false alarms like
   Normal (2) without missing clips like Shoplifting (27).
3. A larger model (`qwen3-vl:8b`, a 6.2 GB download) if 10 frames stay low.

## Run 9: test3 set, 10 frames and temperature 0, through real Frigate, 2026-11-01

Frigate set as run 8's winner on dev: `num_ctx` 6600 (10 frames for the 4:3 eval camera) and
`temperature: 0`. CPU detector, rules v2, `lightning_threshold: 1.0` for eval_cam. The test3 set had
never been used.

| | Run 6 (test2) | Run 9 (test3) |
|---|---|---|
| Caught (effective level) | 3/20 | **3/20** |
| ... with Frigate's own level only | 0/20 | 1/20 |
| False alarms | 0/20 | 0/20 |
| Clips with an alert and summary | 38/40 | **40/40** |
| Summary delay after the review closes (median) | 77 seconds | 96 seconds |

No review was cut by Frigate at 120 seconds.

**The path through Frigate isn't what's broken.** Running offline on the same test3 set (10 frames,
temperature 0, frames spread over the whole clip) also gave 3/20, 0 false alarms, median request 53 seconds. Through
Frigate or offline both give 3/20, only with different clips: Frigate caught (44), (36), (54); offline caught (36),
(35), (63). The two paths take frames at slightly different times, so descriptions differ.

**8/20 on dev is the number from the set used for fixing the rules.** On never-seen clips, the 4B model describes
the theft behavior in about 15% of clips. Among the 17 clips missed:

- Most descriptions have no goods going into a bag: "holding a backpack and picking up a thermos
  bottle", "inspects an item … before returning it".
- On a few clips the model nearly sees it but hedges, and the re-check deliberately doesn't match: "puts something
  into their bag" (doesn't say it's goods from the counter), "appearing to place or remove something … into
  their backpack", "likely preparing to take one of the items with him". The "suspicious" rules say:
  if you can't clearly see where the goods go, don't guess they were hidden.

Correction: runs 4 and 6 recorded Frigate as sending 6 frames. Actually Frigate computes the frame count from the camera's
aspect ratio; with `num_ctx` 6100, the 4:3 eval camera gets 8 frames (6 frames is for a 16:9 camera).

### Conclusion for the release

- The alert path works fully: 40/40 clips have an alert and summary, no alert lost, no
  timeouts, no false alarms. Operators receive **every** alert with a person; the AI only marks which are suspicious.
- The AI's theft marking (4B model) catches about 3/20 never-seen theft clips. Not enough to count as
  a theft detection feature; present it only as a priority hint, not a filter.
- The remaining directions all need a decision: a larger model (`qwen3-vl:8b`, a 6.2 GB download, may pass
  120 seconds with 10 frames), or a "needs a look" level for ambiguous descriptions (a new feature).

## Run 10: checking frames by eye, then the 8B model, offline on test3, 2026-11-01

**Are frames enough to see theft?** Viewing the 10-frame sheets (exactly what the model gets) for all 40
test3 clips myself, shuffled with names hidden, scoring "goods clearly going into a bag / maybe / no":

| Human score | Theft clips | Normal clips |
|---|---|---|
| Clearly | 10/20 | 0/20 |
| Maybe | 7/20 | 5/20 |
| No | 3/20 | 15/20 |
| *Caught by the 4B model* | *3/20* | *0/20* |

Frames are enough to catch about half of theft clips with no false alarms; the 3 clips the 4B model caught are all among
the 10 "clearly" clips. So the bottleneck is the model, not the frames. Limits: one scorer, and 4
clips were seen before shuffling.

**The 8B model** (`qwen3-vl:8b-instruct`, 10 frames, temperature 0), the same 40 test3 clips:

| | 4B model | 8B model, re-check with 4B | 8B model, re-check with 8B |
|---|---|---|---|
| Caught (effective level) | 3/20 | 7/20 | **9/20** |
| ... with Frigate's own level only | 0/20 | 7/20 | 7/20 |
| False alarms | 0/20 | 0/20 | 0/20 |
| Time per request | 53 seconds | 86 seconds (longest 90) | |

- The 8B build scores by itself (level 1), which the 4B build almost never does.
- On 3 clips the 8B build describes the reverse: "retrieves an orange package from his backpack and returns it to
  the table". Still images don't show which way the goods went.
- Re-checking with 8B adds 2 clips but they aren't reliable: one for "puts his hand in his pocket"
  (a hand, not goods), one for "looks around" matching the "looking around to see if anyone notices" rule. That rule
  is too broad, and it caused the false alarm in run 8.
- A 24 GB machine: the 8B build is the largest that runs alongside daily work. Move both Frigate and the re-check
  to 8B so Ollama keeps only one model.

## Run 11: test4 set, 8B model, through real Frigate, 2026-11-01

20 unused clips (10 theft, 10 normal), config as in run 10: 8B for both description and re-check,
10 frames, temperature 0. Video 29 minutes, clips ~86 seconds apart.

| | |
|---|---|
| Caught (effective level) | 4/10 |
| False alarms | 1/10 |
| Clips with a summary | 12/20 (7 timed out, 1 no alert) |
| Summary delay (median) | 137 seconds |

**This machine can't keep up with the alert rate on the 8B build.** Each review takes ~95 seconds on an M4 Pro with
Frigate running too, while alerts arrive every ~86 seconds. The queue grows (delay 112 → 158 seconds), then
from clip 13 every request passes 120 seconds and Frigate cuts it. Alerts still reach operators, only
without a summary or AI flag.

On the 12 summarised clips: caught 4/8 theft clips, 1/4 normal clips falsely flagged. The false alarm is Normal (69):
the model describes "unpacking items from his backpack onto the table" and scores level 1 itself. Two theft clips were
caught only through the "looking around" rule above, not because the description showed hiding goods.

### Conclusion

- **Accuracy:** the 8B build catches about 35–50% of never-seen theft clips (4B: 15%), with false alarms
  0–1 in 10–20 normal clips.
- **Capacity:** one 24 GB M4 Pro with the 8B build and 10 frames summarises only ~35 alerts per
  hour. Beyond that, extra alerts have no AI flag.
- Next, to choose: fewer frames on the 8B build (faster, maybe worse, not measured); alerting only
  when a person stands in the counter area (a Frigate zone) to reduce reviews; or a stronger VLM
  machine. The "looking around" rule should go or be tightened.

## Run 12: 8B model with 8 frames, offline on test3, 2026-11-01

Run 11 showed 10 frames are too slow for the alert rate. Trying 8 frames on the same 40 test3 clips, 8B for both
description and re-check, temperature 0:

| | 8B, 10 frames | 8B, 8 frames |
|---|---|---|
| Frigate's own score | 7/20 | 7/20 |
| Effective level | 7/20 (re-check with 4B) | 7/20 |
| False alarms | 0/20 | 0/20 |
| Time per request | 86 seconds (longest 90) | **64 seconds (longest 70)** |

The same number of clips caught, 4 clips in common; ~25% faster. Unlike the 4B build (10 frames far ahead of 6
in run 8), the 8B build loses nothing going from 10 down to 8 frames. Set Frigate `num_ctx` 6100 (8
frames for a 4:3 camera).

## Run 13: 8B model with 8 frames, through real Frigate, test3 and test4, 2026-11-01

| | Run 9: 4B, 10 frames (test3) | **test3** | **test4** | **Combined** |
|---|---|---|---|---|
| Caught (effective level) | 3/20 | 8/20 | 5/10 | **13/30 (43%)** |
| ... with Frigate's own level only | 1/20 | 6/20 | 5/10 | 11/30 |
| False alarms | 0/20 | 1/20 | 0/10 | **1/30** |
| Clips with a summary | 40/40 | 40/40 | 19/20 (1 no alert) | 59/60 |
| Reviews timed out (120 seconds) | 0 | 0 | 0 | **0** |
| Summary delay (median / longest) | 96 seconds | 104 / 110 seconds | 100 seconds | |

- **Keeps up with the alert rate** (one alert every ~86 seconds, or ~42 per hour): delay stays steady around
  105 seconds for the whole hour, with no queue and no timeouts. Offline requests take ~64–70 seconds, so one 24 GB M4
  Pro keeps up with about 50 alerts per hour.
- The only false alarm, Normal (43): "picks up an orange box, and leaves with it". The re-check matches
  the goods-in-bag rule, though the person carries the goods in hand (which the rules allow).
- A few theft clips were caught through the "looking around to see if anyone notices" rule. That rule is too broad; kept as is while
  measuring, it should be tightened on the dev set.
- Limits: test3 was used to choose 8 frames over 10, and test4 ran once in run 11 (while
  overloaded). No unused clips remain. These are staged clips; real-world numbers will be lower.

### Conclusion

Meets the goal set before changing the model: catch ≥ 8/20 never-seen theft clips, false alarms ≤ 1/20,
no review timing out. The AI flag is a priority hint for operators, not a filter: it catches about
4/10 staged thefts, with almost no false alarms, and every alert with a person still reaches the operator.

## Run 14: stutter fix and a tighter "looking around" rule, 2026-11-05

Two free changes tried before spending on a stronger machine, one at a time:

- **Stutter fix:** the fake camera no longer sends B-frames, which were recorded with out-of-order
  timestamps. Runs 1–13 used the old stream.
- **Tighter rule (v3):** "...before handling a product" became "...while moving a product toward their
  bag, pocket, or clothing".

8B model, 8 frames, temperature 0, CPU detector. All clips had been used before, so these numbers only
compare configurations.

Offline on dev (40 clips, chosen for fixing):

| Rules | Caught (effective level) | ... with Frigate's own level only | False alarms | Time per request |
|---|---|---|---|---|
| v2 | 12/20 | 12/20 | 0/20 | 65 seconds (longest 73) |
| v3 | 12/20 | 9/20 | 0/20 | 63 seconds (longest 66) |

Through real Frigate on test3 and test4:

| | Run 13: old stream, v2 | Stutter fixed, v2 | Stutter fixed, v3 |
|---|---|---|---|
| Caught (effective level), test3 | 8/20 | 8/20 | 6/20 |
| Caught (effective level), test4 | 5/10 | 5/10 | 2/10 |
| **Caught, combined** | **13/30** | **13/30** | **8/30** |
| ... with Frigate's own level only | 11/30 | 12/30 | 7/30 |
| False alarms | 1/30 | 1/30 | 1/30 |
| Clips with a summary | 59/60 | 59/60 | 59/60 |
| Reviews timed out (120 seconds) | 0 | 0 | 0 |
| Summary delay (median, test3 / test4) | 104 / 100 seconds | 99 / 99 seconds | 104 / 96 seconds |

Reading the tables:

- **The stutter fix changed no total.** It did change which clips were caught: only 8 of the 13 are the
  same clips, with 5 lost and 5 gained.
- **The tighter rule isn't better, so rules v2 stay.** Against v2 on the same clips it lost 8 thefts and
  gained 3. That gap could be chance (exact sign test on those 11 clips, p ≈ 0.23), but nothing here
  favours v3.
- **The "looking around" rule was never the main problem.** In the v2 pass the re-check matched it on 1
  alert of 60, and that alert was the pass's false alarm: Normal (44), "stands looking around". v3 cleared
  it, but a different clip took its place: Normal (50), "puts his hands in his pockets", matched the
  goods-in-bag rule though the report names no goods going in.
- **Descriptions are fragile.** Temperature 0 gives the same answer only for the same input. One changed
  rule sentence left just 3/40 dev answers identical. Swings of about 5 clips in 30 are therefore normal
  on this set, and smaller differences between configurations mean little.

**A second active camera loses summaries.** The first attempt at this run was discarded because the
`shop_aisle` sample video was also playing. The two cameras made 31 alerts in about 25 minutes (~74 per
hour) and only 15 had a summary when checked, the newest few still queued. The rest mostly hit Frigate's 120-second
timeout, and timed-out requests also waste the machine's time. With `shop_aisle` quiet, the same set had
no timeouts. Capacity belongs to the machine, not the camera: about 50 alerts per hour in total, as in
run 13, whatever the number of cameras.

### Conclusion

Neither free change improved accuracy on this data, and rule wording has stopped being a useful lever.
This set can't show whether real stores behave like these staged clips, so the next useful measurement
is video from a real scene, recorded with consent. It should come before buying a stronger machine.

## Run 15: tracking bags, 2026-11-10

Frigate tracked `backpack` and `handbag` as well as `person`. Alerts still came only from people. A bag
that moves during an alert is added to the prompt's "Objects in Scene", so the model is told a bag is
there. Everything else was as in run 14 (stutter fixed, rules v2), on a clean stack.

The CPU detector scores a carried bag at most about 0.65, under Frigate's default threshold of 0.7. The
first attempt therefore tracked no bags at all. The run used `min_score: 0.4` and `threshold: 0.5` for
both labels.

| | Run 14: stutter fixed, v2 | Run 15: with bags |
|---|---|---|
| Caught (effective level), test3 | 8/20 | 8/20 |
| Caught (effective level), test4 | 5/10 | 8/10 |
| **Caught, combined** | **13/30** | **16/30** |
| ... with Frigate's own level only | 12/30 | 13/30 |
| False alarms | 1/30 | 2/30 |
| Clips with a summary | 59/60 | 59/60 |
| Summary delay (median, test3 / test4) | 99 / 99 seconds | 113 / 109 seconds |

How often a bag reached the prompt, by clip:

| Clips | A bag in the alert | Flagged by Frigate's level |
|---|---|---|
| Theft, with a bag | 17/30 | 13/17 |
| Theft, no bag | 13/30 | 0/13 |
| Normal, with a bag | 5/30 | 1/5 |
| Normal, no bag | 25/30 | 0/25 |

Reading the tables:

- **The gain is within the noise.** Against run 14, 7 thefts were gained and 4 lost (exact sign test on
  those 11 clips, p ≈ 0.55). Run 14 showed that swings of about 5 clips in 30 are normal on this set.
- **Every theft caught at Frigate's level had a bag in the alert,** and the summaries mostly name it:
  "packing a white package into a backpack", "unzips a backpack, places an item from a table into it".
  The model treats bag handling itself as a sign.
- **That is a feature of this dataset, not of stores.** The actors bring a bag to "steal", so a bag
  appears in 17/30 theft clips but only 5/30 normal ones. Real customers often carry bags. The one
  normal clip flagged because of a bag, Normal (69), "inspecting items from his bag", shows the false
  alarms this would add in a real store.
- **The prompt grows and summaries come about 10 seconds later.**

### Conclusion

Bag tracking stays off. The gain is not separable from chance, and its mechanism, flagging anyone who
handles a bag, would add false alarms where customers carry bags. Measure it again only on real video,
recorded with consent.
