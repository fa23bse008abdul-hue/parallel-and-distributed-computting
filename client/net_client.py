"""Client-side networking: handshake, latency check, upload, progress streaming, download.

Robustness features
  * HMAC challenge/response handshake + protocol version check
  * SHA-256 verification of the upload (server) and of the rendered download (client), with retries
  * socket timeouts everywhere; on a drop the client reconnects with back-off and re-attaches to the
    still-running job (WATCH) instead of restarting the render
"""
import os
import platform
import socket
import time

from protocol import (VERSION, DEFAULT_PORT, ProtocolError, send_msg, recv_msg, make_mac,
                      sha256_file, send_file, recv_file)


class RenderError(Exception):
    pass


class AuthError(RenderError):
    pass


class Cancelled(Exception):
    pass


NET_ERRORS = (OSError, ProtocolError)  # socket.timeout / ConnectionError are OSError subclasses


class RenderClient:
    def __init__(self, host, port=DEFAULT_PORT, token="changeme", log=print, on_progress=None,
                 io_timeout=15, max_retries=5):
        self.host, self.port, self.token = host, int(port), token
        self.log, self.on_progress = log, on_progress
        self.io_timeout, self.max_retries = io_timeout, max_retries
        self.sock, self.caps, self.job_id, self._cancelled = None, {}, None, False

    # ------------------------------------------------------------------ connection / handshake
    def _emit(self, phase, pct, info=""):
        if self.on_progress:
            self.on_progress(phase, pct, info)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def connect(self, timeout=5):
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(self.io_timeout)
            ch = recv_msg(s)
            if ch.get("type") != "CHALLENGE":
                raise ProtocolError("expected CHALLENGE from server")
            if ch.get("version") != VERSION:
                raise RenderError(f"protocol version mismatch (server {ch.get('version')}, client {VERSION})")
            send_msg(s, {"type": "HELLO", "mac": make_mac(self.token, ch["nonce"]),
                         "client": platform.node()})
            ack = recv_msg(s)
        except BaseException:
            s.close()
            raise
        if ack.get("type") == "ERROR":
            s.close()
            raise AuthError(ack.get("message", "rejected"))
        self.sock, self.caps = s, ack
        return ack

    def ping(self, count=5):
        rtts = []
        for _ in range(count):
            t = time.perf_counter()
            send_msg(self.sock, {"type": "PING"})
            if recv_msg(self.sock).get("type") != "PONG":
                raise ProtocolError("expected PONG")
            rtts.append((time.perf_counter() - t) * 1000)
        return {"min": min(rtts), "avg": sum(rtts) / len(rtts), "max": max(rtts), "count": count}

    def check_worker(self, pings=5):
        """Availability check before job submission: connect + authenticate + latency probe."""
        t = time.perf_counter()
        caps = self.connect()
        connect_ms = (time.perf_counter() - t) * 1000
        rtt = self.ping(pings)
        self.close()
        return {"connect_ms": connect_ms, "rtt": rtt, "caps": caps}

    # ------------------------------------------------------------------ job lifecycle
    def cancel(self):
        self._cancelled = True
        jid, s = self.job_id, self.sock
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if jid:
            try:
                c = RenderClient(self.host, self.port, self.token, log=lambda *_: None)
                c.connect()
                send_msg(c.sock, {"type": "CANCEL", "job_id": jid})
                recv_msg(c.sock)
                c.close()
            except Exception:
                pass

    def _retry_wait(self, what, attempt, err):
        if self._cancelled:
            raise Cancelled()
        if attempt >= self.max_retries:
            raise RenderError(f"{what} failed after {attempt} attempts: {err}")
        wait = min(2 * attempt, 10)
        self.log(f"[net] {what}: {err} - retry {attempt}/{self.max_retries} in {wait}s")
        time.sleep(wait)

    def render(self, src, dst, settings):
        """Upload `src`, wait for the remote render and download the result to `dst`. Returns metrics."""
        self._cancelled, self.job_id = False, None
        size = os.path.getsize(src)
        sha = sha256_file(src)
        m = {"input_bytes": size}
        t_start = time.perf_counter()

        # ---- 1. upload (checksummed, retried)
        attempt = 0
        while True:
            attempt += 1
            try:
                self.connect()
                self.log(f"[net] connected to {self.host}:{self.port} "
                         f"(worker mode: {self.caps.get('mode')}, GPU: {self.caps.get('gpu')})")
                send_msg(self.sock, {"type": "SUBMIT", "filename": os.path.basename(src), "size": size,
                                     "sha256": sha, "settings": settings})
                r = recv_msg(self.sock)
                if r.get("type") != "READY":
                    raise RenderError(r.get("message", "job rejected"))
                self.job_id = r["job_id"]
                self.log(f"[net] job {self.job_id} accepted - uploading {size / 1e6:.1f} MB")
                t_up = time.perf_counter()
                send_file(self.sock, src, lambda n: self._emit("upload", 100 * n / size,
                                                               f"{n / 1e6:.1f}/{size / 1e6:.1f} MB"))
                r = recv_msg(self.sock)
                if r.get("type") == "UPLOAD_OK":
                    m["upload_s"] = time.perf_counter() - t_up
                    self.log(f"[net] upload verified by server (SHA-256 ok) in {m['upload_s']:.1f}s")
                    break
                raise ProtocolError("server reported checksum mismatch")
            except NET_ERRORS as e:
                self._retry_wait("upload", attempt, e)

        # ---- 2. follow progress (re-attach after drops)
        t_wait = time.perf_counter()
        done = self._follow()
        m["remote_wait_s"] = time.perf_counter() - t_wait
        m.update(encode_s=done["encode_s"], gpu=done.get("gpu", {}), mode=done.get("mode"))

        # ---- 3. download + verify
        t_dl = time.perf_counter()
        self._download(done, dst)
        m["download_s"] = time.perf_counter() - t_dl
        m["output_bytes"] = os.path.getsize(dst)
        m["total_s"] = time.perf_counter() - t_start
        try:
            send_msg(self.sock, {"type": "CLEANUP", "job_id": self.job_id})
            recv_msg(self.sock)
            send_msg(self.sock, {"type": "BYE"})
        except NET_ERRORS:
            pass
        self.close()
        return m

    def _follow(self):
        attempt = 0
        while True:
            try:
                if attempt > 0:
                    self.connect()
                    self.log("[net] reconnected - re-attaching to running job")
                    send_msg(self.sock, {"type": "WATCH", "job_id": self.job_id})
                while True:
                    msg = recv_msg(self.sock)
                    t = msg.get("type")
                    if t == "PROGRESS":
                        attempt = 0
                        st = msg["status"]
                        if st == "queued":
                            self._emit("queued", 0, f"queued (position {msg['queue_pos']})")
                        else:
                            extra = f" | {msg['fps']:.0f} fps" if msg.get("fps") else ""
                            extra += f" | {msg['speed']}" if msg.get("speed") else ""
                            self._emit("encode", msg["progress"], f"encoding{extra}")
                    elif t == "DONE":
                        return msg
                    elif t == "FAILED":
                        raise RenderError(f"remote render failed: {msg.get('error')}")
                    elif t == "ERROR":
                        raise RenderError(msg.get("message"))
            except NET_ERRORS as e:
                attempt += 1
                self.close()
                self._retry_wait("progress stream", attempt, e)

    def _download(self, done, dst):
        attempt = 0
        part = dst + ".part"
        while True:
            try:
                if not self.sock:
                    self.connect()
                send_msg(self.sock, {"type": "FETCH", "job_id": self.job_id})
                hdr = recv_msg(self.sock)
                if hdr.get("type") != "FILE":
                    raise RenderError(hdr.get("message", "fetch refused"))
                self.log(f"[net] downloading result ({hdr['size'] / 1e6:.1f} MB)")
                recv_file(self.sock, part, hdr["size"], lambda n: self._emit(
                    "download", 100 * n / max(hdr["size"], 1), f"{n / 1e6:.1f}/{hdr['size'] / 1e6:.1f} MB"))
                if sha256_file(part) == hdr["sha256"] == done["sha256"]:
                    os.replace(part, dst)
                    self.log("[net] download verified (SHA-256 ok)")
                    return
                attempt += 1
                self.log("[net] checksum mismatch on download - refetching")
                self._retry_wait("download", attempt, "checksum mismatch")
            except NET_ERRORS as e:
                attempt += 1
                self.close()
                self._retry_wait("download", attempt, e)
