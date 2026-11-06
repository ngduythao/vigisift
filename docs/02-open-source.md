# Which areas to build, which to take free and ready-made

Checked 2026-10-22. The question: among the things VigiSift does (or plans to), in which areas
does a project already do it better and run for free, so we use theirs rather than rewrite it.

Criterion: **runs with no monthly service fee.** Self-hostable, with no subscription
or monthly-billed API for the parts needed. Rules out projects where a needed feature
is only in the paid edition. Open source is preferred, for learning from the code and avoiding lock-in, but
the license isn't a hard condition.

## 1. Short conclusion

- **Frigate** (MIT) is far better at everything about cameras: object detection, zones,
  recording, playback, counting people, and even VLM scene summaries with a per-camera suspicion level. From
  0.18 it also has a chat agent with its own camera tools. This is the largest integration.
- **Valkey** (BSD-3) replaces `redis:7.4`. Not required: self-hosted Redis 7.4 is still free
  (the RSALv2/SSPLv1 license only bans selling Redis as a service). Changed because Valkey is also free,
  fully open, and a drop-in replacement with no code changes.
- VigiSift keeps what Frigate and other tools do **not** do for a multi-site monitoring
  center: per-site rules, gathering alerts across locations, operators judging them, evals measuring
  false alarms, an LLM budget and cost ledger.
- The old stage A ("clips instead of images", writing `ClipSource` ourselves) is **dropped**: Frigate already picks frames and
  sends up to 20 to the VLM.

## 2. Area by area

| Area | VigiSift today | Project doing it better | License | Decision |
|---|---|---|---|---|
| Motion and object detection | Nothing real: `scripts/simulate_device.py` fires fake labels | Frigate: cheap motion first, object detection after, with masks and zones | MIT | **Use Frigate** |
| Recording, clips, playback | None | Frigate: continuous or motion recording, a Review page split into alert/detection | MIT | **Use Frigate** |
| Pulling frames for the VLM | FFmpeg opening RTSP each time, waiting for a keyframe (~2 s) | Frigate already decodes the stream; its API returns snapshots, preview frames, clips | MIT | **Use the Frigate API** |
| Counting people | None | Frigate pushes object counts per camera and per zone over MQTT (`frigate/<zone>/person`) | MIT | **Use Frigate** |
| VLM description, suspicion level | 1 frame, a "stranger" rule hard-coded for a home | Frigate review summaries: up to 20 frames, per-camera `activity_context_prompt`, `potential_threat_level` 0/1/2, `confidence`, `other_concerns` | MIT | **Build on Frigate**, VigiSift measures with evals |
| Running the VLM locally | Ollama + `qwen3-vl:8b-instruct` | Frigate calls Ollama, llama.cpp or an OpenAI-compatible endpoint directly. Qwen3.5 takes both images and video | Ollama MIT, llama.cpp MIT, Qwen Apache 2.0 | Keep Ollama; try Qwen3.5 in the eval |
| Detection on a Mac | None | `frigate-nvr/apple-silicon-detector` runs on the Neural Engine, connecting to Frigate over ZMQ | MIT | Optional; CPU by default. Measured: sees people as well as CPU, total CPU ~99% → ~43% of a core |
| Live video viewing | MediaMTX (WebRTC, HLS) | go2rtc bundled with Frigate | MIT | MediaMTX stays as the **fake camera** playing sample video; live viewing through Frigate |
| Cache, cooldown, pub/sub | `redis:7.4` | Valkey, forked from Redis 7.2.4, a drop-in replacement | BSD-3 | **Move to Valkey** (both are free) |
| Retry, token budget, cost ledger | Hand-written, ~150 lines | LiteLLM (free to self-host, MIT core, with a paid enterprise part), Bifrost, Helicone (in maintenance mode) | Open core | **Keep ours**: small, enough, with no extra service to run |
| Chat agent, tools | Hand-written loop + MCP | Frigate 0.18 chat: find objects, find similar images, view cameras, turn on a VLM watch of a camera | MIT | Keep ours for multi-site and devices; for cameras call the Frigate API |
| Eval | Hand-written, scoring scene descriptions as text | promptfoo (MIT, bought by OpenAI in 2026), Inspect AI, DeepEval | MIT/Apache | **Keep ours**: must measure the whole system (video → Frigate → verdict); those tools measure one prompt turn |
| Labelling datasets | None yet | Label Studio (Apache 2.0), CVAT (MIT) | Apache/MIT | Not needed with 40–60 clips; use when the eval set grows |
| Operators judging alerts | None yet | Frigate only has "reviewed"; submitting false positives is part of Frigate+ (a paid service) | — | **Build** in VigiSift |
| Gathering alerts across sites | None yet | Frigate runs per location | — | **Build** in VigiSift |
| Theft-specific models | None yet | Paza (2026): cheap edge + behavior filter + VLM, code not released. Small, old MIT repos. The PoseLift set (Apache 2.0) | — | Not integrated; learn Paza's tiering |
| Devices, commands, presence | Hand-written | Home Assistant | Apache 2.0 | No longer a focus: keep as is, don't expand |

## 3. Costs and licenses to remember

- **Frigate**: free, MIT source; the name and logo "Frigate" are trademarks. Frigate+ (training
  models on your images, submitting false positive feedback) is a **paid subscription**, not used.
- **Detection models**: YOLOv9 (GPL-3.0) is still open source but copyleft; D-FINE is
  Apache 2.0. The default CPU model (MobileNet v2) is bundled with Frigate.
- **Redis 7.4** is RSALv2/SSPLv1: free to self-host but not open source. Valkey stays
  BSD-3. The Python `redis` library (MIT) works with both.
- **Inter** (SIL OFL 1.1) is the console font, bundled in `src/vigisift/web/fonts/` with its license.
- **Claude API** bills per token. Kept as an option; the default runs a local model through Ollama,
  costing only electricity.
- **Data**: UCF-Crime and DCSASS have their own terms, read before use. PoseLift is
  Apache 2.0. The eval set uses "Shoplifting Dataset (2022)" by MNNIT (Mendeley Data, CC BY 4.0).

## 4. What you only learn by running real Frigate (0.18.2)

Docs don't say; the source or measurements show it. Recorded in
`deploy/frigate/config.template.yml` next to the configuration:

- **`detect.enabled` defaults to `false`.** Without it cameras run and record, but there is never
  any detection or alert.
- **Without the `version` key Frigate "migrates" the config from 0.13**, and the 0.16→0.17 step moves a
  0.18-style `genai` block to the old place, breaking the config and sending Frigate to safe mode. Set `version: 0.18-0`.
- **The frame count sent to the VLM comes from context**, estimating ~1 token/1,250 pixels. With
  `qwen3-vl` on Ollama each image uses ~1,050 tokens at any resolution (measured from
  `prompt_eval_count`), so letting Frigate fill the context always overflows. The fix:
  the `ollama` provider has two places for `num_ctx`: `provider_options.options.num_ctx` only serves
  Frigate's frame count, while `runtime_options.options` is what's really sent to Ollama (16,384), since
  Frigate merges `runtime_options` over it. The frame count also depends on the camera's aspect ratio: 6100 gives 8 frames
  on a 4:3 camera but 6 on 16:9.
- **The VLM call timeout is a hard 120 seconds**, not configurable, and `qwen3-vl:4b` "thinks"
  by default. Through the OpenAI-compatible endpoint Ollama ignores `think: false`, so real requests
  take 80–120 seconds and about half of reviews time out. Frigate's `ollama` provider sends
  `think: false` properly. Measured on an M4 Pro: ~3.5 seconds per frame + ~25 seconds generating JSON; with 10
  frames still 80–110 seconds, with 6 frames 57–60 seconds and every full review gets a
  summary. Ollama handles one review at a time, so alerts arriving faster than a summary takes queue
  and then time out. Now using `qwen3-vl:8b-instruct` with 8 frames: ~65 seconds per review, one M4 Pro
  keeps up with ~50 alerts per hour (`docs/03-eval.md` runs 11–13).
- **Ollama's `qwen3-vl:4b` tag now points to the Thinking build** (`ollama show` says
  `qwen3-vl:4b-thinking`): it always thinks for hundreds to over a thousand tokens, and `think: false`
  can't turn it off. The eval caught this: 17/29 alerts had no summary. Use
  `qwen3-vl:4b-instruct`: 29/29 summarised, 70–110 seconds after the review closes
  (`docs/03-eval.md`).
- **The `genai` message widens review times** at each end by 10% of the duration, at most 5 seconds (measured
  3 seconds for a 30-second review), and Frigate saves the widened values too.
- **Changing suspicious rules needs no restart.** `PUT /api/config/set` with
  `update_topic: config/cameras/<cam>/review` writes to `config.yml` and publishes the camera's whole `review`
  block; the summary process (`embeddings/maintainer.py`) listens on that topic and replaces all of
  `review.genai` (`config/camera/updater.py`). Needs an admin-role account. Because Frigate rewrites
  `config.yml` itself, the running file isn't in git, only the template.
- **A review closes only after the person has been gone `cutoff_time` (40 seconds)** plus a few tracking
  seconds. Two appearances closer than that become one review: that's why the eval set puts 75 quiet
  seconds between clips.

## 5. Architecture after integration

```text
Sample video ──FFmpeg──> MediaMTX (fake camera, RTSP) ──> Frigate (one per site)
                                                        │  motion → object detection → zone
                                                        │  recording, Review page
                                                        │  review summary: Ollama/llama.cpp + Qwen
                                                        │
                                       MQTT frigate/reviews, frigate/<zone>/person
                                                        │
                                                        ▼
                                               VigiSift (FastAPI)
                                   per-site rules → each camera's activity_context_prompt
                                   gathers alerts across sites, operators judge them
                                   eval: false alarms per hour, recall, cost
                                   Postgres (truth), Valkey (cooldown, budget, pub/sub)
```

## 6. Sources

- Frigate: [repo](https://github.com/blakeblackshear/frigate), [0.17.0](https://github.com/blakeblackshear/frigate/releases/tag/v0.17.0), [releases](https://github.com/blakeblackshear/frigate/releases), [Review Summaries](https://docs.frigate.video/configuration/genai/genai_review/), [MQTT](https://docs.frigate.video/integrations/mqtt/), [object detectors](https://docs.frigate.video/configuration/object_detectors), [VLM watch job](https://docs.frigate.video/integrations/api/schemas/vlmmonitorrequest/), [apple-silicon-detector](https://github.com/frigate-nvr/apple-silicon-detector)
- Articles on Frigate 0.18: [ettayeb.fr](https://ettayeb.fr/en/selfhosted/frigate-0-18-ui-configuration-2026/)
- NVR comparisons: [bigiron.cc](https://www.bigiron.cc/guides/frigate-vs-shinobi-vs-zoneminder), [privacysmarthome.com](https://www.privacysmarthome.com/guides/local-nvr-comparison-frigate-scrypted-unifi-blue-iris-2026/), [Viseron](https://github.com/roflcoopter/viseron)
- Redis and Valkey: [Phoronix: Redis 8 AGPLv3](https://www.phoronix.com/news/Redis-8.0-Goes-AGPLv3), [elest.io](https://blog.elest.io/valkey-vs-redis-vs-keydb-which-in-memory-store-after-the-license-change/)
- VLM: [Qwen3-VL](https://github.com/qwenlm/qwen3-vl), [llama.cpp adds video input](https://aiweekly.co/alerts/llamacpp-adds-video-input-via-ffmpeg-subprocess), [Qwen3.5 on SGLang](https://cookbook.sglang.io/autoregressive/Qwen/Qwen3.5)
- Theft detection: [Paza (arXiv 2604.14846)](https://arxiv.org/abs/2604.14846), [PoseLift](https://github.com/TeCSAR-UNCC/PoseLift)
- Gateway LLM: [openalternative.co](https://openalternative.co/alternatives/litellm), [kosmoy.com](https://www.kosmoy.com/resources/blog/litellm-alternatives/)
- Eval: [cognee.ai](https://www.cognee.ai/llm-agent-evaluation-tools)
- Labelling: [Label Studio](https://github.com/HumanSignal/label-studio), [CVAT](https://docs.cvat.ai/docs/getting_started/overview/)
