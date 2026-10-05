<<<<<<< HEAD
# Distributed Task Offloading & Remote GPU Rendering System
**CSC-334 Parallel and Distributed Computing** — offload heavy video transcoding from a weak client laptop to a remote worker with an NVIDIA GPU (FFmpeg + NVENC) over a direct LAN link.

```
 CLIENT LAPTOP                                         WORKER NODE (GPU)
 CustomTkinter GUI ─► RenderClient ─ TCP :5001 ─►  server.py ─► job queue ─► ffmpeg -c:v h264_nvenc
 progress bar / logs ◄─ PROGRESS stream (2 Hz) ◄──┘                    └► SHA-256 verified download ─► client
```

## Repository layout
| Path | Purpose |
|---|---|
| `server/server.py` | Headless GPU daemon: auth, upload, job queue, NVENC encode, progress, download |
| `server/protocol.py` | Wire protocol (identical copy in `client/`) — documented at the top of the file |
| `client/gui.py` | CustomTkinter desktop app (file picker, resolution/bitrate/preset, IP config, live log) |
| `client/net_client.py` | Handshake, latency check, upload/download, retries & reconnect logic |
| `client/cli.py` | Headless client (`check`, `render`) |
| `client/benchmark.py`, `make_test_videos.py`, `local_render.py` | Task 5 tooling |
| `docs/REPORT.md` | Technical report (fill in with your measured numbers) |

## 1. Setup
**Worker (server) node** — Windows/Linux with an NVIDIA GPU (4 GB+):
1. Install the latest NVIDIA driver; check `nvidia-smi`.
2. Install an FFmpeg build with NVENC (Windows: gyan.dev/BtbN "full" build; Linux: `sudo apt install ffmpeg`, or BtbN static build). Verify: `ffmpeg -encoders | grep nvenc` shows `h264_nvenc`.
3. Python 3.9+. No pip packages needed.

**Client laptop**: Python 3.9+, FFmpeg (only for the local-render comparison/benchmark), then
```bash
cd client && pip install -r requirements.txt
```

## 2. Network configuration (static IPs)
Connect both machines with a CAT6 cable (or put both on one dedicated Wi-Fi/hotspot subnet). Use the same /24 subnet, no gateway needed.

| Node | IP | Mask |
|---|---|---|
| Server | `192.168.1.1` | `255.255.255.0` |
| Client | `192.168.1.2` | `255.255.255.0` |

**Windows** (admin PowerShell; find the adapter name with `Get-NetAdapter`):
```powershell
netsh interface ip set address "Ethernet" static 192.168.1.1 255.255.255.0   # server
netsh interface ip set address "Ethernet" static 192.168.1.2 255.255.255.0   # client
```
**Linux** (NetworkManager): `nmcli con mod "Wired connection 1" ipv4.method manual ipv4.addresses 192.168.1.1/24 && nmcli con up "Wired connection 1"` (use `.2` on the client).
**macOS**: System Settings → Network → Ethernet → Details → TCP/IP → *Manually*.

Then:
1. Verify: `ping 192.168.1.1` from the client.
2. Open the firewall on the **server** for TCP 5001:
   `netsh advfirewall firewall add rule name="GPU Offload" dir=in action=allow protocol=TCP localport=5001` (Windows) / `sudo ufw allow 5001/tcp` (Linux).
3. Windows: set the Ethernet network profile to *Private*.

## 3. Running
**Server:**
```bash
cd server
python server.py --host 0.0.0.0 --port 5001 --token mysecret
# startup log must say:  mode: nvenc   (if it says CPU mode, FFmpeg lacks h264_nvenc or the driver is missing)
```
Options: `--workers N` (concurrent encodes), `--workdir`, `--max-gb`, `--timeout`, `--no-cuda-decode` (if `-hwaccel cuda` fails on an input), `--cpu` (testing without a GPU). Token can also be set via `RENDER_TOKEN`.

**Client GUI:**
```bash
cd client && python gui.py
```
Enter server IP and token → **Test Connection** (handshake + 5-ping latency probe + GPU info) → **Browse** a video → choose resolution / bitrate / preset → **Render on Remote GPU**. Watch upload → queue → encode → download in the progress bar and log. **Render Locally** runs the same job on the laptop and the log prints the speedup.

**Headless:** `python cli.py check --host 192.168.1.1 --token mysecret` and `python cli.py render in.mp4 out.mp4 --host 192.168.1.1 --token mysecret --height 1080 --bitrate 8000 --preset p4`.

## 4. How the requirements are met
| Task | Implementation |
|---|---|
| **1 Handshake** | TCP with length-prefixed JSON; server sends a nonce, client answers with HMAC-SHA256(token, nonce) (secret never on the wire); protocol-version check; `check_worker()` measures connect time and min/avg/max RTT over N pings and reports GPU/NVENC availability *before* a job is submitted; errors (refused, timeout, auth failure) are surfaced in the GUI. |
| **2 GPU daemon** | `server.py`: threaded listener, FIFO job queue with worker threads, strictly validated settings (no shell, argument lists only), `ffmpeg -hwaccel cuda … -c:v h264_nvenc -preset pN -b:v …`; `nvidia-smi` sampled each second for utilization/VRAM; CPU `libx264` fallback for testing only. |
| **3 GUI** | CustomTkinter dark UI: file picker, resolution, bitrate, preset, server IP/port/token, test button, progress bar, live log terminal, cancel; network work in threads, UI updated through a queue. |
| **4 Robustness** | Progress parsed from `ffmpeg -progress` and streamed at 2 Hz; SHA-256 verified on upload (server) and download (client) with automatic retry; socket timeouts; on a dropped connection the client reconnects with back-off and **re-attaches (`WATCH`) to the still-running job** instead of restarting; failed ffmpeg runs report the error tail; cancel kills the encode; temp files cleaned up. |
| **5 Benchmark** | `benchmark.py` → CSV + table + chart: local vs remote time, speedup (total and encode-only), upload/download time and overhead %, link throughput, GPU util/VRAM, local CPU %. |

## 5. Benchmarking (Task 5)
```bash
cd client
python make_test_videos.py                       # synthetic inputs in testdata/
python benchmark.py --host 192.168.1.1 --token mysecret testdata/*.mp4 --heights 720 1080 --repeat 3
```
Output goes to `results/benchmark.csv|md|speedup.png`. Copy the numbers into `docs/REPORT.md`. If the client has its own weak GPU, add `--local-nvenc` to compare against it as well as CPU.

## 6. Demo (add before submission)
Capture and place in `docs/screenshots/`: GUI after *Test Connection*; progress bar mid-render; server console showing `mode: nvenc`; `nvidia-smi` during an encode; the benchmark chart. Record a short GIF (ScreenToGif / `peek`) of a full render, and a second one unplugging/replugging the cable mid-encode to show recovery.

![GUI](docs/screenshots/gui.png)
![Progress](docs/screenshots/progress.gif)

## Troubleshooting
- *Connection refused/timeout*: wrong IP, server not running, firewall, or adapters on different subnets.
- *`authentication failed`*: tokens differ.
- *Server says CPU mode*: `ffmpeg -encoders | grep nvenc` is empty → install a NVENC-enabled build / update the driver.
- *`-hwaccel cuda` errors / unsupported codec*: start the server with `--no-cuda-decode`.
- *Preset errors on old FFmpeg (<4.4)*: p1–p7 need FFmpeg ≥ 4.4 and a recent driver.
- Security note: the token authenticates the client but traffic is not encrypted — use only on a trusted/isolated LAN.
=======
<img width="1366" height="728" alt="WhatsApp Image 2026-10-04 at 10 06 13 PM (3)" src="https://github.com/user-attachments/assets/c53502ee-84ac-4ab8-98db-387c09730c2b" />
<img width="381" height="234" alt="WhatsApp Image 2026-10-04 at 10 06 13 PM (2)" src="https://github.com/user-attachments/assets/24db587b-b4c9-40e6-9a14-375bc9d73136" />
<img width="1366" height="728" alt="WhatsApp Image 2026-10-04 at 10 06 13 PM (1)" src="https://github.com/user-attachments/assets/eb962d67-b4c3-412d-8c14-c9b0120c2a40" />
<img width="1366" height="728" alt="WhatsApp Image 2026-10-04 at 10 06 13 PM" src="https://github.com/user-attachments/assets/25903650-eb38-4487-adc9-6fbc8b439180" />
<img width="1350" height="766" alt="WhatsApp Image 2026-10-04 at 10 06 13 PM (4)" src="https://github.com/user-attachments/assets/d4314989-8eda-4d67-b191-51630083813e" />
>>>>>>> a12cd90d111034915a167543cca93d72b4aba6cc
