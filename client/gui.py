#!/usr/bin/env python3
"""CustomTkinter desktop client for the distributed GPU offloading system."""
import os
import queue
import threading
import time
from tkinter import filedialog, messagebox

import customtkinter as ctk

from local_render import local_encode
from net_client import RenderClient, RenderError, Cancelled, DEFAULT_PORT

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

RESOLUTIONS = {"Original": 0, "2160p (4K)": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480}
PRESETS = [f"p{i}" for i in range(1, 8)]


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Distributed GPU Render Offloader")
        self.geometry("920x780")
        self.minsize(820, 700)
        self.q = queue.Queue()
        self.client, self.busy = None, False
        self.t_local = self.t_remote = None
        self._build()
        self.after(100, self._pump)

    # ------------------------------------------------------------------ UI
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # server connection
        f = ctk.CTkFrame(self)
        f.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        ctk.CTkLabel(f, text="Remote Worker", font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, columnspan=6, padx=10, pady=(8, 2), sticky="w")
        self.ip = ctk.CTkEntry(f, width=150, placeholder_text="Server IP")
        self.ip.insert(0, "192.168.1.1")
        self.port = ctk.CTkEntry(f, width=70)
        self.port.insert(0, str(DEFAULT_PORT))
        self.token = ctk.CTkEntry(f, width=140, show="*", placeholder_text="Token")
        self.token.insert(0, "changeme")
        for i, (lbl, w) in enumerate([("IP", self.ip), ("Port", self.port), ("Token", self.token)]):
            ctk.CTkLabel(f, text=lbl).grid(row=1, column=i * 2, padx=(10, 2), pady=8, sticky="e")
            w.grid(row=1, column=i * 2 + 1, padx=(0, 6), pady=8)
        self.btn_test = ctk.CTkButton(f, text="Test Connection", width=130, command=self.test_connection)
        self.btn_test.grid(row=1, column=6, padx=10)
        self.status = ctk.CTkLabel(f, text="● not checked", text_color="gray")
        self.status.grid(row=2, column=0, columnspan=7, padx=10, pady=(0, 8), sticky="w")

        # job settings
        j = ctk.CTkFrame(self)
        j.grid(row=1, column=0, padx=12, pady=6, sticky="ew")
        j.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(j, text="Render Job", font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, columnspan=4, padx=10, pady=(8, 2), sticky="w")
        self.src = ctk.CTkEntry(j, placeholder_text="Input video file...")
        self.src.grid(row=1, column=1, columnspan=2, padx=6, pady=4, sticky="ew")
        ctk.CTkLabel(j, text="Input").grid(row=1, column=0, padx=10, sticky="e")
        ctk.CTkButton(j, text="Browse", width=90, command=self.pick_src).grid(row=1, column=3, padx=10)
        self.dst = ctk.CTkEntry(j, placeholder_text="Output file...")
        self.dst.grid(row=2, column=1, columnspan=2, padx=6, pady=4, sticky="ew")
        ctk.CTkLabel(j, text="Output").grid(row=2, column=0, padx=10, sticky="e")
        ctk.CTkButton(j, text="Save as", width=90, command=self.pick_dst).grid(row=2, column=3, padx=10)

        q = ctk.CTkFrame(j, fg_color="transparent")
        q.grid(row=3, column=0, columnspan=4, padx=6, pady=(4, 10), sticky="w")
        self.res = ctk.CTkOptionMenu(q, values=list(RESOLUTIONS), width=120)
        self.res.set("1080p")
        self.bitrate = ctk.CTkEntry(q, width=80)
        self.bitrate.insert(0, "8000")
        self.preset = ctk.CTkOptionMenu(q, values=PRESETS, width=80)
        self.preset.set("p4")
        for i, (lbl, w) in enumerate([("Resolution", self.res), ("Bitrate (kbps)", self.bitrate),
                                      ("Preset (p1 fast … p7 quality)", self.preset)]):
            ctk.CTkLabel(q, text=lbl).grid(row=0, column=i * 2, padx=(8, 4))
            w.grid(row=0, column=i * 2 + 1, padx=(0, 10))

        # actions + progress
        a = ctk.CTkFrame(self)
        a.grid(row=2, column=0, padx=12, pady=6, sticky="ew")
        a.grid_columnconfigure(3, weight=1)
        self.btn_go = ctk.CTkButton(a, text="▶ Render on Remote GPU", command=self.start_remote)
        self.btn_go.grid(row=0, column=0, padx=10, pady=10)
        self.btn_local = ctk.CTkButton(a, text="Render Locally (compare)", fg_color="#444",
                                       hover_color="#555", command=self.start_local)
        self.btn_local.grid(row=0, column=1, padx=4)
        self.btn_cancel = ctk.CTkButton(a, text="Cancel", width=80, fg_color="#a33", hover_color="#c44",
                                        state="disabled", command=self.cancel)
        self.btn_cancel.grid(row=0, column=2, padx=4)
        self.phase = ctk.CTkLabel(a, text="idle", anchor="e")
        self.phase.grid(row=0, column=3, padx=10, sticky="e")
        self.bar = ctk.CTkProgressBar(a)
        self.bar.set(0)
        self.bar.grid(row=1, column=0, columnspan=4, padx=10, pady=(0, 2), sticky="ew")
        self.pct = ctk.CTkLabel(a, text="0.0 %")
        self.pct.grid(row=2, column=0, columnspan=4, pady=(0, 8))

        # log terminal
        self.logbox = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
        self.logbox.grid(row=3, column=0, padx=12, pady=(6, 12), sticky="nsew")
        self.logbox.configure(state="disabled")
        self.log("Ready. Enter the worker IP, press 'Test Connection', pick a file and render.")

    # ------------------------------------------------------------------ helpers
    def log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def qlog(self, text):
        self.q.put(("log", text))

    def pick_src(self):
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"),
                                                  ("All files", "*.*")])
        if p:
            self.src.delete(0, "end")
            self.src.insert(0, p)
            base, _ = os.path.splitext(p)
            self.dst.delete(0, "end")
            self.dst.insert(0, base + "_rendered.mp4")

    def pick_dst(self):
        p = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if p:
            self.dst.delete(0, "end")
            self.dst.insert(0, p)

    def settings(self):
        try:
            br = int(self.bitrate.get())
        except ValueError:
            raise ValueError("Bitrate must be an integer (kbps)")
        return {"height": RESOLUTIONS[self.res.get()], "bitrate_kbps": br, "preset": self.preset.get()}

    def set_busy(self, busy):
        self.busy = busy
        for b in (self.btn_go, self.btn_local, self.btn_test):
            b.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    def _client(self):
        return RenderClient(self.ip.get().strip(), int(self.port.get()), self.token.get(),
                            log=self.qlog, on_progress=lambda *a: self.q.put(("progress", *a)))

    def _pump(self):
        try:
            while True:
                kind, *a = self.q.get_nowait()
                if kind == "log":
                    self.log(a[0])
                elif kind == "progress":
                    phase, pct, info = a
                    self.bar.set(pct / 100)
                    self.pct.configure(text=f"{pct:.1f} %")
                    self.phase.configure(text=f"{phase.upper()} — {info}")
                elif kind == "status":
                    self.status.configure(text=a[0], text_color=a[1])
                elif kind == "finished":
                    self.set_busy(False)
                    self.phase.configure(text=a[0])
        except queue.Empty:
            pass
        self.after(100, self._pump)

    # ------------------------------------------------------------------ actions
    def test_connection(self):
        self.status.configure(text="● checking…", text_color="orange")
        self.set_busy(True)

        def work():
            try:
                r = self._client().check_worker()
                c, rtt = r["caps"], r["rtt"]
                gpu = c.get("gpu") or "no NVIDIA GPU detected"
                self.q.put(("status", f"● online — {gpu} | mode {c.get('mode')} | RTT avg {rtt['avg']:.2f} ms",
                            "#3c3"))
                self.qlog(f"Handshake OK. connect={r['connect_ms']:.1f} ms, RTT min/avg/max = "
                          f"{rtt['min']:.2f}/{rtt['avg']:.2f}/{rtt['max']:.2f} ms, queue={c.get('queue_len')}")
                if c.get("mode") != "nvenc":
                    self.qlog("WARNING: worker is NOT using NVENC (CPU fallback).")
            except Exception as e:
                self.q.put(("status", f"● unreachable — {e}", "#e44"))
                self.qlog(f"Connection check failed: {e}")
            self.q.put(("finished", "idle"))
        threading.Thread(target=work, daemon=True).start()

    def _validate(self):
        src, dst = self.src.get().strip(), self.dst.get().strip()
        if not os.path.isfile(src):
            messagebox.showerror("Input", "Choose a valid input file.")
            return None
        if not dst:
            messagebox.showerror("Output", "Choose an output path.")
            return None
        try:
            return src, dst, self.settings()
        except ValueError as e:
            messagebox.showerror("Settings", str(e))

    def start_remote(self):
        v = self._validate()
        if not v:
            return
        src, dst, st = v
        self.set_busy(True)
        self.bar.set(0)
        self.client = self._client()
        self.qlog(f"Submitting {os.path.basename(src)} with {st}")

        def work():
            try:
                m = self.client.render(src, dst, st)
                self.t_remote = m["total_s"]
                self.qlog(f"✔ Remote render complete: total {m['total_s']:.1f}s = upload {m['upload_s']:.1f}s + "
                          f"encode {m['encode_s']:.1f}s + download {m['download_s']:.1f}s  "
                          f"(network overhead {100 * (m['upload_s'] + m['download_s']) / m['total_s']:.0f}%)")
                if m.get("gpu"):
                    self.qlog(f"   GPU util avg/max {m['gpu']['avg_util']}/{m['gpu']['max_util']}% | "
                              f"VRAM peak {m['gpu']['max_mem_mb']:.0f} MB")
                if self.t_local:
                    self.qlog(f"   Speedup vs local run: {self.t_local / self.t_remote:.2f}x")
                self.q.put(("progress", "done", 100, "output saved"))
                self.q.put(("finished", "done"))
            except Cancelled:
                self.qlog("Job cancelled.")
                self.q.put(("finished", "cancelled"))
            except (RenderError, OSError) as e:
                self.qlog(f"✖ Render failed: {e}")
                self.q.put(("finished", "failed"))
        threading.Thread(target=work, daemon=True).start()

    def start_local(self):
        v = self._validate()
        if not v:
            return
        src, dst, st = v
        base, ext = os.path.splitext(dst)
        self.set_busy(True)
        self.btn_cancel.configure(state="disabled")
        self.qlog("Starting LOCAL render (libx264 on this machine)…")

        def work():
            try:
                r = local_encode(src, base + "_local" + ext, st,
                                 on_progress=lambda p: self.q.put(("progress", "local", p, "encoding locally")))
                self.t_local = r["seconds"]
                self.qlog(f"✔ Local render took {r['seconds']:.1f}s"
                          + (f" (avg CPU {r['cpu_avg']:.0f}%)" if r["cpu_avg"] is not None else ""))
                if self.t_remote:
                    self.qlog(f"   Speedup of remote vs local: {self.t_local / self.t_remote:.2f}x")
            except Exception as e:
                self.qlog(f"✖ Local render failed: {e}")
            self.q.put(("finished", "idle"))
        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        if self.client:
            self.qlog("Cancelling…")
            threading.Thread(target=self.client.cancel, daemon=True).start()


if __name__ == "__main__":
    App().mainloop()
