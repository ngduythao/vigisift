#!/bin/sh
# Download sample videos for the fake camera and check sha256. Videos aren't committed.
#
#   scripts/fetch_samples.sh
#
# Source: github.com/intel-iot-devkit/sample-videos (CC BY 4.0, the repo is archived
# so the files don't change). License attribution is in samples/README.md.
set -eu

cd "$(dirname "$0")/../samples"

fetch() {
    name=$1 url=$2 sha=$3
    if [ -f "$name" ] && echo "$sha  $name" | shasum -a 256 -c - >/dev/null 2>&1; then
        echo "$name: already here"
        return
    fi
    curl -fsSL -o "$name.part" "$url"
    if ! echo "$sha  $name.part" | shasum -a 256 -c - >/dev/null 2>&1; then
        rm -f "$name.part"
        echo "$name: sha256 mismatch, removing the download" >&2
        exit 1
    fi
    mv "$name.part" "$name"
    echo "$name: downloaded"
}

fetch shop-aisle.mp4 \
    https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master/store-aisle-detection.mp4 \
    3526fee39cac70d6366471e4324a6d63b337c8f8ab99c015521bee0d60ed6e04
