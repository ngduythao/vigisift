# Live demo

[vigisift-demo.vercel.app](https://vigisift-demo.vercel.app) is the real console page with no
server behind it. `demo.js` loads before the page's own script and answers its API calls in the
browser from recorded data:

- The queue, history and metrics come from 4 recorded alerts. Verdicts change them for the
  current tab only, and a reload starts over.
- Clips are the MP4 files Frigate recorded for those alerts.
- The live connection reports itself as live but sends nothing. Device commands and the assistant
  say they are off in the demo.
- Recorded times move so the newest alert started a few minutes ago.

## What was recorded

A clean stack (`make demo`) with `qwen3-vl:8b-instruct`, rules from
`evals/shoplifting/rules.json` on the eval camera, and two fake cameras:

| Camera | Video | Alerts |
|---|---|---|
| `eval_cam` | 6 staged clips from the Shoplifting Dataset (MNNIT, CC BY 4.0): 4 thefts, 2 normal | 6 recorded, 2 kept: the normal clips |
| `shop_aisle` | `store-aisle-detection.mp4` (intel-iot-devkit, CC BY 4.0) | 2, normal |

The AI text is exactly what the model wrote. The four theft clips were dropped (`demo_site.py
drop`) because the model described all four wrongly: it reversed putting an item into a bag into
taking one out, and missed an item going into a pocket. Two of them it flagged anyway, for the
wrong reason. Rerunning doesn't help, since Frigate asks at temperature 0. The demo therefore
shows the console, not theft detection; [the eval](../docs/03-eval.md) has the real numbers.

## Rebuild and publish

```bash
# with a stack running and some summarised alerts
VIGISIFT_USER=... VIGISIFT_PASSWORD=... uv run python scripts/demo_site.py capture
uv run python scripts/demo_site.py drop <review_id>...   # optional
uv run python scripts/demo_site.py build
npx vercel deploy --prod samples/demo/site
```

Recordings and the built site stay under `samples/demo/`, so clips never enter git.

The Vercel project is deliberately not connected to the GitHub repo: a git deploy would publish
the repo root instead of the built site and replace the demo. `vercel link` connects it by
itself, so run `npx vercel git disconnect` after linking a new project.
