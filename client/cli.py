#!/usr/bin/env python3
"""Headless client:  python cli.py check --host 192.168.1.1
                    python cli.py render in.mp4 out.mp4 --host 192.168.1.1 --height 1080 --bitrate 8000"""
import argparse
import sys

from net_client import RenderClient, RenderError, DEFAULT_PORT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "render"])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--host", default="192.168.1.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default="changeme")
    ap.add_argument("--height", type=int, default=0, help="0 = keep original")
    ap.add_argument("--bitrate", type=int, default=5000, help="kbps")
    ap.add_argument("--preset", default="p4", choices=[f"p{i}" for i in range(1, 8)])
    a = ap.parse_args()

    last = {"phase": None, "pct": -10}

    def prog(phase, pct, info):
        if phase != last["phase"] or pct - last["pct"] >= 5 or pct >= 100:
            print(f"  [{phase:8}] {pct:5.1f}%  {info}")
            last.update(phase=phase, pct=pct)

    c = RenderClient(a.host, a.port, a.token, on_progress=prog)
    try:
        if a.cmd == "check":
            r = c.check_worker()
            print(f"worker OK  connect {r['connect_ms']:.1f} ms | RTT min/avg/max "
                  f"{r['rtt']['min']:.2f}/{r['rtt']['avg']:.2f}/{r['rtt']['max']:.2f} ms | "
                  f"mode={r['caps'].get('mode')} GPU={r['caps'].get('gpu')} queue={r['caps'].get('queue_len')}")
        else:
            if len(a.files) != 2:
                ap.error("render needs: INPUT OUTPUT")
            m = c.render(a.files[0], a.files[1], {"height": a.height, "bitrate_kbps": a.bitrate,
                                                  "preset": a.preset})
            print({k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()})
    except (RenderError, OSError) as e:
        print("ERROR:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
