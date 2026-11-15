"""G.711 helpers and the conference mixer.

Every viewer (one RTSP connection from go2rtc per browser session) may send its
microphone on the backchannel. The mixer:
  * sums all microphones into the audio sent to the door station (SIP), and
  * adds the *other* viewers' microphones to the door audio each viewer hears,
    so two phones and the person at the gate all hear each other (nobody hears
    their own voice back).
"""
from __future__ import annotations

from array import array
from collections import deque


def _alaw_decode(a: int) -> int:
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (seg - 1)
    return t if a & 0x80 else -t


def _ulaw_decode(u: int) -> int:
    u = ~u & 0xFF
    t = (((u & 0x0F) << 3) + 0x84) << ((u & 0x70) >> 4)
    return (0x84 - t) if u & 0x80 else (t - 0x84)


def _alaw_encode(pcm: int) -> int:
    mask = 0xD5 if pcm >= 0 else 0x55
    pcm = pcm if pcm >= 0 else -pcm - 1
    pcm = min(pcm, 0x7FFF)
    seg = 0
    for seg, end in enumerate((0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)):
        if pcm <= end:
            break
    aval = (pcm >> 4) & 0x0F if seg < 2 else (pcm >> (seg + 3)) & 0x0F
    return ((seg << 4) | aval) ^ mask


ALAW_DEC = [_alaw_decode(i) for i in range(256)]
ULAW_DEC = [_ulaw_decode(i) for i in range(256)]
# index: (sample >> 3) + 4096 for sample in -32768..32767 (13-bit resolution is all A-law keeps)
_ALAW_ENC = bytes(_alaw_encode((i - 4096) << 3) for i in range(8192))

MAX_BUFFER = 4000  # 0.5 s at 8 kHz; older audio is dropped to keep latency low


def decode(payload: bytes, pt: int) -> array:
    table = ULAW_DEC if pt == 0 else ALAW_DEC
    return array("h", [table[b] for b in payload])


def encode_alaw(samples) -> bytes:
    enc = _ALAW_ENC
    return bytes(enc[(max(-32768, min(32767, s)) >> 3) + 4096] for s in samples)


class Speaker:
    """Microphone samples of one viewer, read separately for the door and for the other viewers."""

    def __init__(self) -> None:
        self.to_door: deque[int] = deque(maxlen=MAX_BUFFER)
        self.to_peers: deque[int] = deque(maxlen=MAX_BUFFER)
        self.packets = 0

    def feed(self, samples: array) -> None:
        self.packets += 1
        self.to_door.extend(samples)
        self.to_peers.extend(samples)

    @staticmethod
    def take(buf: deque, n: int) -> list[int] | None:
        if not buf:
            return None
        k = min(n, len(buf))
        out = [buf.popleft() for _ in range(k)]
        if k < n:
            out.extend([0] * (n - k))
        return out


def mix_into(base: list[int], parts: list[list[int]]) -> list[int]:
    out = list(base)
    for part in parts:
        for i, s in enumerate(part):
            out[i] += s
    return out
