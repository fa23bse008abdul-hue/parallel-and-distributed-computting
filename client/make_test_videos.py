#!/usr/bin/env python3
"""Generate synthetic benchmark inputs:  python make_test_videos.py  ->  testdata/*.mp4"""
import os
import subprocess

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
# (name, resolution, seconds)  -> a spread of sizes / resolutions
SPECS = [("sd_480p_20s", "854x480", 20), ("hd_720p_30s", "1280x720", 30),
         ("fhd_1080p_30s", "1920x1080", 30), ("fhd_1080p_90s", "1920x1080", 90),
         ("uhd_2160p_30s", "3840x2160", 30)]

os.makedirs("testdata", exist_ok=True)
for name, size, secs in SPECS:
    out = f"testdata/{name}.mp4"
    if os.path.exists(out):
        continue
    print("creating", out)
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", str(secs), "-c:v", "libx264", "-preset", "veryfast", "-b:v", "20M",
                    "-c:a", "aac", "-shortest", out], check=True)
print("done")
