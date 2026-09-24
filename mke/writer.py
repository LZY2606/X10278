"""Minimal Matroska serializer used to synthesize the offline case pack."""

from __future__ import annotations

from dataclasses import dataclass, field
from struct import pack

from . import catalog as C
from .ebml import encode_id, encode_sint, encode_uint, encode_vint


def _float(data: float) -> bytes:
    return pack(">f", data)


def elem(element_id: int, payload: bytes = b"") -> bytes:
    return encode_id(element_id) + encode_vint(len(payload)) + payload


def u(element_id: int, value: int) -> bytes:
    return elem(element_id, encode_uint(value))


def s(element_id: int, value: int) -> bytes:
    return elem(element_id, encode_sint(value))


def f(element_id: int, value: float) -> bytes:
    return elem(element_id, _float(value))


def st(element_id: int, value: str) -> bytes:
    return elem(element_id, value.encode("utf-8"))


def block(track: int, rel_tc: int, flags: int, payload: bytes) -> bytes:
    head = bytes([0x80 | (track & 0x7F)]) + encode_sint(rel_tc).rjust(2, b"\xff" if rel_tc < 0 else b"\x00") + bytes([flags])
    return head + payload


def xiph_block(track: int, rel_tc: int, frame_sizes: list[int],
               payload: bytes) -> bytes:
    flags = 0x20 | ((len(frame_sizes) - 1) << 4)
    table = b"".join(
        (b"\xff" * (size // 255)) + bytes([size % 255])
        for size in frame_sizes[:-1]
    )
    return block(track, rel_tc, flags, table + payload)


def build_ebml_header() -> bytes:
    head = b"".join([
        u(C.EBML_VERSION, 1),
        u(C.EBML_READ_VERSION, 1),
        u(C.EBML_MAX_ID_LENGTH, 4),
        u(C.EBML_MAX_SIZE_LENGTH, 8),
        st(C.DOC_TYPE, "matroska"),
        u(C.DOC_TYPE_VERSION, 4),
        u(C.DOC_TYPE_READ_VERSION, 2),
    ])
    return elem(C.EBML_HEADER, head)


def build_info(scale_ns: int) -> bytes:
    return elem(C.INFO, u(C.TIMESTAMP_SCALE, scale_ns) + st(C.MUXING_APP, "mke-court") + st(C.WRITING_APP, "mke-court"))


def build_tracks() -> bytes:
    video = elem(C.TRACK_ENTRY, b"".join([
        u(C.TRACK_NUMBER, 1),
        u(C.TRACK_UID, 101),
        u(C.TRACK_TYPE, 1),
        u(C.FLAG_DEFAULT, 1),
        u(C.DEFAULT_DURATION, 40_000_000),
        st(C.CODEC_ID, "V_MPEG4/ISO/AVC"),
        st(C.NAME, "video"),
        elem(C.VIDEO),
    ]))
    audio = elem(C.TRACK_ENTRY, b"".join([
        u(C.TRACK_NUMBER, 2),
        u(C.TRACK_UID, 202),
        u(C.TRACK_TYPE, 2),
        u(C.FLAG_DEFAULT, 1),
        st(C.CODEC_ID, "A_OPUS"),
        st(C.NAME, "audio"),
        elem(C.AUDIO, f(C.SAMPLING_FREQ, 48000.0)),
    ]))
    return elem(C.TRACKS, video + audio)


def build_cluster(cluster_tc: int, block_elements: list[bytes]) -> bytes:
    body = s(C.CLUSTER_TIMESTAMP, cluster_tc) + b"".join(block_elements)
    return elem(C.CLUSTER, body)


def simple_video_block(rel_tc: int, payload: bytes, keyframe: bool = True) -> bytes:
    flags = 0x80 if keyframe else 0x00
    return elem(C.SIMPLE_BLOCK, block(1, rel_tc, flags, payload))


def audio_block_group(rel_tc: int, frame_sizes: list[int], payload: bytes,
                      discard_padding_ns: int | None = None) -> bytes:
    inner = elem(C.BLOCK, xiph_block(2, rel_tc, frame_sizes, payload))
    if discard_padding_ns is not None:
        inner += s(C.DISCARD_PADDING, discard_padding_ns)
    return elem(C.BLOCK_GROUP, inner)


def cue_point(cue_ticks: int, track: int, cluster_pos: int,
              relative_pos: int) -> bytes:
    pos = elem(C.CUE_TRACK_POSITIONS,
               u(C.CUE_TRACK, track)
               + u(C.CUE_CLUSTER_POSITION, cluster_pos)
               + u(C.CUE_RELATIVE_POSITION, relative_pos))
    return elem(C.CUE_POINT, u(C.CUE_TIME, cue_ticks) + pos)


def build_segment(payload: bytes, unknown: bool = False) -> bytes:
    size = (
        encode_vint(0, unknown=True, width=4)
        if unknown
        else encode_vint(len(payload), width=4)
    )
    return encode_id(C.SEGMENT) + size + payload


def build_cues(points: list[bytes]) -> bytes:
    payload = points if isinstance(points, (bytes, bytearray)) else b"".join(points)
    return elem(C.CUES, bytes(payload))
