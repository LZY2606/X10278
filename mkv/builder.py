"""Deterministic synthetic Matroska/WebM fixtures (the domain case pack data).

The baseline file exercises, in one small byte stream: an unknown-size
Segment, per-track timebase difference (TrackTimecodeScale), Xiph and fixed
lacing, a BlockGroup carrying DiscardPadding, and two CuePoints.

Each named variant changes exactly ONE direct source fact relative to the
baseline, so the counter-example archive can verify that the impact of a
single change stays inside the correct derivation scope.
"""
from __future__ import annotations

import struct

from .ebml import encode_vint, unknown_size_bytes

TIMECODE_SCALE_NS = 1_000_000  # 1 ms ticks
TRACK1_UID = 111
TRACK2_UID = 222
TRACK1_DEFAULT_DURATION_NS = 40_000_000
TRACK2_TIMECODE_SCALE = 1.001  # NTSC-style pull, differs from track 1
DISCARD_PADDING_NS = 5_000_000
GAP_BYTES = bytes(((i * 37 + 11) % 256) for i in range(64))
# first byte is 0x0b -> not a valid element id start for our table, so the
# parser must treat the region as a corrupted gap and re-sync.


def _uint(id_hex: str, value: int) -> bytes:
    payload = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    return bytes.fromhex(id_hex) + encode_vint(len(payload)) + payload


def _int(id_hex: str, value: int, length: int = 4) -> bytes:
    payload = value.to_bytes(length, "big", signed=True)
    return bytes.fromhex(id_hex) + encode_vint(len(payload)) + payload


def _float(id_hex: str, value: float) -> bytes:
    payload = struct.pack(">d", value)
    return bytes.fromhex(id_hex) + encode_vint(len(payload)) + payload


def _str(id_hex: str, value: str) -> bytes:
    payload = value.encode("ascii")
    return bytes.fromhex(id_hex) + encode_vint(len(payload)) + payload


def _master(id_hex: str, payload: bytes, unknown: bool = False,
            unknown_len: int = 1) -> bytes:
    if unknown:
        return bytes.fromhex(id_hex) + unknown_size_bytes(unknown_len) + payload
    return bytes.fromhex(id_hex) + encode_vint(len(payload)) + payload


def _block(track: int, rel_timecode: int, frames: list[bytes],
           lacing: str = "none", keyframe: bool = True,
           lace_sizes: list[int] | None = None) -> bytes:
    """Encode a (Simple)Block payload.

    rel_timecode is written as a *signed* big-endian int16 exactly as the
    Matroska spec requires; callers may pass out-of-range raw values via
    ``raw_rel`` semantics by masking to 16 bits.
    """
    header = encode_vint(track)
    header += (rel_timecode & 0xFFFF).to_bytes(2, "big")
    if lacing == "none":
        flags = 0x80 if keyframe else 0x00
        return header + bytes([flags]) + b"".join(frames)
    count = len(frames)
    if lacing == "xiph":
        flags = (0x80 if keyframe else 0x00) | 0x02
        body = bytes([count - 1])
        sizes = lace_sizes if lace_sizes is not None else [len(f) for f in frames[:-1]]
        for size in sizes:
            q, r = divmod(size, 255)
            body += b"\xff" * q + bytes([r])
        return header + bytes([flags]) + body + b"".join(frames)
    if lacing == "fixed":
        flags = (0x80 if keyframe else 0x00) | 0x04
        return header + bytes([flags]) + bytes([count - 1]) + b"".join(frames)
    raise ValueError(lacing)


def _simple_block(track: int, rel: int, frames, lacing="none",
                  keyframe=True, lace_sizes=None) -> bytes:
    payload = _block(track, rel, frames, lacing, keyframe, lace_sizes)
    return bytes.fromhex("A3") + encode_vint(len(payload)) + payload


def _block_group(track: int, rel: int, frames, lacing="fixed",
                 discard_padding_ns: int | None = None) -> bytes:
    payload = _block(track, rel, frames, lacing, keyframe=True)
    inner = bytes.fromhex("A1") + encode_vint(len(payload)) + payload
    if discard_padding_ns is not None:
        inner += _int("75A2", discard_padding_ns, length=4)
    return _master("A0", inner)


def _ebml_header() -> bytes:
    payload = (
        _uint("4286", 1) + _uint("42F7", 1) + _uint("42F2", 4)
        + _uint("42F3", 8) + _str("4282", "webm")
        + _uint("4287", 4) + _uint("4285", 2)
    )
    return _master("1A45DFA3", payload)


def _info() -> bytes:
    return _master("1549A966", _uint("2AD7B1", TIMECODE_SCALE_NS))


def _tracks(track2_scale: float) -> bytes:
    t1 = (
        _uint("D7", 1) + _uint("73C5", TRACK1_UID) + _uint("83", 1)
        + _str("86", "V_AV1")
        + _uint("23E383", TRACK1_DEFAULT_DURATION_NS)
        + _float("23314F", 1.0)
    )
    t2 = (
        _uint("D7", 2) + _uint("73C5", TRACK2_UID) + _uint("83", 2)
        + _str("86", "A_OPUS")
        + _float("23314F", track2_scale)
    )
    return _master("1654AE6B", _master("AE", t1) + _master("AE", t2))


def _cluster1(discard_padding_ns: int) -> bytes:
    payload = _uint("E7", 0)
    payload += _simple_block(1, 0, [b"\xaa" * 8])
    payload += _simple_block(2, 0, [b"\xb1" * 5, b"\xb2" * 7], lacing="xiph",
                             keyframe=False)
    payload += _block_group(1, 40, [b"\xc1" * 4, b"\xc2" * 4], lacing="fixed",
                            discard_padding_ns=discard_padding_ns)
    return _master("1F43B675", payload)


def _cluster2(unknown_size: bool, overflow_rel: int | None,
              lacing_corrupt: bool) -> bytes:
    payload = _uint("E7", 1000)
    payload += _simple_block(1, 0, [b"\xd0" * 6])
    if lacing_corrupt:
        # declared lace sizes (4 + 250) exceed the actual payload: the parser
        # must refuse to guess frame boundaries.
        payload += _simple_block(2, 5, [b"\xe1" * 4, b"\xe2" * 3, b"\xe3" * 3],
                                 lacing="xiph", keyframe=False,
                                 lace_sizes=[4, 250])
    else:
        payload += _simple_block(2, 5, [b"\xe1" * 4, b"\xe2" * 6, b"\xe3" * 3],
                                 lacing="xiph", keyframe=False)
    payload += _simple_block(1, overflow_rel if overflow_rel is not None else 40,
                             [b"\xd1" * 6])
    return _master("1F43B675", payload, unknown=unknown_size)


def _cues(cluster_positions: list[int]) -> bytes:
    payload = b""
    for i, (cue_time, pos) in enumerate(zip([0, 1000], cluster_positions)):
        ctp = _uint("F7", 1) + _uint("F1", pos) + _uint("5378", i + 1)
        payload += _master("BB", _uint("B3", cue_time) + _master("B7", ctp))
    return _master("1C53BB6B", payload)


def build(params: dict | None = None) -> bytes:
    """Build the fixture file. ``params`` keys (default = baseline):

    unknown_cluster_size, overflow_rel, lacing_corrupt, discard_padding_ns,
    cue_mispoint_bytes, track2_scale, cluster_gap
    """
    p = {
        "unknown_cluster_size": False,
        "overflow_rel": None,
        "lacing_corrupt": False,
        "discard_padding_ns": DISCARD_PADDING_NS,
        "cue_mispoint_bytes": 0,
        "track2_scale": TRACK2_TIMECODE_SCALE,
        "cluster_gap": False,
    }
    if params:
        p.update(params)

    body = _info() + _tracks(p["track2_scale"])
    c1 = _cluster1(p["discard_padding_ns"])
    cluster1_pos = len(body)
    body += c1
    if p["cluster_gap"]:
        body += GAP_BYTES
    c2 = _cluster2(p["unknown_cluster_size"], p["overflow_rel"],
                   p["lacing_corrupt"])
    cluster2_pos = len(body)
    body += c2
    positions = [cluster1_pos, cluster2_pos]
    if p["cue_mispoint_bytes"]:
        positions[1] += p["cue_mispoint_bytes"]
    body += _cues(positions)

    segment = _master("18538067", body, unknown=True, unknown_len=8)
    return _ebml_header() + segment


# --- named variants: each flips exactly one direct source fact -------------

VARIANTS = {
    "baseline": {},
    # cluster 2 written with unknown size (streaming style)
    "unknown_length": {"unknown_cluster_size": True},
    # block relative timecode raw 0x8000 -> must be read as signed -32768
    "rel_tc_overflow": {"overflow_rel": -32768},
    # xiph lace sizes sum beyond the payload -> frames indeterminate
    "lacing_corrupt": {"lacing_corrupt": True},
    # discard padding grows past the block duration
    "discard_padding_overrun": {"discard_padding_ns": 55_000_000},
    # second cue points 7 bytes past the real cluster start
    "cue_mispoint": {"cue_mispoint_bytes": 7},
    # track 2 timebase changes (1.001 -> 1.25)
    "track_timebase": {"track2_scale": 1.25},
    # 64 junk bytes inserted between cluster 1 and cluster 2
    "cluster_gap": {"cluster_gap": True},
}


def build_variant(name: str) -> bytes:
    if name not in VARIANTS:
        raise KeyError(f"unknown variant {name!r}; have {sorted(VARIANTS)}")
    return build(VARIANTS[name])
