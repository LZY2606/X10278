"""Deterministic Matroska fixture builder (source layer test material).

Builds fixtures/baseline.mkv containing every edge case of the case pack:
unknown-size Segment and Cluster, signed relative timecode extremes,
corrupted xiph lacing, DiscardPadding (both signs), a mis-pointing cue,
per-track timebase difference and a mid-file cluster gap.
"""
from __future__ import annotations

import struct


def _id_bytes(eid: int) -> bytes:
    length = (eid.bit_length() + 7) // 8
    return eid.to_bytes(length, "big")


def vsize(value: int, length: int | None = None) -> bytes:
    if length is None:
        length = 1
        while value >= (1 << (7 * length)) - 1:
            length += 1
    if value >= (1 << (7 * length)):
        raise ValueError("value does not fit requested size length")
    return (value | (1 << (7 * length))).to_bytes(length, "big")


def unknown_size(length: int = 8) -> bytes:
    return ((1 << (7 * length)) - 1 | (1 << (7 * length))).to_bytes(length, "big")


def elem(eid: int, payload: bytes, size_len: int | None = None, unknown: bool = False) -> bytes:
    head = _id_bytes(eid)
    if unknown:
        return head + unknown_size(8) + payload
    return head + vsize(len(payload), size_len) + payload


def uint_el(eid: int, value: int, size: int | None = None) -> bytes:
    if size is None:
        size = max(1, (value.bit_length() + 7) // 8)
    return elem(eid, value.to_bytes(size, "big"))


def int_el(eid: int, value: int, size: int = 8) -> bytes:
    return elem(eid, value.to_bytes(size, "big", signed=True))


def float_el(eid: int, value: float, size: int = 8) -> bytes:
    fmt = ">f" if size == 4 else ">d"
    return elem(eid, struct.pack(fmt, value))


def str_el(eid: int, text: str) -> bytes:
    return elem(eid, text.encode("utf-8"))


def block_payload(track: int, rel: int, frames: list[bytes], lacing: str = "none",
                  corrupt_xiph: bool = False) -> bytes:
    out = bytearray()
    out.append(0x80 | track)
    out += rel.to_bytes(2, "big", signed=True)
    if lacing == "none":
        out.append(0x80)
        out += frames[0]
    elif lacing == "fixed":
        out.append(0x80 | (0x02 << 1))
        out.append(len(frames) - 1)
        for frame in frames:
            out += frame
    elif lacing == "xiph":
        out.append(0x80 | (0x01 << 1))
        if corrupt_xiph:
            out.append(2)  # 3 frames declared
            out.append(10)
            out.append(10)
            out += b"A" * 15  # only 15 payload bytes for declared 20
        else:
            out.append(len(frames) - 1)
            for frame in frames[:-1]:
                size = len(frame)
                while size >= 255:
                    out.append(255)
                    size -= 255
                out.append(size)
            for frame in frames:
                out += frame
    elif lacing == "ebml":
        out.append(0x80 | (0x03 << 1))
        out.append(len(frames) - 1)
        sizes = [len(f) for f in frames]
        out += vsize(sizes[0])
        for prev, cur in zip(sizes, sizes[1:-1]):
            diff = cur - prev
            out.append(diff + 63)  # 1-byte signed vint, bias 63
        for frame in frames:
            out += frame
    else:
        raise ValueError(lacing)
    return bytes(out)


def simple_block(track: int, rel: int, frame: bytes) -> bytes:
    return elem(0xA3, block_payload(track, rel, [frame], "none"))


def block_group(track: int, rel: int, frames: list[bytes], lacing: str,
                duration_ticks: int | None, discard_padding_ns: int | None) -> bytes:
    payload = elem(0xA1, block_payload(track, rel, frames, lacing))
    if duration_ticks is not None:
        payload += uint_el(0x9B, duration_ticks)
    if discard_padding_ns is not None:
        payload += int_el(0x75A2, discard_padding_ns)
    return elem(0xA0, payload)


def build_baseline() -> bytes:
    header = elem(0x1A45DFA3, b"".join([
        uint_el(0x4286, 1), uint_el(0x42F7, 1), uint_el(0x42F2, 4), uint_el(0x42F3, 8),
        str_el(0x4282, "matroska"), uint_el(0x4287, 4), uint_el(0x4285, 2),
    ]))

    info = elem(0x1549A966, b"".join([
        uint_el(0x2AD7B1, 1_000_000),
        str_el(0x4D80, "mkvcase-builder 1.0"),
        str_el(0x5741, "mkvcase-builder 1.0"),
    ]))

    track1 = elem(0xAE, b"".join([
        uint_el(0xD7, 1), uint_el(0x73C5, 0x1111),
        uint_el(0x83, 1), str_el(0x86, "V_UNCOMPRESSED"),
        float_el(0x23314F, 1.0), uint_el(0x23E383, 40_000_000),
    ]))
    track2 = elem(0xAE, b"".join([
        uint_el(0xD7, 2), uint_el(0x73C5, 0x2222),
        uint_el(0x83, 2), str_el(0x86, "A_PCM/INT/LIT"),
        float_el(0x23314F, 0.5), uint_el(0x23E383, 20_000_000),
    ]))
    tracks = elem(0x1654AE6B, track1 + track2)

    cluster_a = elem(0x1F43B675, b"".join([
        uint_el(0xE7, 0),
        simple_block(1, 0, b"V000"),
        simple_block(1, 40, b"V040"),
        block_group(1, 80, [b"L80a", "L80b"], "fixed", 80, -10_000_000),
        simple_block(2, 0, b"AUD0"),
    ]))

    cluster_b_body = b"".join([
        uint_el(0xE7, 1000),
        simple_block(1, -32768, b"NEGT"),   # raw 0x8000: signed interpretation required
        simple_block(1, 0, b"B000"),
        elem(0xA1, block_payload(2, 5, [], "xiph", corrupt_xiph=True)),
    ])
    cluster_b = _id_bytes(0x1F43B675) + unknown_size(8) + cluster_b_body

    cluster_c = elem(0x1F43B675, b"".join([
        uint_el(0xE7, 2000),
        simple_block(1, 0, b"C000"),
        block_group(1, 40, [b"E40aa", b"E4b", b"E40c"], "ebml", 40, 20_000_000),
        simple_block(2, 32767, b"AUDX"),
    ]))

    body_parts = [info, tracks, cluster_a, cluster_b, cluster_c]
    offsets = {}
    cursor = 0
    for name, part in zip(["info", "tracks", "cluster_a", "cluster_b", "cluster_c"], body_parts):
        offsets[name] = cursor  # position relative to Segment data start (Matroska convention)
        cursor += len(part)
    segment_body_without_cues = b"".join(body_parts)
    segment_header_len = len(_id_bytes(0x18538067)) + 8
    seg_data_offset = len(header) + segment_header_len
    pos_a = offsets["cluster_a"]
    pos_c = offsets["cluster_c"]

    cues = elem(0x1C53BB6B, b"".join([
        elem(0xBB, b"".join([
            uint_el(0xB3, 0),
            elem(0xB7, uint_el(0xF7, 1) + uint_el(0xF1, pos_a)),
        ])),
        elem(0xBB, b"".join([
            uint_el(0xB3, 1000),
            elem(0xB7, uint_el(0xF7, 1) + uint_el(0xF1, pos_c)),  # mis-pointing cue
        ])),
    ]))

    segment = _id_bytes(0x18538067) + unknown_size(8) + segment_body_without_cues + cues
    assert seg_data_offset == len(header) + segment_header_len
    return header + segment


FRAME_SUMMARY_CSV = """# frame summary exported by the media engineering team (raw, ticks unit)
# track,frame_index,expected_start_ticks,note
track,frame_index,expected_start_ticks,note
1,0,0,cluster A first video frame
1,1,40,cluster A second video frame
1,2,80,fixed-laced group start (2 frames)
2,0,0,audio first frame
1,3,-31768,signed overflow frame in cluster B (0x8000)
1,4,1000,cluster B first regular frame
1,5,2000,cluster C first frame
2,1,34767,audio frame at max positive relative timecode
"""
