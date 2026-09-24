"""Domain extraction: from raw EBML tree to tracks / clusters / cues.

This layer only **records** imported evidence (raw bytes, offsets, raw units).
Normalisation to a common time axis happens in ``mkebml.timeline``.  Nothing
here throws away an unconvertible value: it is kept with a diagnostic.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from . import ids
from .block import parse_block
from .parser import Element, parse_ebml
from .units import Diagnostic, Raw, unsigned_of, signed_of


def stable_id(prefix: str, *coords: int) -> str:
    return f"{prefix}:" + ":".join(str(c) for c in coords)


def _uint(elem: Element) -> int | None:
    data = elem.payload()
    return unsigned_of(data) if data else None


def _sint(elem: Element) -> int | None:
    data = elem.payload()
    return signed_of(data) if data else None


def _float(elem: Element) -> float | None:
    data = elem.payload()
    if len(data) == 4:
        return struct.unpack(">f", data)[0]
    if len(data) == 8:
        return struct.unpack(">d", data)[0]
    return None


def _raw_uint(elem: Element, unit: str, basis: str) -> Raw:
    data = elem.payload()
    return Raw.from_bytes(data, unit, basis, signed=False,
                          encoding="uint-be") if data else Raw(
        None, unit, basis, "", "uint-be", False,
        [Diagnostic("EMPTY_INTEGER", "zero-length integer element",
                    subject=f"0x{elem.id:X}@{elem.id_offset}")])


def _raw_sint(elem: Element, unit: str, basis: str) -> Raw:
    data = elem.payload()
    return Raw.from_bytes(data, unit, basis, signed=True,
                          encoding="sint-be") if data else Raw(
        None, unit, basis, "", "sint-be", False,
        [Diagnostic("EMPTY_INTEGER", "zero-length integer element",
                    subject=f"0x{elem.id:X}@{elem.id_offset}")])


@dataclass
class DiscardPadding:
    block_id: str
    raw: Raw                       # codec samples, track sample-rate basis
    byte_range: tuple[int, int]
    block_add_id: int

    def to_json(self) -> dict[str, Any]:
        return {
            "block": self.block_id,
            "block_add_id": self.block_add_id,
            "raw": self.raw.to_json(),
            "byte_range": list(self.byte_range),
        }


@dataclass
class Block:
    sid: str
    cluster_sid: str
    element_kind: str              # "SimpleBlock" | "Block"
    element: Element
    track_number: int
    block_group: Element | None
    duration_raw: int | None       # cluster ticks, raw
    reference_blocks: list[int]
    discard_padding: DiscardPadding | None
    parse: Any

    def byte_range(self) -> tuple[int, int]:
        holder = self.block_group if self.block_group is not None else self.element
        return holder.id_offset, holder.elem_end

    def to_json(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "kind": self.element_kind,
            "cluster": self.cluster_sid,
            "track_number": self.track_number,
            "byte_range": list(self.byte_range()),
            "block_byte_range": [self.element.id_offset, self.element.elem_end],
            "block_header": self.parse.to_json(),
            "duration_ticks_raw": self.duration_raw,
            "reference_blocks": self.reference_blocks,
            "discard_padding": self.discard_padding.to_json()
            if self.discard_padding else None,
            "diagnostics": [d.to_json() for d in
                            self.parse.diagnostics],
        }


@dataclass
class Cluster:
    sid: str
    element: Element
    timecode_raw: int | None
    unknown_length: bool
    blocks: list[Block] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "byte_range": [self.element.id_offset, self.element.elem_end],
            "header_byte_range": [self.element.id_offset,
                                  self.element.data_start],
            "timecode_ticks_raw": self.timecode_raw,
            "unknown_length": self.unknown_length,
            "size": self.element.size,
            "blocks": [b.to_json() for b in self.blocks],
            "diagnostics": [d.to_json() for d in self.element.diagnostics],
        }


@dataclass
class Track:
    sid: str
    number: int
    uid: int | None
    track_type: int | None
    codec_id: str | None
    name: str | None
    flag_lacing: bool
    scale_raw: Raw                 # dimensionless ratio, raw
    offset_raw: Raw                # cluster ticks
    default_duration_raw: Raw      # ns
    sampling_hz: float | None

    def to_json(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "number": self.number,
            "uid": self.uid,
            "track_type": self.track_type,
            "codec_id": self.codec_id,
            "name": self.name,
            "flag_lacing": self.flag_lacing,
            "timecode_scale": self.scale_raw.to_json(),
            "offset_ticks": self.offset_raw.to_json(),
            "default_duration": self.default_duration_raw.to_json(),
            "sampling_frequency_hz": self.sampling_hz,
        }


@dataclass
class Cue:
    sid: str
    cue_time_ticks: int | None
    track_number: int | None
    cluster_position: int | None   # bytes relative to segment data start
    relative_position: int | None
    byte_range: tuple[int, int]

    def to_json(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "cue_time_ticks_raw": self.cue_time_ticks,
            "track_number": self.track_number,
            "cluster_position_raw": self.cluster_position,
            "relative_position_raw": self.relative_position,
            "byte_range": list(self.byte_range),
        }


@dataclass
class Document:
    blob: bytes
    roots: list[Element]
    segment: Element | None
    doc_type: str
    timecode_scale_raw: Raw
    duration_raw: Raw | None
    tracks: list[Track]
    clusters: list[Cluster]
    cues: list[Cue]
    diagnostics: list[Diagnostic]

    def to_json(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "file_size": len(self.blob),
            "segment_byte_range": ([self.segment.id_offset,
                                    self.segment.elem_end]
                                   if self.segment else None),
            "segment_unknown_length": (self.segment.unknown_length
                                       if self.segment else None),
            "timecode_scale": self.timecode_scale_raw.to_json(),
            "duration": self.duration_raw.to_json() if self.duration_raw
            else None,
            "tracks": [t.to_json() for t in self.tracks],
            "clusters": [c.to_json() for c in self.clusters],
            "cues": [c.to_json() for c in self.cues],
            "ebml_tree": [r.to_json() for r in self.roots],
            "diagnostics": [d.to_json() for d in self.diagnostics],
        }


def _extract_discard_padding(bg: Element, block_sid: str) -> tuple[
        DiscardPadding | None, list[Diagnostic]]:
    diag: list[Diagnostic] = []
    additions = bg.find(ids.BLOCK_ADDITIONS)
    if additions is None:
        return None, diag
    for more in additions.find_all(ids.BLOCK_MORE):
        add_id_el = more.find(ids.BLOCK_ADD_ID)
        add_el = more.find(ids.BLOCK_ADDITIONAL)
        add_id = _uint(add_id_el) if add_id_el is not None else 1
        if add_id == 1 and add_el is not None:
            raw = _raw_sint(add_el, "codec_sample", "track-sample-rate")
            return DiscardPadding(block_sid, raw,
                                  (more.id_offset, more.elem_end), add_id), diag
    return None, diag


def extract(blob: bytes) -> Document:
    roots, parse_diag = parse_ebml(blob)
    diagnostics = list(parse_diag)
    segment = next((r for r in roots if r.id == ids.SEGMENT), None)
    doc_type = ""
    ebml_head = next((r for r in roots if r.id == ids.EBML), None)
    if ebml_head is not None:
        dt = ebml_head.find(ids.DOC_TYPE)
        if dt is not None:
            doc_type = dt.payload().decode("utf-8", "replace")

    timecode_scale = Raw(1_000_000, "ns", "segment-timecode-scale",
                         None, "default")
    duration_raw: Raw | None = None
    tracks: list[Track] = []
    clusters: list[Cluster] = []
    cues: list[Cue] = []

    if segment is not None:
        info = segment.find(ids.INFO)
        if info is not None:
            ts = info.find(ids.TIMESTAMP_SCALE)
            if ts is not None:
                timecode_scale = _raw_uint(ts, "ns",
                                           "segment-timecode-scale")
            dur = info.find(ids.DURATION)
            if dur is not None:
                value = _float(dur)
                duration_raw = Raw(
                    value, "tick", "segment-timecode-scale",
                    dur.payload().hex(),
                    f"float{len(dur.payload())*8}-be",
                    value is not None,
                    [] if value is not None else
                    [Diagnostic("DURATION_UNREADABLE",
                                "Duration float width unsupported", "warning")])

        tracks_el = segment.find(ids.TRACKS)
        if tracks_el is not None:
            for te in tracks_el.find_all(ids.TRACK_ENTRY):
                num_el = te.find(ids.TRACK_NUMBER)
                number = _uint(num_el) if num_el is not None else -1
                sid = stable_id("track", te.id_offset)
                uid_el = te.find(ids.TRACK_UID)
                type_el = te.find(ids.TRACK_TYPE)
                codec_el = te.find(ids.CODEC_ID)
                name_el = te.find(ids.NAME)
                scale_el = te.find(ids.TRACK_TIMECODE_SCALE)
                offset_el = te.find(ids.TRACK_OFFSET)
                dur_el = te.find(ids.DEFAULT_DURATION)
                audio = te.find(ids.AUDIO)
                sampling = None
                if audio is not None:
                    sf = audio.find(ids.SAMPLING_FREQUENCY)
                    if sf is not None:
                        sampling = _float(sf)
                lacing_el = te.find(ids.FLAG_LACING)
                flag_lacing = (_uint(lacing_el) != 0) if lacing_el is not None else True
                scale = (_raw_uint(scale_el, "1", "track-timecode-scale")
                         if scale_el is not None else
                         Raw(1, "1", "track-timecode-scale", None, "default"))
                offset = (_raw_uint(offset_el, "tick",
                                    "cluster-timecode")
                          if offset_el is not None else
                          Raw(0, "tick", "cluster-timecode", None, "default"))
                default_dur = (_raw_uint(dur_el, "ns", "segment-timecode-scale")
                               if dur_el is not None else
                               Raw(None, "ns", "segment-timecode-scale",
                                   None, "absent", False,
                                   [Diagnostic("DEFAULT_DURATION_ABSENT",
                                               "no DefaultDuration", "info",
                                               subject=sid)]))
                tracks.append(Track(
                    sid, number,
                    _uint(uid_el) if uid_el else None,
                    _uint(type_el) if type_el else None,
                    codec_el.payload().decode("utf-8", "replace")
                    if codec_el else None,
                    name_el.payload().decode("utf-8", "replace")
                    if name_el else None,
                    flag_lacing, scale, offset, default_dur, sampling))

        for ce in segment.find_all(ids.CLUSTER):
            csid = stable_id("cluster", ce.id_offset)
            tc = ce.find(ids.TIMESTAMP)
            cluster = Cluster(csid, ce, _uint(tc) if tc else None,
                              ce.unknown_length)
            for holder in ce.children:
                if holder.id == ids.SIMPLE_BLOCK:
                    bs = stable_id("block", holder.id_offset)
                    parsed = parse_block(holder.payload())
                    cluster.blocks.append(Block(
                        bs, csid, "SimpleBlock", holder,
                        parsed.track_number, None, None, [], None, parsed))
                    for d in parsed.diagnostics:
                        d.subject = bs
                    diagnostics.extend(parsed.diagnostics)
                elif holder.id == ids.BLOCK_GROUP:
                    be = holder.find(ids.BLOCK)
                    if be is None:
                        diagnostics.append(Diagnostic(
                            "BLOCKGROUP_NO_BLOCK",
                            "BlockGroup without Block element", "warning",
                            evidence={"offset": holder.id_offset}))
                        continue
                    bs = stable_id("block", be.id_offset)
                    parsed = parse_block(be.payload())
                    dur_el = holder.find(ids.BLOCK_DURATION)
                    refs = [_uint(r) for r in holder.find_all(ids.REFERENCE_BLOCK)]
                    discard, dp_diag = _extract_discard_padding(holder, bs)
                    diagnostics.extend(dp_diag)
                    for d in parsed.diagnostics:
                        d.subject = bs
                    diagnostics.extend(parsed.diagnostics)
                    cluster.blocks.append(Block(
                        bs, csid, "Block", be, parsed.track_number, holder,
                        _uint(dur_el) if dur_el else None,
                        [r for r in refs if r is not None], discard, parsed))
            cluster.blocks.sort(key=lambda b: b.element.id_offset)
            clusters.append(cluster)
        clusters.sort(key=lambda c: c.element.id_offset)

        cues_el = segment.find(ids.CUES)
        if cues_el is not None:
            for cp in cues_el.find_all(ids.CUE_POINT):
                ct = cp.find(ids.CUE_TIME)
                ctp = cp.find(ids.CUE_TRACK_POSITIONS)
                track_no = None
                cpos = None
                rpos = None
                if ctp is not None:
                    t = ctp.find(ids.CUE_TRACK)
                    cl = ctp.find(ids.CUE_CLUSTER_POSITION)
                    rp = ctp.find(ids.CUE_RELATIVE_POSITION)
                    track_no = _uint(t) if t else None
                    cpos = _uint(cl) if cl else None
                    rpos = _uint(rp) if rp else None
                cues.append(Cue(
                    stable_id("cue", cp.id_offset),
                    _uint(ct) if ct else None, track_no, cpos, rpos,
                    (cp.id_offset, cp.elem_end)))

    return Document(blob, roots, segment, doc_type, timecode_scale,
                    duration_raw, tracks, clusters, cues, diagnostics)
