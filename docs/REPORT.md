# Performance Evaluation Report — Distributed GPU Offloading
*CSC-334 · Student: ______ · Date: ______*

## 1. Test environment
| | Client (local) | Worker (remote) |
|---|---|---|
| CPU / RAM | | |
| GPU / VRAM | | |
| OS | | |
| Link | CAT6 Ethernet / Wi-Fi, negotiated speed ___ Mbps; RTT avg ___ ms (from *Test Connection*) | |
| FFmpeg / driver version | | |

Encode parameters: H.264, bitrate ___ kbps, preset p4 (libx264 `fast` locally), audio AAC 128k. Inputs: `make_test_videos.py` (480p–2160p, 20–90 s). Each point is the mean of ___ runs (`--repeat`).

## 2. Method
Local time = wall-clock `ffmpeg` on the client. Remote time = upload + queue/encode + download + checksum verification, measured by `RenderClient.render()`.
- **Speedup (end-to-end)** = T_local / T_remote_total
- **Speedup (encode-only)** = T_local / T_remote_encode
- **Network overhead** = (T_upload + T_download) / T_remote_total
- **Link throughput** = input bytes × 8 / T_upload
- **Utilization**: server `nvidia-smi` samples (GPU %, VRAM peak) during encode; client CPU % via `psutil`.

## 3. Results
Paste `results/benchmark.md` here, and include `results/speedup.png`.

## 4. Analysis (answer with your data)
1. Where does remote offloading win? Relationship between resolution and speedup.
2. Break-even: file size / duration below which transfer overhead cancels the GPU gain.
3. Effect of link type (Ethernet vs Wi-Fi) on overhead.
4. GPU utilization: is NVENC saturated, or is the transfer/decoder the bottleneck? (Try `--workers 2`.)
5. Local CPU load vs client responsiveness; thermal behaviour.
6. Limitations (unencrypted link, single-GPU queue, whole-file transfer instead of streaming) and future work (chunked pipelining of upload and encode, TLS, resumable uploads, PyTorch CUDA task type).

## 5. Conclusion
