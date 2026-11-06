# Sample videos for the fake camera

`scripts/fetch_samples.sh` downloads videos here and checks their sha256. Video files are
in `.gitignore` and aren't committed.

MediaMTX loops `samples/<camera>.mp4` as the RTSP stream
`rtsp://mediamtx:8554/<camera>`; with no file it plays a test pattern (`testsrc2`, with
no people, so Frigate creates no alerts).

| File | Source | License |
|---|---|---|
| `shop-aisle.mp4` | `store-aisle-detection.mp4` from [intel-iot-devkit/sample-videos](https://github.com/intel-iot-devkit/sample-videos) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), per the intel-iot-devkit/sample-videos repo |
| `eval-cam.mp4` | `scripts/eval_alerts.py prepare` joins 40 clips from "Shoplifting Dataset (2022)", MNNIT, Mendeley Data, [doi:10.17632/r3yjf35hzr](https://doi.org/10.17632/r3yjf35hzr) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |

Use only public or self-recorded video. Don't put workplace video here
without written consent.
