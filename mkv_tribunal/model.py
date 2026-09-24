"""Raw Matroska facts lifted straight from the bytes.

Every value here is either a byte range, the raw integer as stored, or a raw
string.  No normalization (nanosecond conversion, global time mapping, cue
verification) happens at import time: those are *derived* so that rule changes
and adjudication can be versioned.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any

from . import ebml, ids
from .blocks import parse_block, BlockParse


@dataclass
class RawTrack:
    identity: str
    number: int
    uid: int | None
    type: int
    codec_id: str
    name: str | None
    default_duration_ns: int | None       # raw element, already nanoseconds
    flag_lacing: bool | None
    video: dict
    audio: dict
    element_offset: int
    byte_range: list


@dataclass
class RawBlock:
    identity: str
    cluster_identity: str
    kind: str                 # "SimpleBlock" | "Block"
    track_number: int
    rel_tc: int               # signed 16-bit interpretation
    rel_tc_raw: str
    lacing: str
    flags: int
    keyframe: bool | None
    discardable: bool | None
    block_duration_raw: int | None   # scale units, BlockGroup only
    discard_padding_raw: int | None  # nanoseconds, BlockGroup only
    reference_blocks: list
    frame_count: int
    frames: list | None             # [[offset,size],...] or None when corrupt
    block_errors: list
    element_offset: int
    payload_range: list
    raw_range: list


@dataclass
class RawCluster:
    identity: str
    timecode_raw: int | None
    element_offset: int
    payload_range: list
    unknown_length: bool
    blocks: list = field(default_factory=list)


@dataclass
class RawCue:
    identity: str
    cue_time_raw: int
    track_number: int
    cluster_pos: int
    rel_pos: int | None
    element_offset: int
    raw_range: list


@dataclass
class RawDocument:
    file_identity: str
    filename: str
    byte_length: int
    sha1: str
    timecode_scale_raw: int | None
    duration_raw: float | None
    muxing_app: str | None
    writing_app: str | None
    segment_unknown_length: bool
    segment_range: list
    segment_data_start: int | None
    tracks: list
    clusters: list
    blocks: list
    cues: list
    elements: list            # compact serializable element list
    parse_diagnostics: list
    unknown_length_elements: list

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True,
                          indent=1)

    @classmethod
    def from_dict(cls, d: dict) -> "RawDocument":
        return cls(**d)


def _elem_dict(e: ebml.Element) -> dict:
    return {
        "identity": e.identity,
        "id": f"0x{e.eid:x}",
        "id_name": ids.id_name(e.eid),
        "offset": e.offset,
        "header_size": e.header_size,
        "size": e.size,
        "payload_start": e.payload_start,
        "payload_end": e.payload_end,
        "depth": e.depth,
        "ordinal": e.ordinal,
        "parent_path": [f"0x{x:x}" for x in e.parent_path],
        "raw_range": [e.offset, e.payload_end],
    }


def _child_payload(data: bytes, parent: ebml.Element, elems: list[ebml.Element],
                   want: int) -> bytes | None:
    for c in ebml.children(elems, parent):
        if c.eid == want:
            return ebml.payload(data, c)
    return None


def import_bytes(data: bytes, filename: str = "input.mkv") -> RawDocument:
    sha = hashlib.sha1(data).hexdigest()
    file_identity = "f-" + sha[:16]
    elems, diag = ebml.parse_document(data)

    segment = next((e for e in elems if e.eid == ids.SEGMENT), None)
    seg_unknown = segment is not None and segment.size is None
    seg_range: list = [None, None]
    seg_data_start: int | None = None
    if segment:
        seg_range = [segment.offset, segment.payload_end]
        seg_data_start = segment.payload_start

    def seg_children(eid: int):
        if not segment:
            return []
        return [c for c in ebml.children(elems, segment) if c.eid == eid]

    # --- Info -------------------------------------------------------------
    scale_raw: int | None = None
    duration_raw: float | None = None
    mux_app = write_app = None
    for info in seg_children(ids.INFO):
        if (b := _child_payload(data, info, elems, ids.TIMECODE_SCALE)) is not None:
            scale_raw = ebml.read_uint(b)
        if (b := _child_payload(data, info, elems, ids.DURATION)) is not None:
            duration_raw = _read_float(b)
        if (b := _child_payload(data, info, elems, ids.MUXING_APP)) is not None:
            mux_app = _read_text(b)
        if (b := _child_payload(data, info, elems, ids.WRITING_APP)) is not None:
            write_app = _read_text(b)

    # --- Tracks -----------------------------------------------------------
    tracks: list[RawTrack] = []
    for tracks_el in seg_children(ids.TRACKS):
        for te in ebml.children(elems, tracks_el):
            if te.eid != ids.TRACK_ENTRY:
                continue
            tnum = ebml.read_uint(_child_payload(data, te, elems, ids.TRACK_NUMBER) or b"\x00")
            tuid_b = _child_payload(data, te, elems, ids.TRACK_UID)
            ttype = ebml.read_uint(_child_payload(data, te, elems, ids.TRACK_TYPE) or b"\x00")
            codec = _read_text(_child_payload(data, te, elems, ids.CODEC_ID) or b"")
            name_b = _child_payload(data, te, elems, ids.NAME)
            dd_b = _child_payload(data, te, elems, ids.DEFAULT_DURATION)
            fl_b = _child_payload(data, te, elems, ids.FLAG_LACING)
            video_d: dict = {}
            audio_d: dict = {}
            for sub in ebml.children(elems, te):
                if sub.eid == ids.VIDEO:
                    if (b := _child_payload(data, sub, elems, ids.PIXEL_WIDTH)) is not None:
                        video_d["width"] = ebml.read_uint(b)
                    if (b := _child_payload(data, sub, elems, ids.PIXEL_HEIGHT)) is not None:
                        video_d["height"] = ebml.read_uint(b)
                elif sub.eid == ids.AUDIO:
                    if (b := _child_payload(data, sub, elems, ids.SAMPLING_FREQ)) is not None:
                        audio_d["sampling_freq_hz"] = _read_float(b)
                    if (b := _child_payload(data, sub, elems, ids.CHANNELS)) is not None:
                        audio_d["channels"] = ebml.read_uint(b)
            tracks.append(RawTrack(
                identity=te.identity, number=tnum,
                uid=ebml.read_uint(tuid_b) if tuid_b is not None else None,
                type=ttype, codec_id=codec,
                name=_read_text(name_b) if name_b is not None else None,
                default_duration_ns=ebml.read_uint(dd_b) if dd_b is not None else None,
                flag_lacing=bool(ebml.read_uint(fl_b)) if fl_b is not None else None,
                video=video_d, audio=audio_d, element_offset=te.offset,
                byte_range=[te.offset, te.payload_end],
            ))

    # --- Clusters / blocks ------------------------------------------------
    clusters: list[RawCluster] = []
    for ce in seg_children(ids.CLUSTER):
        tc_b = _child_payload(data, ce, elems, ids.CLUSTER_TIMECODE)
        tc = ebml.read_uint(tc_b) if tc_b is not None else None
        rc = RawCluster(
            identity=ce.identity, timecode_raw=tc, element_offset=ce.offset,
            payload_range=[ce.payload_start, ce.payload_end],
            unknown_length=ce.size is None,
        )
        _collect_blocks(data, elems, ce, rc, blocks)
        clusters.append(rc)

    # --- Cues -------------------------------------------------------------
    cues: list[RawCue] = []
    for cues_el in seg_children(ids.CUES):
        for cp in ebml.children(elems, cues_el):
            if cp.eid != ids.CUE_POINT:
                continue
            ct_b = _child_payload(data, cp, elems, ids.CUE_TIME)
            for pos in ebml.children(elems, cp):
                if pos.eid != ids.CUE_TRACK_POSITIONS:
                    continue
                cr_b = _child_payload(data, pos, elems, ids.CUE_CLUSTER_POS)
                rr_b = _child_payload(data, pos, elems, ids.CUE_REL_POS)
                ctp_b = _child_payload(data, pos, elems, ids.CUE_TRACK)
                cues.append(RawCue(
                    identity=pos.identity,
                    cue_time_raw=ebml.read_uint(ct_b) if ct_b is not None else 0,
                    track_number=ebml.read_uint(ctp_b) if ctp_b is not None else 0,
                    cluster_pos=ebml.read_uint(cr_b) if cr_b is not None else 0,
                    rel_pos=ebml.read_uint(rr_b) if rr_b is not None else None,
                    element_offset=pos.offset, raw_range=[pos.offset, pos.payload_end],
                ))

    unknowns = [
        {"identity": e.identity, "id_name": ids.id_name(e.eid), "offset": e.offset}
        for e in elems if e.size is None
    ]

    return RawDocument(
        file_identity=file_identity, filename=filename, byte_length=len(data),
        sha1=sha, timecode_scale_raw=scale_raw, duration_raw=duration_raw,
        muxing_app=mux_app, writing_app=write_app,
        segment_unknown_length=seg_unknown, segment_range=seg_range,
        segment_data_start=seg_data_start, tracks=[asdict(t) for t in tracks],
        clusters=[asdict(c) for c in clusters],
        blocks=[asdict(b) for b in blocks],
        cues=[asdict(c) for c in cues],
        elements=[_elem_dict(e) for e in elems],
        parse_diagnostics=diag, unknown_length_elements=unknowns,
    )


def _collect_blocks(data, elems, ce, rc, out) -> None:
    for child in ebml.children(elems, ce):
        if child.eid == ids.SIMPLE_BLOCK:
            rec = _block_record(data, child, ce, "SimpleBlock")
            out.append(rec); rc.blocks.append(rec.identity)
        elif child.eid == ids.BLOCK_GROUP:
            inner = None
            for sub in ebml.children(elems, child):
                if sub.eid == ids.BLOCK:
                    inner = sub
            bd_b = _child_payload(data, child, elems, ids.BLOCK_DURATION)
            dp_b = _child_payload(data, child, elems, ids.DISCARD_PADDING)
            refs = [
                ebml.read_int_signed(ebml.payload(data, r))
                for r in ebml.children(elems, child) if r.eid == ids.REFERENCE_BLOCK
            ]
            if inner is not None:
                rec = _block_record(data, inner, ce, "Block",
                                    container=child,
                                    block_duration=ebml.read_uint(bd_b) if bd_b is not None else None,
                                    discard_padding=_read_signed(dp_b) if dp_b is not None else None,
                                    references=refs)
                out.append(rec); rc.blocks.append(rec.identity)


def _block_record(data, be, ce, kind, container=None, block_duration=None,
                  discard_padding=None, references=None) -> RawBlock:
    payload = ebml.payload(data, be)
    bp: BlockParse = parse_block(payload)
    element_el = container or be
    return RawBlock(
        identity=element_el.identity, cluster_identity=ce.identity, kind=kind,
        track_number=bp.track_number, rel_tc=bp.rel_tc_signed,
        rel_tc_raw=bp.rel_tc_raw_hex, lacing=("none", "xiph", "fixed", "ebml")[bp.lacing],
        flags=bp.flags, keyframe=bp.keyframe, discardable=bp.discardable,
        block_duration_raw=block_duration, discard_padding_raw=discard_padding,
        reference_blocks=references or [], frame_count=bp.frame_count,
        frames=bp.frames, block_errors=bp.errors, element_offset=element_el.offset,
        payload_range=[be.payload_start, be.payload_end],
        raw_range=[element_el.offset, element_el.payload_end],
    )


def _read_float(b: bytes) -> float:
    import struct

    if len(b) == 4:
        return struct.unpack(">f", b)[0]
    return struct.unpack(">d", b)[0]


def _read_signed(b: bytes) -> int:
    return ebml.read_int_signed(b)


def _read_text(b: bytes) -> str:
    return b.rstrip(b"\x00").decode("utf-8", "replace")
