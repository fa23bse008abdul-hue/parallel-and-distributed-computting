"""Wire protocol shared by client and server (identical copy lives in client/ and server/).

Framing: every control message is  [4-byte big-endian length][UTF-8 JSON].
Bulk file data is sent as raw bytes immediately after a message that announces its size.

Session flow
  S->C CHALLENGE {nonce, version}
  C->S HELLO     {mac = HMAC-SHA256(token, nonce)}
  S->C HELLO_ACK {gpu/ffmpeg capabilities}   | ERROR
  C<->S PING/PONG                            (latency probe)
  C->S SUBMIT {filename,size,sha256,settings}
  S->C READY {job_id}  -> C sends <size> raw bytes -> S verifies SHA-256
  S->C UPLOAD_OK | UPLOAD_BAD
  S->C PROGRESS {...} every 0.5 s  ...  DONE {...} | FAILED {error}
  C->S FETCH {job_id} -> S->C FILE {size,sha256} + raw bytes
  C->S WATCH {job_id}   (re-attach to a running job after a reconnect)
  C->S CANCEL {job_id} / CLEANUP {job_id} / BYE
"""
import hashlib
import hmac
import json
import os
import struct

VERSION = 1
DEFAULT_PORT = 5001
CHUNK = 1 << 20
MAX_MSG = 1 << 20


class ProtocolError(Exception):
    pass


def send_msg(sock, obj):
    data = json.dumps(obj).encode()
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(CHUNK, n - len(buf)))
        if not chunk:
            raise ConnectionError("peer closed the connection")
        buf += chunk
    return bytes(buf)


def recv_msg(sock):
    (n,) = struct.unpack("!I", recv_exact(sock, 4))
    if n > MAX_MSG:
        raise ProtocolError(f"message too large ({n} bytes)")
    try:
        return json.loads(recv_exact(sock, n).decode())
    except ValueError as e:
        raise ProtocolError(f"bad JSON: {e}")


def make_mac(token, nonce):
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def send_file(sock, path, progress=None):
    sent = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            sock.sendall(b)
            sent += len(b)
            if progress:
                progress(sent)


def recv_file(sock, path, size, progress=None):
    remaining = size
    with open(path, "wb") as f:
        while remaining > 0:
            chunk = sock.recv(min(CHUNK, remaining))
            if not chunk:
                raise ConnectionError("connection lost during file transfer")
            f.write(chunk)
            remaining -= len(chunk)
            if progress:
                progress(size - remaining)
