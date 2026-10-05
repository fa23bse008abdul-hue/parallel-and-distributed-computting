"""Local (client-side) rendering used for comparison/benchmarking."""
import os
import re
import subprocess
import threading
import time

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
PRESETS = {"p1": "ultrafast", "p2": "superfast", "p3": "veryfast", "p4": "fast",
           "p5": "medium", "p6": "slow", "p7": "slower"}


def probe_duration(path):
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def local_encode(src, dst, settings, use_nvenc=False, on_progress=None):
    """Encode locally with libx264 (default) or the local NVENC GPU. Returns {'seconds','cpu_avg'}."""
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    if settings.get("height"):
        cmd += ["-vf", f"scale=-2:{settings['height']}"]
    p = settings.get("preset", "p4")
    cmd += (["-c:v", "h264_nvenc", "-preset", p] if use_nvenc else ["-c:v", "libx264", "-preset", PRESETS[p]])
    cmd += ["-b:v", f"{settings.get('bitrate_kbps', 5000)}k", "-c:a", "aac", "-b:a", "128k",
            "-progress", "pipe:1", "-nostats", dst]
    duration, cpu, stop = probe_duration(src), [], threading.Event()
    try:
        import psutil

        def sampler():
            psutil.cpu_percent(None)
            while not stop.wait(0.5):
                cpu.append(psutil.cpu_percent(None))
        threading.Thread(target=sampler, daemon=True).start()
    except ImportError:
        pass
    t0, tail = time.perf_counter(), []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        k, _, v = line.strip().partition("=")
        if k in ("out_time_us", "out_time_ms") and v.lstrip("-").isdigit() and duration and on_progress:
            on_progress(max(0, min(99.9, int(v) / 1e6 / duration * 100)))
        elif k not in ("frame", "fps", "stream_0_0_q", "bitrate", "total_size", "out_time", "dup_frames",
                       "drop_frames", "speed", "progress", "out_time_us", "out_time_ms"):
            tail.append(line.strip())
    rc = proc.wait()
    stop.set()
    if rc != 0:
        raise RuntimeError("local ffmpeg failed: " + " | ".join(tail[-5:]))
    return {"seconds": time.perf_counter() - t0, "cpu_avg": sum(cpu) / len(cpu) if cpu else None}
