"""Deterministic in-repo builders for Matroska/EBML fixture bytes.

The case pack never depends on external media files: every fixture is
synthesised here so tests and the offline demo are fully self-contained.
"""
from __future__ import annotations

import struct

from . import ebml


def vint_encode(value: int, length: int | None = None) -> bytes:
    """Encode a size vint (marker bit set). Picks the shortest length
    unless one is forced."""
    if length is None:
        for cand in range(1, 9):
            if value <= (1 << (7 * cand)) - 2:
                length = cand
                break
        else:
            raise ValueError("value too large for vint")
    if value > (1 << (7 * length)) - 2:
        raise ValueError(f"value {value} does not fit in {length}-byte vint")
    marker = 1 << (7 * length)
    return ((marker | value).to_bytes(length + (7 * length) // 8 and length or length, "big")
            if False else (marker | value).to_bytes(length, "big"))


def unknown_size(length: int = 8) -> bytes:
    return ((1 << (7 * length)) - 1 | (1 << (7 * length))).to_bytes(length, "big")


def element(eid: int, payload: bytes, size_length: int | None = None,
            unknown: bool = False) -> bytes:
    id_len = 1 if eid < 0x100 else 2 if eid < 0x10000 else 3 if eid < 0x1000000 else 4
    id_bytes = eid.to_bytes(id_len, "big")
    if unknown:
        return id_bytes + unknown_size(8) + payload
    return id_bytes + vint_encode(len(payload), size_length) + payload


def uint_el(eid: int, value: int, size: int | None = None) -> bytes:
    if size is None:
        size = max(1, (value.bit_length() + 7) // 8)
    return element(eid, value.to_bytes(size, "big"))


def int_el(eid: int, value: int, size: int = 1) -> bytes:
    return element(eid, value.to_bytes(size, "big", signed=True))


def float_el(eid: int, value: float) -> bytes:
    return element(eid, struct.pack(">d", value))


def str_el(eid: int, value: str) -> bytes:
    return element(eid, value.encode("utf-8"))


def block_payload(track: int, rel_timecode: int, flags: int,
                  frames: list[bytes], lacing: str = "none",
                  corrupt_lace: bool = False) -> bytes:
    """Build a Block/SimpleBlock payload. rel_timecode is a signed 16-bit
    value; lacing in {"none","xiph","fixed","ebml"}."""
    head = vint_encode(track) + rel_timecode.to_bytes(2, "big", signed=True)
    if lacing == "none":
        assert len(frames) == 1
        return head + bytes([flags]) + frames[0]
    n = len(frames)
    if lacing == "fixed":
        head += bytes([flags | 0x04, n - 1])
        return head + b"".join(frames)
    if lacing == "xiph":
        head += bytes([flags | 0x02, n - 1])
        lace = b""
        for f in frames[:-1]:
            q, r = divmod(len(f), 255)
            lace += b"\xff" * q + bytes([r])
        if corrupt_lace:
            # 0xFF run before the real table: sizes run into the frame data
            # and overrun the payload -> parser must refuse to guess
            lace = b"\xff\xff\xff" + lace
        return head + lace + b"".join(frames)
    if lacing == "ebml":
        head += bytes([flags | 0x06, n - 1])
        lace = vint_encode(len(frames[0]))
        prev = len(frames[0])
        for f in frames[1:-1]:
            diff = len(f) - prev
            bias = (1 << 6) - 1  # 2-byte signed vint bias would be larger; use 1-byte
            lace += vint_encode(diff + bias, 1)
            prev = len(f)
        return head + lace + b"".join(frames)
    raise ValueError(lacing)


def ebml_header() -> bytes:
    body = (uint_el(0x4286, 1) + uint_el(0x42F7, 1) + uint_el(0x42F2, 4)
            + uint_el(0x42F3, 8) + str_el(0x4282, "matroska")
            + uint_el(0x4287, 4) + uint_el(0x4285, 2))
    return element(0x1A45DFA3, body)


def info(timecode_scale: int = 1_000_000) -> bytes:
    return element(0x1549A966, uint_el(0x2AD7B1, timecode_scale, 4))


def track_entry(number: int, uid: int, codec: str, track_type: int = 1,
                default_duration: int | None = None,
                timecode_scale: float | None = None) -> bytes:
    body = (uint_el(0xD7, number) + uint_el(0x73C5, uid, 4)
            + uint_el(0x83, track_type) + str_el(0x86, codec))
    if default_duration is not None:
        body += uint_el(0x23E383, default_duration, 4)
    if timecode_scale is not None:
        body += float_el(0x23314F, timecode_scale)
    return element(0xAE, body)


def simple_block(track: int, rel: int, frames: list[bytes], keyframe: bool = True,
                 lacing: str = "none", corrupt_lace: bool = False) -> bytes:
    flags = 0x80 if keyframe else 0x00
    return element(0xA3, block_payload(track, rel, flags, frames, lacing, corrupt_lace))


def block_group(track: int, rel: int, frames: list[bytes],
                discard_padding_ns: int | None = None,
                block_duration_ticks: int | None = None,
                lacing: str = "none", corrupt_lace: bool = False) -> bytes:
    blk = element(0xA1, block_payload(track, rel, 0x00, frames, lacing, corrupt_lace))
    body = blk
    if block_duration_ticks is not None:
        body += uint_el(0x9B, block_duration_ticks, 4)
    if discard_padding_ns is not None:
        body += int_el(0x75A2, discard_padding_ns, 8)
    return element(0xA0, body)


def cluster(timecode: int | None, blocks: list[bytes], unknown: bool = False) -> bytes:
    body = b""
    if timecode is not None:
        body += uint_el(0xE7, timecode, 4)
    body += b"".join(blocks)
    return element(0x1F43B675, body, unknown=unknown)


def cue_point(time: int, track: int, cluster_pos: int, block_number: int = 1) -> bytes:
    pos = (uint_el(0xF7, track) + uint_el(0xF1, cluster_pos, 4)
           + uint_el(0x5378, block_number))
    return element(0xBB, uint_el(0xB3, time, 4) + element(0xB7, pos))


def segment(payload: bytes, unknown: bool = True) -> bytes:
    return element(0x18538067, payload, unknown=unknown)


def sample_file(*, unknown_size_cluster_b: bool = False,
                audio_rel: int = 60, corrupt_lace: bool = False,
                discard_padding_ns: int = 5_000_000,
                cue2_target: str = "mispoint",
                track2_scale: float = 2.0,
                cluster_c_timecode: int = 900) -> bytes:
    """Baseline sample: 2 tracks (video + audio with a different track
    timebase), 3 clusters with a mid-file gap, one BlockGroup carrying
    DiscardPadding, one Xiph-laced block, a block at the signed int16
    boundary, an unknown-size cluster, and a cue table containing one
    mispointing entry.  Keyword arguments produce the counterexample
    variants; each changes exactly one direct source."""
    parts = [ebml_header()]
    seg_payload = b""
    seg_payload += info(1_000_000)  # 1 ms ticks
    tracks = (track_entry(1, 0x1111, "V_FFV1", 1, 40_000_000)
              + track_entry(2, 0x2222, "A_PCM/INT/LIT", 2, None, track2_scale))
    seg_payload += element(0x1654AE6B, tracks)

    # cluster A @ tick 0 (known size)
    c_a = cluster(0, [
        simple_block(1, 0, [b"V0" * 20]),
        simple_block(2, 5, [b"A0" * 10], keyframe=False),
        block_group(1, 40, [b"V1" * 20], discard_padding_ns=discard_padding_ns,
                    block_duration_ticks=40),
    ])
    # cluster B @ tick 120: Xiph-laced video block (3 frames)
    c_b = cluster(120, [
        simple_block(1, 0, [b"V2" * 20, b"V3" * 20, b"V4" * 20], lacing="xiph",
                     corrupt_lace=corrupt_lace),
        simple_block(2, audio_rel, [b"A1" * 10], keyframe=False),
    ], unknown=unknown_size_cluster_b)
    # (mid-file cluster gap: no cluster covering ticks ~200..cluster_c_timecode)
    # cluster C, unknown size, boundary relative timecode
    c_c = cluster(cluster_c_timecode, [
        simple_block(1, 0, [b"V5" * 20]),
        simple_block(1, -32768, [b"V6" * 4], keyframe=False),  # int16 min
    ], unknown=True)

    seg_payload += c_a + c_b + c_c
    # cues: positions are relative to the segment payload start.
    c_a_pos = len(info(1_000_000)) + len(element(0x1654AE6B, tracks))
    c_b_pos = c_a_pos + len(c_a)
    c_c_pos = c_b_pos + len(c_b)
    if cue2_target == "mispoint":
        cue2_pos = c_c_pos + 37          # points into the middle of nowhere
    elif cue2_target == "clu0":
        cue2_pos = c_a_pos               # resolves, but time mismatches
    else:
        cue2_pos = c_c_pos
    cues = element(0x1C53BB6B,
                   cue_point(0, 1, c_a_pos)
                   + cue_point(120, 1, c_b_pos)
                   + cue_point(cluster_c_timecode, 1, cue2_pos))
    seg_payload += cues
    parts.append(segment(seg_payload, unknown=True))
    return b"".join(parts)
