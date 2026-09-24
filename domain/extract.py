"""Extract the evidence model from a parsed EBML document.

Everything here is source-level fact: element offsets, unknown-length flags,
track parameters, cluster/block timecodes, discard padding, cues and original
byte ranges. Normalisation to nanoseconds happens in the derived layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from . import ebml, ids
from .ebml import Element
from .blocks import decode_block, BlockInfo
from .time import DEFAULT_TIMECODE_SCALE


@dataclass
class TrackParam:
    id: str
    number: int
    uid: int | None
    type: int | None
    codec: str | None
    default_duration: int | None
    track_timecode_scale: float
    track_offset: int
    default_duration_unit: str
    element_start: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Frame:
    id: str
    index: int
    block_id: str
    start: int
    end: int
    trusted: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Block:
    id: str
    start: int
    end: int
    payload_start: int
    kind: str                 # SimpleBlock | Block
    group_start: int | None
    track_number: int
    rel_tc_raw: int
    rel_tc_signed: int
    tc_width_bits: int
    tc_overflow: bool
    lacing: int
    keyframe: bool
    duration_raw: int | None
    discard_padding_raw: int | None
    reference_blocks: list
    corrupt: bool
    diagnostics: list
    frames: list
    raw_range: list

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Cluster:
    id: str
    start: int
    end: int
    segment_relative_pos: int
    timecode_raw: int | None
    unknown_size: bool
    diagnostics: list
    blocks: list
    raw_range: list

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Cue:
    id: str
    start: int
    time_raw: int
    track: int | None
    cluster_position: int | None
    relative_position: int | None
    duration_raw: int | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Document:
    file_id: str
    byte_length: int
    segment_start: int | None
    segment_unknown_size: bool
    segment_data_start: int | None
    timecode_scale: int
    timecode_scale_present: bool
    doc_type: str | None
    tracks: list
    clusters: list
    cues: list
    elements: list
    unknown_lengths: list
    raw_ranges: list
    diagnostics: list

    def to_dict(self) -> dict:
        return asdict(self)


def _val(el: Element | None, default=None):
    return el.value if el is not None else default


def extract(file_id: str, buf: bytes, roots: list[Element]) -> Document:
    doc = Document(
        file_id=file_id, byte_length=len(buf), segment_start=None,
        segment_unknown_size=False, segment_data_start=None,
        timecode_scale=DEFAULT_TIMECODE_SCALE, timecode_scale_present=False,
        doc_type=None, tracks=[], clusters=[], cues=[], elements=[],
        unknown_lengths=[], raw_ranges=[], diagnostics=[],
    )
    header = next((r for r in roots if r.id == "1a45dfa3"), None)
    if header:
        dt = ebml.first(header, "4282")
        doc.doc_type = _val(dt)
        doc.elements.append(_element_summary(file_id, header))
        _collect_elements(file_id, header, doc)

    segment = next((r for r in roots if r.id == "18538067"), None)
    if segment is None:
        doc.diagnostics.append("no Segment element found")
        return doc
    doc.segment_start = segment.start
    doc.segment_unknown_size = segment.unknown_size
    doc.segment_data_start = segment.data_start
    if segment.unknown_size:
        doc.unknown_lengths.append({
            "id": ids.element_id(file_id, "", 0, segment.id, segment.start),
            "element": "Segment", "start": segment.start,
            "data_start": segment.data_start, "resolved_end": segment.end,
        })

    for child in segment.children:
        t = ebml.first(child, "2ad7b1")
        if t is not None:
            doc.timecode_scale = t.value
            doc.timecode_scale_present = True
            break

    tracks_el = ebml.first(segment, "1654ae6b")
    if tracks_el:
        for idx, te in enumerate(tracks_el.children):
            if te.id != "ae":
                continue
            tts_el = ebml.first(te, "23314f")
            off_el = ebml.first(te, "234e7a")
            num_el = ebml.first(te, "d7")
            tp = TrackParam(
                id=ids.track_id(file_id, _val(num_el, idx + 1), te.start),
                number=_val(num_el, idx + 1),
                uid=_val(ebml.first(te, "73c5")),
                type=_val(ebml.first(te, "83")),
                codec=_val(ebml.first(te, "86")),
                default_duration=_val(ebml.first(te, "23e383")),
                track_timecode_scale=float(_val(tts_el, 1.0) or 1.0),
                track_offset=int(_val(off_el, 0) or 0),
                default_duration_unit="ns-per-frame"
                if _val(ebml.first(te, "23e383")) is not None else "unset",
                element_start=te.start,
            )
            doc.tracks.append(tp.to_dict())

    for ce in [c for c in segment.children if c.id == "1f43b675"]:
        doc.clusters.append(_cluster(file_id, ce, segment.data_start, buf, doc))

    cues_el = ebml.first(segment, "1c53bb6b")
    if cues_el:
        for cp in cues_el.children:
            if cp.id != "bb":
                continue
            ctp = ebml.first(cp, "b7")
            doc.cues.append(Cue(
                id=ids.cue_id(file_id, cp.start),
                start=cp.start,
                time_raw=_val(ebml.first(cp, "b3")),
                track=_val(ebml.first(ctp, "f7")) if ctp else None,
                cluster_position=_val(ebml.first(ctp, "f1")) if ctp else None,
                relative_position=_val(ebml.first(ctp, "f0")) if ctp else None,
                duration_raw=_val(ebml.first(ctp, "5378")) if ctp else None,
            ).to_dict())

    _collect_elements(file_id, segment, doc)
    return doc


def _cluster(file_id, ce, seg_data_start, buf, doc):
    tc_el = ebml.first(ce, "e7")
    cl = Cluster(
        id=ids.cluster_id(file_id, ce.start), start=ce.start, end=ce.end,
        segment_relative_pos=ce.start - seg_data_start,
        timecode_raw=_val(tc_el), unknown_size=ce.unknown_size,
        diagnostics=list(ce.diagnostics), blocks=[],
        raw_range=[ce.start, ce.end],
    )
    if ce.unknown_size:
        doc.unknown_lengths.append({
            "id": ids.element_id(file_id, "", 0, ce.id, ce.start),
            "element": "Cluster", "start": ce.start,
            "data_start": ce.data_start, "resolved_end": ce.end,
        })
    blocks = []
    for child in ce.children:
        if child.id == "a3":
            blocks.append((child, child, "SimpleBlock"))
        elif child.id == "a0":
            inner = ebml.first(child, "a1")
            if inner is not None:
                blocks.append((child, inner, "Block"))
    for group_or_block, block_el, kind in blocks:
        payload = bytes.fromhex(block_el.raw) if block_el.raw else b""
        info = decode_block(payload, base=block_el.data_start)
        bid = ids.block_id(file_id, block_el.start)
        discard_el = ebml.first(group_or_block, "75a1") if group_or_block is not block_el else None
        dur_el = ebml.first(group_or_block, "9b") if group_or_block is not block_el else None
        refs = [r.value for r in ebml.find_all(group_or_block, "fa")] \
            if group_or_block is not block_el else []
        frames = [Frame(
            id=ids.frame_id(file_id, block_el.start, f.index),
            index=f.index, block_id=bid, start=f.start, end=f.end,
            trusted=f.trusted,
        ).to_dict() for f in info.frames]
        blk = Block(
            id=bid, start=block_el.start, end=block_el.end,
            payload_start=block_el.data_start, kind=kind,
            group_start=group_or_block.start if group_or_block is not block_el else None,
            track_number=info.track_value, rel_tc_raw=info.tc_uint,
            rel_tc_signed=info.tc_signed, tc_width_bits=info.tc_width_bits,
            tc_overflow=info.tc_overflow, lacing=info.lacing,
            keyframe=info.keyframe, duration_raw=_val(dur_el),
            discard_padding_raw=_val(discard_el), reference_blocks=refs,
            corrupt=info.corrupt, diagnostics=info.diagnostics, frames=frames,
            raw_range=[block_el.start, block_el.end],
        )
        cl.blocks.append(blk.to_dict())
        if info.corrupt:
            doc.raw_ranges.append({
                "kind": "corrupt-lacing", "block_id": bid,
                "start": block_el.data_start + info.header_len,
                "end": block_el.end,
            })
    return cl.to_dict()


def _element_summary(file_id, el):
    return {
        "id": ids.element_id(file_id, "", 0, el.id, el.start),
        "eid": el.id, "name": el.name, "start": el.start, "end": el.end,
        "data_start": el.data_start, "data_end": el.data_end,
        "id_len": el.id_len, "size_len": el.size_len,
        "unknown_size": el.unknown_size, "declared_size": el.declared_size,
        "value": el.value,
    }


def _collect_elements(file_id, root, doc, path="", depth=0):
    for idx, el in enumerate(root.children):
        summary = _element_summary(file_id, el)
        summary["depth"] = depth
        summary["path"] = f"{path}/{el.name}[{idx}]"
        doc.elements.append(summary)
        doc.raw_ranges.append({
            "kind": "element", "element": el.name,
            "start": el.start, "end": el.end})
        if el.diagnostics:
            doc.diagnostics.extend(
                f"{el.name}@{el.start}: {d}" for d in el.diagnostics)
        _collect_elements(file_id, el, doc, summary["path"], depth + 1)


def extract_bytes(file_id: str, buf: bytes) -> Document:
    roots = ebml.parse_document(buf)
    return extract(file_id, buf, roots)
