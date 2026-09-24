"""Deterministic Matroska fixture builder for the offline case pack.

The baseline is a deliberately tiny but structurally honest file:
EBML header, Segment(Info, Tracks[v,a], Cluster x4, Cues).
Bytes are laid out with fixed width sizes where the case study needs
later byte patches to stay surgical.
"""
from __future__ import annotations

import json

from . import ebml
from .ebml import (master, elem, uint_el, sint_el, float_el, string_el,
                   serialize, encode_vint)


def ebml_header() -> bytes:
    hdr = master("EBML", [
        uint_el("EBMLVersion", 1),
        uint_el("EBMLReadVersion", 1),
        uint_el("EBMLMaxIDLength", 4),
        uint_el("EBMLMaxSizeLength", 8),
        string_el("DocType", "matroska"),
        uint_el("DocTypeVersion", 4),
        uint_el("DocTypeReadVersion", 2),
    ])
    return serialize(hdr)


def info(scale_ns: int = 1_000_000, duration: float | None = None) -> ebml.Element:
    children = [
        uint_el("TimecodeScale", scale_ns),
        string_el("MuxingApp", "tribunal-fixture"),
        string_el("WritingApp", "tribunal-casebuild"),
    ]
    if duration is not None:
        children.append(float_el("Duration", duration, 8))
    return master("Info", children)


def tracks(video_default_duration_ns: int = 40_000_000,
           audio_track_scale: float | None = None) -> bytes:
    video = [
        uint_el("TrackNumber", 1),
        uint_el("TrackUID", 1001),
        uint_el("TrackType", 1),
        uint_el("FlagLacing", 1),
        uint_el("DefaultDuration", video_default_duration_ns),
        string_el("Name", "video"),
        string_el("CodecID", "V_FAKE/RAW"),
    ]
    audio = [
        uint_el("TrackNumber", 2),
        uint_el("TrackUID", 2002),
        uint_el("TrackType", 2),
        uint_el("FlagLacing", 1),
        string_el("Name", "audio"),
        string_el("CodecID", "A_FAKE/PCM"),
    ]
    if audio_track_scale is not None:
        audio.append(float_el("TimecodeScale", audio_track_scale, 8))
    return master("Tracks", [
        master("TrackEntry", video),
        master("TrackEntry", audio),
    ])


def block_bytes(track_number: int, rel_ticks: int, flags: int,
               payload: bytes) -> bytes:
    return (encode_vint(track_number, 1)
            + rel_ticks.to_bytes(2, "big", signed=True)
            + bytes([flags]) + payload)


def xiph_payload(frame_sizes: list[int], frame_bodies: list[bytes]) -> bytes:
    sizes = frame_sizes[:-1]
    header = bytes([len(sizes)])
    for size in sizes:
        chunks = [255] * (size // 255)
        rem = size % 255
        header += bytes(chunks + ([rem] if rem or not chunks else []))
    return header + b"".join(frame_bodies)


def simple_block(track: int, rel_ticks: int, keyframe: bool, payload: bytes,
                invisible: bool = False) -> ebml.Element:
    flags = (0x80 if keyframe else 0) | (0x08 if invisible else 0)
    return elem("SimpleBlock", block_bytes(track, rel_ticks, flags, payload))


def block_group(track: int, rel_ticks: int, keyframe: bool, payload: bytes,
                duration: int | None = None,
                discard_padding: int | None = None,
                references: list[int] | None = None,
                invisible: bool = False) -> ebml.Element:
    flags = (0x80 if keyframe else 0) | (0x08 if invisible else 0)
    children = [elem("Block", block_bytes(track, rel_ticks, flags, payload))]
    if duration is not None:
        children.append(uint_el("BlockDuration", duration))
    if discard_padding is not None:
        width = max(1, (abs(discard_padding).bit_length() + 8) // 8)
        children.append(sint_el("DiscardPadding", discard_padding, width))
    for ref in references or []:
        children.append(sint_el("ReferenceBlock", ref, 2))
    return master("BlockGroup", children)


def cluster(timecode: int, children: list[ebml.Element],
            prev_size: int | None = None,
            unknown_size_width: int | None = None,
            size_width: int = 4) -> ebml.Element:
    nodes = [uint_el("Timecode", timecode, width=4)]
    if prev_size is not None:
        nodes.append(uint_el("PrevSize", prev_size, width=4))
    nodes.extend(children)
    return master("Cluster", nodes,
                 unknown_size_width=unknown_size_width, size_width=size_width)


def cue_point(cue_time: int, track: int, cluster_position: int,
              relative: int | None = None, duration: int | None = None) -> bytes:
    positions = [
        uint_el("CueTrack", track),
        uint_el("CueClusterPosition", cluster_position, width=4),
    ]
    if relative is not None:
        positions.append(uint_el("CueRelativePosition", relative))
    if duration is not None:
        positions.append(uint_el("CueDuration", duration))
    return master("CuePoint", [
        uint_el("CueTime", cue_time, width=4),
        master("CueTrackPositions", positions),
    ])


def cues(points: list[bytes]) -> bytes:
    return serialize(master("Cues", points_elems(points)))


def points_elems(points: list[bytes]) -> list[ebml.Element]:
    return [elem("CuePoint", p) for p in points]


def build_segment_body(parts: list[bytes]) -> bytes:
    return b"".join(parts)


def assemble(scale_ns: int, cluster_bytes: list[bytes],
             cue_points: list[bytes], *,
             unknown_segment_width: int | None = None,
             video_default_duration: int = 40_000_000,
             audio_track_scale: float | None = None,
             duration: float | None = None) -> bytes:
    info_el = info(scale_ns, duration)
    tracks_el = tracks(video_default_duration, audio_track_scale)
    body = (serialize(info_el) + serialize(tracks_el)
            + b"".join(cluster_bytes)
            + serialize(master("Cues", points_elems(cue_points))))
    segment = elem("Segment", body,
                   unknown_size_width=unknown_segment_width,
                   size_width=None if unknown_segment_width else 4)
    return ebml_header() + serialize(segment)


def frame_sidecar() -> dict:
    """Frame summaries as imported from the media pipeline."""
    return {
        "source": "frame-summary-v1",
        "frames": [
            {"track_number": 1, "index": 0, "presentation_ns": 0,
             "keyframe": True, "payload_bytes": 64},
            {"track_number": 1, "index": 1, "presentation_ns": 40_000_000,
             "keyframe": False, "payload_bytes": 48},
            {"track_number": 2, "index": 0, "presentation_ns": 0,
             "keyframe": True, "payload_bytes": 12},
            {"track_number": 2, "index": 1,
             "presentation_time": {"ticks": 20, "scale_ns": 1_000_000},
             "keyframe": False, "payload_bytes": 12},
        ],
    }


BASELINE_SCALE_NS = 1_000_000
