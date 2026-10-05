#!/usr/bin/env python3
"""Remote GPU render daemon (headless worker node).

Accepts authenticated TCP connections, receives media files, queues them, encodes with
FFmpeg + NVENC (h264_nvenc), streams progress back and serves the result for download.

    python server.py --host 0.0.0.0 --port 5001 --token mysecret
"""
import argparse
import hmac
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid

from protocol import (VERSION, DEFAULT_PORT, ProtocolError, send_msg, recv_msg, make_mac,
                      sha256_file, send_file, recv_file)

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
log = logging.getLogger("worker")
JOBS, JOBS_LOCK, JOBQ = {}, threading.Lock(), queue.Queue()
CAPS = {"ffmpeg": False, "nvenc": False, "gpu": None, "mode": "cpu"}
CFG = argparse.Namespace()

# NVENC presets p1 (fastest) .. p7 (best quality) and their libx264 equivalents (CPU fallback)
PRESETS = {"p1": "ultrafast", "p2": "superfast", "p3": "veryfast", "p4": "fast",
           "p5": "medium", "p6": "slow", "p7": "slower"}
PROGRESS_KEYS = {"frame", "fps", "stream_0_0_q", "bitrate", "total_size", "out_time_us",
                 "out_time_ms", "out_time", "dup_frames", "drop_frames", "speed", "progress"}


# ----------------------------------------------------------------------------- jobs
class Job:
    def __init__(self, settings, ext):
        self.id = uuid.uuid4().hex[:12]
        self.settings = settings
        self.in_path = os.path.join(CFG.workdir, f"{self.id}_in{ext}")
        self.out_path = os.path.join(CFG.workdir, f"{self.id}_out.{settings['container']}")
        self.status, self.progress, self.error = "uploading", 0.0, None
        self.encode_s = self.out_size = self.out_sha = self.fps = self.speed = None
        self.gpu, self.mode = {}, CAPS["mode"]
        self.created, self.cancel, self.proc = time.time(), threading.Event(), None

    def snapshot(self):
        pos = 0
        if self.status == "queued":
            with JOBS_LOCK:
                pos = 1 + sum(1 for j in JOBS.values() if j.status == "queued" and j.created < self.created)
        return {"job_id": self.id, "status": self.status, "progress": round(self.progress, 1),
                "queue_pos": pos, "fps": self.fps, "speed": self.speed}

    def remove_files(self):
        for p in (self.in_path, self.out_path, self.out_path + ".part"):
            try:
                os.remove(p)
            except OSError:
                pass


def clean_settings(s):
    h = int(s.get("height") or 0)
    br = int(s.get("bitrate_kbps") or 5000)
    preset = str(s.get("preset", "p4"))
    if not 0 <= h <= 4320 or not 100 <= br <= 200000:
        raise ValueError("height/bitrate out of range")
    return {"height": h, "bitrate_kbps": br, "preset": preset if preset in PRESETS else "p4",
            "container": "mp4"}


def probe_duration(path):
    """Parse 'Duration: HH:MM:SS.xx' from `ffmpeg -i` (no ffprobe dependency)."""
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def build_cmd(job):
    s, gpu = job.settings, CFG.gpu
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    if gpu and not CFG.no_cuda_decode:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", job.in_path]
    if s["height"]:
        cmd += ["-vf", f"scale=-2:{s['height']}"]
    cmd += (["-c:v", "h264_nvenc", "-preset", s["preset"]] if gpu
            else ["-c:v", "libx264", "-preset", PRESETS[s["preset"]]])
    cmd += ["-b:v", f"{s['bitrate_kbps']}k", "-c:a", "aac", "-b:a", "128k",
            "-progress", "pipe:1", "-nostats", job.out_path]
    return cmd


def sample_gpu(stop, samples):
    while not stop.is_set():
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=3).stdout.splitlines()[0]
            u, m = (float(x) for x in out.split(","))
            samples.append((u, m))
        except Exception:
            pass
        stop.wait(1.0)


def run_job(job):
    if job.cancel.is_set():
        job.error, job.status = "cancelled", "failed"
        job.remove_files()
        return
    job.status = "running"
    duration = probe_duration(job.in_path)
    cmd = build_cmd(job)
    log.info("job %s: %s", job.id, " ".join(cmd))
    stop, samples, tail = threading.Event(), [], []
    if CFG.gpu and shutil.which("nvidia-smi"):
        threading.Thread(target=sample_gpu, args=(stop, samples), daemon=True).start()
    t0, rc = time.perf_counter(), -1
    try:
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
        for line in job.proc.stdout:
            line = line.strip()
            if job.cancel.is_set():
                job.proc.kill()
                break
            k, _, v = line.partition("=")
            if k in ("out_time_us", "out_time_ms") and v.lstrip("-").isdigit() and duration:
                job.progress = max(0.0, min(99.9, int(v) / 1e6 / duration * 100))
            elif k == "fps":
                try:
                    job.fps = float(v)
                except ValueError:
                    pass
            elif k == "speed":
                job.speed = v
            elif k not in PROGRESS_KEYS and line:
                tail = (tail + [line])[-8:]
        rc = job.proc.wait()
    except Exception as e:  # e.g. ffmpeg not installed
        tail.append(str(e))
    finally:
        stop.set()
    job.encode_s = time.perf_counter() - t0
    if samples:
        us, ms = [s[0] for s in samples], [s[1] for s in samples]
        job.gpu = {"avg_util": round(sum(us) / len(us), 1), "max_util": max(us),
                   "max_mem_mb": max(ms), "samples": len(us)}
    try:
        os.remove(job.in_path)
    except OSError:
        pass
    if job.cancel.is_set():
        job.error, job.status = "cancelled", "failed"
    elif rc != 0 or not os.path.exists(job.out_path):
        job.error, job.status = " | ".join(tail) or f"ffmpeg exited with {rc}", "failed"
    else:
        job.out_size, job.out_sha = os.path.getsize(job.out_path), sha256_file(job.out_path)
        job.progress, job.status = 100.0, "done"
    log.info("job %s finished: %s in %.1fs", job.id, job.status, job.encode_s)


def worker_loop():
    while True:
        job = JOBQ.get()
        try:
            run_job(job)
        except Exception:
            log.exception("worker crashed on job %s", job.id)
            job.error, job.status = "internal error", "failed"


# ----------------------------------------------------------------------------- connections
def stream_job(conn, job):
    while True:
        send_msg(conn, {"type": "PROGRESS", **job.snapshot()})
        if job.status in ("done", "failed"):
            break
        time.sleep(0.5)
    if job.status == "failed":
        send_msg(conn, {"type": "FAILED", "error": job.error})
    else:
        send_msg(conn, {"type": "DONE", "job_id": job.id, "encode_s": job.encode_s, "size": job.out_size,
                        "sha256": job.out_sha, "gpu": job.gpu, "mode": job.mode})


def handle_submit(conn, m):
    try:
        size, settings = int(m["size"]), clean_settings(m.get("settings", {}))
    except (KeyError, ValueError, TypeError) as e:
        send_msg(conn, {"type": "ERROR", "message": f"bad SUBMIT: {e}"})
        return
    if size <= 0 or size > CFG.max_bytes or shutil.disk_usage(CFG.workdir).free < size * 3:
        send_msg(conn, {"type": "ERROR", "message": "file too large or insufficient disk space"})
        return
    ext = re.sub(r"[^A-Za-z0-9.]", "", os.path.splitext(str(m.get("filename", "")))[1])[:8] or ".bin"
    job = Job(settings, ext)
    with JOBS_LOCK:
        JOBS[job.id] = job
    send_msg(conn, {"type": "READY", "job_id": job.id})
    try:
        recv_file(conn, job.in_path, size)
    except Exception:
        job.remove_files()
        with JOBS_LOCK:
            JOBS.pop(job.id, None)
        raise
    if sha256_file(job.in_path) != m.get("sha256"):
        log.warning("job %s: upload checksum mismatch", job.id)
        job.remove_files()
        with JOBS_LOCK:
            JOBS.pop(job.id, None)
        send_msg(conn, {"type": "UPLOAD_BAD"})
        return
    send_msg(conn, {"type": "UPLOAD_OK", "job_id": job.id})
    job.status = "queued"
    JOBQ.put(job)
    stream_job(conn, job)


def handle(conn, addr):
    log.info("connection from %s:%d", *addr[:2])
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(CFG.timeout)
    try:
        nonce = secrets.token_hex(16)
        send_msg(conn, {"type": "CHALLENGE", "nonce": nonce, "version": VERSION})
        hello = recv_msg(conn)
        if hello.get("type") != "HELLO" or not hmac.compare_digest(str(hello.get("mac", "")),
                                                                    make_mac(CFG.token, nonce)):
            send_msg(conn, {"type": "ERROR", "message": "authentication failed"})
            log.warning("auth failed from %s", addr[0])
            return
        send_msg(conn, {"type": "HELLO_ACK", **CAPS, "queue_len": JOBQ.qsize()})
        while True:
            m = recv_msg(conn)
            t, jid = m.get("type"), m.get("job_id")
            job = JOBS.get(jid)
            if t == "PING":
                send_msg(conn, {"type": "PONG"})
            elif t == "SUBMIT":
                handle_submit(conn, m)
            elif t in ("WATCH", "FETCH", "CANCEL", "CLEANUP") and job is None:
                send_msg(conn, {"type": "ERROR", "message": "unknown job"})
            elif t == "WATCH":
                stream_job(conn, job)
            elif t == "FETCH":
                if job.status != "done":
                    send_msg(conn, {"type": "ERROR", "message": "job not finished"})
                    continue
                send_msg(conn, {"type": "FILE", "size": job.out_size, "sha256": job.out_sha})
                send_file(conn, job.out_path)
            elif t == "CANCEL":
                job.cancel.set()
                if job.proc and job.proc.poll() is None:
                    job.proc.kill()
                send_msg(conn, {"type": "OK"})
            elif t == "CLEANUP":
                job.remove_files()
                with JOBS_LOCK:
                    JOBS.pop(jid, None)
                send_msg(conn, {"type": "OK"})
            elif t == "BYE":
                return
            else:
                send_msg(conn, {"type": "ERROR", "message": f"unknown message {t!r}"})
    except (OSError, ProtocolError) as e:
        log.warning("connection %s dropped: %s (running jobs are unaffected)", addr[0], e)
    finally:
        conn.close()


# ----------------------------------------------------------------------------- startup
def detect_caps():
    try:
        enc = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        CAPS["ffmpeg"], CAPS["nvenc"] = True, "h264_nvenc" in enc
    except FileNotFoundError:
        pass
    if shutil.which("nvidia-smi"):
        try:
            CAPS["gpu"] = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                                          "--format=csv,noheader"], capture_output=True,
                                         text=True, timeout=5).stdout.splitlines()[0].strip()
        except Exception:
            pass


def main():
    global CFG
    ap = argparse.ArgumentParser(description="Remote GPU render daemon")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default=os.environ.get("RENDER_TOKEN", "changeme"),
                    help="shared secret (or env RENDER_TOKEN)")
    ap.add_argument("--workdir", default="./jobs")
    ap.add_argument("--workers", type=int, default=1, help="concurrent encodes")
    ap.add_argument("--timeout", type=float, default=60, help="socket I/O timeout (s)")
    ap.add_argument("--max-gb", type=float, default=20)
    ap.add_argument("--cpu", action="store_true", help="force libx264 (testing without a GPU)")
    ap.add_argument("--no-cuda-decode", action="store_true", help="drop '-hwaccel cuda' (decode on CPU)")
    CFG = ap.parse_args()
    CFG.max_bytes = int(CFG.max_gb * 2**30)
    os.makedirs(CFG.workdir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("server.log")])
    detect_caps()
    CFG.gpu = CAPS["nvenc"] and not CFG.cpu
    CAPS["mode"] = "nvenc" if CFG.gpu else "cpu"
    if not CAPS["ffmpeg"]:
        log.error("ffmpeg not found - install it or set the FFMPEG env var")
    elif not CFG.gpu:
        log.warning("running in CPU mode (h264_nvenc unavailable or --cpu given)")
    log.info("GPU: %s | mode: %s | workdir: %s", CAPS["gpu"], CAPS["mode"], os.path.abspath(CFG.workdir))
    for _ in range(CFG.workers):
        threading.Thread(target=worker_loop, daemon=True).start()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((CFG.host, CFG.port))
    srv.listen(8)
    log.info("listening on %s:%d", CFG.host, CFG.port)
    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
