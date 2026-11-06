#!/bin/sh
# Fake camera for Frigate: loops /samples/<path>.mp4 as an RTSP stream, with 60
# quiet seconds between loops. Real cameras have quiet moments; without a quiet gap
# Frigate's review never ends, so it never gets summarised.
# With no video yet it plays a test pattern (no people).
#
# MediaMTX calls this script through runOnInit and passes MTX_PATH, RTSP_PORT by env.
set -eu

out="rtsp://localhost:${RTSP_PORT}/${MTX_PATH}"
video="/samples/${MTX_PATH}.mp4"
# Test pattern (no video yet): 15 fps, a keyframe every 2 seconds like a real camera,
# so Frigate starts decoding or recording quickly without a long wait for a keyframe.
encode="-an -r 15 -c:v libx264 -preset veryfast -tune zerolatency -g 30 -pix_fmt yuv420p -f rtsp -rtsp_transport tcp"

if [ ! -f "$video" ]; then
    # shellcheck disable=SC2086
    exec ffmpeg -nostats -loglevel warning -re -f lavfi -i testsrc2=size=1280x720:rate=15 $encode "$out"
fi

# Build ONE loop file up front (the video then 60 quiet seconds, same 15 fps, a keyframe
# every 2 seconds), then loop it with `-c copy`. Joining two different sources while
# playing makes timestamps jump at the seam and the RTSP stream breaks.
size=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$video")
# No B-frames, like most real cameras: Frigate stamps frames by arrival time when it
# records, so frames sent out of display order play back with visible stutter.
# Fixed path, overwritten each time: a restart leaves no stray video-length file behind.
loop="/tmp/${MTX_PATH}-loop.mp4"
ffmpeg -nostdin -y -nostats -loglevel error -i "$video" -f lavfi -i "color=c=0x404040:s=${size}:r=15:d=60" \
    -filter_complex "[0:v]fps=15,setsar=1[a];[1:v]setsar=1[b];[a][b]concat=n=2:v=1:a=0[v]" \
    -map "[v]" -c:v libx264 -preset veryfast -bf 0 -g 30 -pix_fmt yuv420p "$loop"

exec ffmpeg -nostats -loglevel warning -re -stream_loop -1 -i "$loop" -c copy \
    -f rtsp -rtsp_transport tcp "$out"
