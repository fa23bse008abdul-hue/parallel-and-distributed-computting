#!/usr/bin/env python3
"""Local-vs-remote benchmark.

    python benchmark.py --host 192.168.1.1 --token secret testdata/*.mp4 --heights 720 1080 --repeat 3
Writes results/benchmark.csv, results/benchmark.md (+ results/speedup.png if matplotlib is installed).
"""
import argparse
import csv
import os
import statistics as st
import tempfile

from local_render import local_encode
from net_client import RenderClient, DEFAULT_PORT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--host", default="192.168.1.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default="changeme")
    ap.add_argument("--heights", type=int, nargs="+", default=[1080])
    ap.add_argument("--bitrate", type=int, default=8000)
    ap.add_argument("--preset", default="p4")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--local-nvenc", action="store_true", help="local encode with the client's own NVENC GPU")
    ap.add_argument("--out", default="../results")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    client = RenderClient(a.host, a.port, a.token, log=lambda *_: None)
    chk = client.check_worker(20)
    print(f"worker: {chk['caps'].get('gpu')} mode={chk['caps'].get('mode')} RTT avg {chk['rtt']['avg']:.2f} ms")
    rows, tmp = [], tempfile.mkdtemp()
    for f in a.files:
        for h in a.heights:
            settings = {"height": h, "bitrate_kbps": a.bitrate, "preset": a.preset}
            for rep in range(a.repeat):
                tag = f"{os.path.basename(f)} @{h}p run {rep + 1}"
                print("==", tag)
                loc = local_encode(f, os.path.join(tmp, "local.mp4"), settings, use_nvenc=a.local_nvenc)
                rem = RenderClient(a.host, a.port, a.token, log=lambda *_: None).render(
                    f, os.path.join(tmp, "remote.mp4"), settings)
                net = rem["upload_s"] + rem["download_s"]
                rows.append({
                    "file": os.path.basename(f), "input_MB": round(rem["input_bytes"] / 1e6, 1), "height": h,
                    "run": rep + 1, "local_s": round(loc["seconds"], 2), "remote_total_s": round(rem["total_s"], 2),
                    "upload_s": round(rem["upload_s"], 2), "remote_encode_s": round(rem["encode_s"], 2),
                    "download_s": round(rem["download_s"], 2),
                    "speedup_total": round(loc["seconds"] / rem["total_s"], 2),
                    "speedup_encode_only": round(loc["seconds"] / rem["encode_s"], 2),
                    "net_overhead_pct": round(100 * net / rem["total_s"], 1),
                    "upload_Mbps": round(rem["input_bytes"] * 8 / rem["upload_s"] / 1e6, 0),
                    "local_cpu_avg_pct": round(loc["cpu_avg"], 0) if loc["cpu_avg"] is not None else "",
                    "gpu_util_avg_pct": rem["gpu"].get("avg_util", ""), "gpu_util_max_pct": rem["gpu"].get("max_util", ""),
                    "gpu_vram_peak_MB": rem["gpu"].get("max_mem_mb", "")})
                print("   ", rows[-1]["local_s"], "s local vs", rows[-1]["remote_total_s"], "s remote ->",
                      rows[-1]["speedup_total"], "x")

    keys = list(rows[0])
    with open(os.path.join(a.out, "benchmark.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, keys)
        w.writeheader()
        w.writerows(rows)
    # averaged markdown table
    groups = {}
    for r in rows:
        groups.setdefault((r["file"], r["height"]), []).append(r)
    cols = ["input_MB", "local_s", "remote_total_s", "upload_s", "remote_encode_s", "download_s",
            "speedup_total", "speedup_encode_only", "net_overhead_pct", "gpu_util_avg_pct", "local_cpu_avg_pct"]
    md = ["| file | res | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 2)]
    for (fn, h), g in groups.items():
        vals = []
        for c in cols:
            xs = [x[c] for x in g if x[c] != ""]
            vals.append(f"{st.mean(xs):.2f}" if xs else "n/a")
        md.append(f"| {fn} | {h}p | " + " | ".join(vals) + " |")
    open(os.path.join(a.out, "benchmark.md"), "w").write("\n".join(md) + "\n")
    print("\n".join(md))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        labels = [f"{k[0].replace('.mp4', '')}\n{k[1]}p" for k in groups]
        loc = [st.mean(x["local_s"] for x in g) for g in groups.values()]
        rem = [st.mean(x["remote_total_s"] for x in g) for g in groups.values()]
        import numpy as np
        i = np.arange(len(labels))
        plt.figure(figsize=(max(7, len(labels) * 1.2), 4.5))
        plt.bar(i - 0.2, loc, 0.4, label="Local")
        plt.bar(i + 0.2, rem, 0.4, label="Remote GPU (incl. transfer)")
        plt.xticks(i, labels, fontsize=7)
        plt.ylabel("seconds")
        plt.legend()
        plt.title("Local vs remote render time")
        plt.tight_layout()
        plt.savefig(os.path.join(a.out, "speedup.png"), dpi=150)
    except ImportError:
        pass
    print("results written to", os.path.abspath(a.out))


if __name__ == "__main__":
    main()
