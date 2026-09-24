"""Parse Matroska/WebM bytes + frame-summary sidecar into raw facts.

Nothing here is normalized beyond what the format itself states.  Times are
kept as ticks with their basis; offsets stay byte coordinates.  The derive
layer (tribunal.derive) converts units.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict

from . import ebml
from .ebml import Element
from .blocks import Block, decode_block, attach_blockgroup_details


@dataclass
class Diagnostic:
    code: str
    severity: str           # info | warning | error
    message: str
    byte_range: list[int] | None = None
    object_id: str | None = None
    rule: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TrackRaw:
    track_number: int
    uid: int | None
    track_type: int | None
    type_name: str
    codec_id: str | None
    name: str | None
    flag_lacing: bool | None
    default_duration_ns: int | None      # DefaultDuration is already nanoseconds
    track_scale: float | None           # TrackTimecodeScale (raw float)
    element_range: list[int]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClusterRaw:
    cluster_id: str
    id_offset: int
    data_offset: int
    size: int                           # -1 for unknown
    known_size: bool
    timecode_ticks: int | None
    prev_size: int | None
    byte_range: list[int]
    blocks: list[Block] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "id_offset": self.id_offset,
            "data_offset": self.data_offset,
            "size": self.size,
            "known_size": self.known_size,
            "timecode_ticks": self.timecode_ticks,
            "prev_size": self.prev_size,
            "byte_range": self.byte_range,
            "blocks": [b.as_dict() for b in self.blocks],
        }


@dataclass
class CueRaw:
    cue_id: str
    cue_time_ticks: int
    track_number: int
    cluster_position: int
    relative_position: int | None
    cue_duration: int | None
    element_range: list[int]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class FrameRow:
    row_id: str
    track_number: int | None
    raw: dict
    present_ns: int | None
    diagnostic: str | None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TreeNode:
    node_id: str
    name: str
    eid_hex: str
    id_offset: int
    size_offset: int
    data_offset: int
    size: int
    known_size: bool
    parse_error: str | None
    value_preview: str | None
    byte_range: list[int | None]
    children: list["TreeNode"] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "node_id": self.node_id, "name": self.name, "eid_hex": self.eid_hex,
            "id_offset": self.id_offset, "size_offset": self.size_offset,
            "data_offset": self.data_offset, "size": self.size,
            "known_size": self.known_size, "parse_error": self.parse_error,
            "value_preview": self.value_preview, "byte_range": self.byte_range,
            "children": [c.as_dict() for c in self.children],
        }


@dataclass
class Document:
    source_sha256: str
    byte_length: int
    segment: dict
    timecode_scale_ns: int
    duration_ticks: float | None
    tracks: list[TrackRaw]
    clusters: list[ClusterRaw]
    cues: list[CueRaw]
    frames: list[FrameRow]
    tree: list[TreeNode]
    diagnostics: list[Diagnostic]

    def blocks(self) -> list[tuple[ClusterRaw, Block]]:
        out = []
        for cluster in self.clusters:
            for block in cluster.blocks:
                out.append((cluster, block))
        return out

    def track(self, number: int) -> TrackRaw | None:
        for track in self.tracks:
            if track.track_number == number:
                return track
        return None

    def as_dict(self) -> dict:
        return {
            "source_sha256": self.source_sha256,
            "byte_length": self.byte_length,
            "segment": self.segment,
            "timecode_scale_ns": self.timecode_scale_ns,
            "duration_ticks": self.duration_ticks,
            "tracks": [t.as_dict() for t in self.tracks],
            "clusters": [c.as_dict() for c in self.clusters],
            "cues": [c.as_dict() for c in self.cues],
            "frames": [f.as_dict() for f in self.frames],
            "tree": [n.as_dict() for n in self.tree],
            "diagnostics": [d.as_dict() for d in self.diagnostics],
        }


TRACK_TYPES = {1: "video", 2: "audio", 3: "complex", 16: "subtitle", 17: "buttons",
               18: "control", 32: "metadata"}


def _preview(el: Element) -> str | None:
    sp = ebml.spec(el.eid)
    try:
        if sp.etype == "u":
            return str(ebml.decode_uint(el))
        if sp.etype == "s":
            return str(ebml.decode_sint(el))
        if sp.etype == "f":
            return str(ebml.decode_float(el))
        if sp.etype == "8":
            return repr(ebml.decode_string(el))
        if sp.etype == "b" and el.name in ("Block", "SimpleBlock"):
            return f"<{len(el.raw)} bytes>"
    except Exception:
        return None
    if sp.etype == "b":
        return f"<{len(el.raw)} bytes>"
    return None


def _tree(el: Element) -> TreeNode:
    end = el.end_offset
    node = TreeNode(
        node_id=f"{el.name}@{el.id_offset}",
        name=el.name, eid_hex=f"0x{el.eid:X}",
        id_offset=el.id_offset, size_offset=el.size_offset,
        data_offset=el.data_offset,
        size=el.size if el.known_size else -1,
        known_size=el.known_size, parse_error=el.parse_error,
        value_preview=_preview(el),
        byte_range=[el.id_offset, end],
    )
    node.children = [_tree(c) for c in el.children]
    return node


def parse_matroska(data: bytes, sidecar: dict | None = None) -> Document:
    diagnostics: list[Diagnostic] = []
    sidecar = sidecar or {}
    digest = hashlib.sha256(data).hexdigest()

    roots = ebml.parse_document(data)
    segment = next((e for e in roots if e.name == "Segment"), None)
    if segment is None:
        raise ValueError("no Segment element")
    segment_children = ebml.parse_segment_children(segment, data)

    scale_ns = 1_000_000
    duration = None
    tracks: list[TrackRaw] = []
    clusters: list[ClusterRaw] = []
    cues: list[CueRaw] = []

    info = next((e for e in segment_children if e.name == "Info"), None)
    if info is None:
        diagnostics.append(Diagnostic("E-INFO-MISSING", "error",
                                      "no Info element in Segment"))
    else:
        tc = info.get_child("TimecodeScale")
        if tc is not None:
            scale_ns = ebml.decode_uint(tc)
        else:
            diagnostics.append(Diagnostic(
                "E-SCALE-DEFAULT", "info",
                "TimecodeScale missing; raw default 1,000,000 ns retained "
                "by convention", object_id="timecode_scale",
                rule="R-UNIT-RAW"))
        dur_el = info.get_child("Duration")
        if dur_el is not None:
            duration = ebml.decode_float(dur_el)

    tracks_el = next((e for e in segment_children if e.name == "Tracks"), None)
    if tracks_el is not None:
        for entry in tracks_el.find_all("TrackEntry"):
            num_el = entry.get_child("TrackNumber")
            number = ebml.decode_uint(num_el) if num_el else -1
            uid = typ = codec = name = flag = None
            default_dur = track_scale = None
            if (e := entry.get_child("TrackUID")) is not None:
                uid = ebml.decode_uint(e)
            if (e := entry.get_child("TrackType")) is not None:
                typ = ebml.decode_uint(e)
            if (e := entry.get_child("CodecID")) is not None:
                codec = ebml.decode_string(e)
            if (e := entry.get_child("Name")) is not None:
                name = ebml.decode_string(e)
            if (e := entry.get_child("FlagLacing")) is not None:
                flag = bool(ebml.decode_uint(e))
            if (e := entry.get_child("DefaultDuration")) is not None:
                default_dur = ebml.decode_uint(e)
            if (e := entry.get_child("TimecodeScale")) is not None:
                track_scale = ebml.decode_float(e)
                if track_scale <= 0:
                    diagnostics.append(Diagnostic(
                        "E-TRACK-SCALE", "error",
                        f"track {number} TrackTimecodeScale {track_scale} "
                        "is not convertible; retained raw",
                        byte_range=[e.id_offset, e.end_offset],
                        object_id=f"track:{number}", rule="R-UNIT-RAW"))
                    track_scale = None
            tracks.append(TrackRaw(
                track_number=number, uid=uid, track_type=typ,
                type_name=TRACK_TYPES.get(typ, f"type{typ}"),
                codec_id=codec, name=name, flag_lacing=flag,
                default_duration_ns=default_dur, track_scale=track_scale,
                element_range=[entry.id_offset, entry.end_offset]))

    for seg_child in segment_children:
        if seg_child.name != "Cluster":
            continue
        cluster_el = seg_child
        cid = f"cluster@{cluster_el.data_offset}"
        tc_el = cluster_el.get_child("Timecode")
        tc = ebml.decode_uint(tc_el) if tc_el is not None else None
        if tc is None:
            diagnostics.append(Diagnostic(
                "E-CLUSTER-NO-TC", "error",
                f"cluster at {cluster_el.data_offset} has no Timecode",
                byte_range=[cluster_el.id_offset, cluster_el.end_offset],
                object_id=cid, rule="R-UNIT-RAW"))
        prev = cluster_el.get_child("PrevSize")
        cluster = ClusterRaw(
            cluster_id=cid, id_offset=cluster_el.id_offset,
            data_offset=cluster_el.data_offset,
            size=cluster_el.size if cluster_el.known_size else -1,
            known_size=cluster_el.known_size,
            timecode_ticks=tc,
            prev_size=ebml.decode_uint(prev) if prev is not None else None,
            byte_range=[cluster_el.id_offset, cluster_el.end_offset
                        if cluster_el.end_offset is not None else len(data)])
        block_index = 0
        for child in cluster_el.children:
            if child.name == "SimpleBlock":
                block = decode_block(child, "SimpleBlock",
                                    cluster_el.data_offset, block_index)
                cluster.blocks.append(block)
                block_index += 1
            elif child.name == "BlockGroup":
                block_el = child.get_child("Block")
                if block_el is not None:
                    block = decode_block(block_el, "Block",
                                        cluster_el.data_offset, block_index)
                    attach_blockgroup_details(block, child)
                    cluster.blocks.append(block)
                    block_index += 1
                else:
                    diagnostics.append(Diagnostic(
                        "E-BLOCKGROUP-EMPTY", "error",
                        "BlockGroup without Block",
                        byte_range=[child.id_offset, child.end_offset],
                        object_id=cid))
        for block in cluster.blocks:
            if block.lacing.damaged:
                diagnostics.append(Diagnostic(
                    "E-LACING-DAMAGED", "error",
                    f"{block.block_id}: {'; '.join(block.lacing.errors)}",
                    byte_range=[block.byte_range[0], block.byte_range[1]],
                    object_id=block.block_id, rule="R-LACING-NO-GUESS"))
        clusters.append(cluster)

    cues_el = next((e for e in segment_children if e.name == "Cues"), None)
    if cues_el is not None:
        for point in cues_el.find_all("CuePoint"):
            cue_time = ebml.decode_uint(point.get_child("CueTime"))
            for pos in point.find_all("CueTrackPositions"):
                track_no = ebml.decode_uint(pos.get_child("CueTrack"))
                cpos_el = pos.get_child("CueClusterPosition")
                cpos = ebml.decode_uint(cpos_el)
                relp = durp = None
                if (e := pos.get_child("CueRelativePosition")) is not None:
                    relp = ebml.decode_uint(e)
                if (e := pos.get_child("CueDuration")) is not None:
                    durp = ebml.decode_uint(e)
                cues.append(CueRaw(
                    cue_id=f"cuepoint@{point.id_offset}:track{track_no}",
                    cue_time_ticks=cue_time, track_number=track_no,
                    cluster_position=cpos, relative_position=relp,
                    cue_duration=durp,
                    element_range=[point.id_offset, point.end_offset]))

    if not clusters and not cues:
        diagnostics.append(Diagnostic(
            "E-EMPTY-SEGMENT", "warning",
            "segment contains neither clusters nor cues"))

    frames = _parse_sidecar(sidecar, tracks, diagnostics)
    tree = [_tree(e) for e in roots]

    return Document(
        source_sha256=digest, byte_length=len(data),
        segment={
            "id_offset": segment.id_offset,
            "data_offset": segment.data_offset,
            "size": segment.size if segment.known_size else -1,
            "known_size": segment.known_size,
        },
        timecode_scale_ns=scale_ns, duration_ticks=duration,
        tracks=tracks, clusters=clusters, cues=cues, frames=frames,
        tree=tree, diagnostics=diagnostics)


def _parse_sidecar(sidecar: dict, tracks: list[TrackRaw],
                    diagnostics: list[Diagnostic]) -> list[FrameRow]:
    rows: list[FrameRow] = []
    known = {t.track_number for t in tracks}
    for i, item in enumerate(sidecar.get("frames", [])):
        row_id = f"frame:{i}"
        raw = dict(item)
        track_no = item.get("track_number")
        present = item.get("presentation_ns")
        problem = None
        if track_no not in known:
            problem = (f"frame row {i}: track_number {track_no} not present "
                       "in container; raw row retained")
            diagnostics.append(Diagnostic(
                "E-FRAME-ORPHAN-TRACK", "warning", problem,
                object_id=row_id, rule="R-UNIT-RAW"))
            present = None
        elif present is None:
            pts = item.get("presentation_time")
            if pts is None:
                problem = f"frame row {i}: no presentation time; raw retained"
                diagnostics.append(Diagnostic(
                    "E-FRAME-NO-PTS", "warning", problem, object_id=row_id,
                    rule="R-UNIT-RAW"))
            else:
                problem = (f"frame row {i}: presentation_time has no "
                           "convertible ns representation; raw retained")
                diagnostics.append(Diagnostic(
                    "E-FRAME-UNCONVERTIBLE", "warning", problem,
                    object_id=row_id, rule="R-UNIT-RAW"))
        rows.append(FrameRow(row_id=row_id, track_number=track_no, raw=raw,
                            present_ns=present, diagnostic=problem))
    return rows


def _cue_from_dict(d: dict) -> CueRaw:
    return CueRaw(
        cue_id=d["cue_id"], cue_time_ticks=d["cue_time_ticks"],
        track_number=d["track_number"], cluster_position=d["cluster_position"],
        relative_position=d["relative_position"],
        cue_duration=d["cue_duration"], element_range=d["element_range"])


def _tree_from_dict(d: dict) -> TreeNode:
    node = TreeNode(
        node_id=d["node_id"], name=d["name"], eid_hex=d["eid_hex"],
        id_offset=d["id_offset"], size_offset=d["size_offset"],
        data_offset=d["data_offset"], size=d["size"],
        known_size=d["known_size"], parse_error=d["parse_error"],
        value_preview=d["value_preview"], byte_range=d["byte_range"])
    node.children = [_tree_from_dict(c) for c in d["children"]]
    return node


def document_from_dict(d: dict) -> Document:
    tracks = [TrackRaw(**{k: t[k] for k in (
        "track_number", "uid", "track_type", "type_name", "codec_id",
        "name", "flag_lacing", "default_duration_ns", "track_scale",
        "element_range")}) for t in d["tracks"]]
    clusters = []
    for c in d["clusters"]:
        cluster = ClusterRaw(
            cluster_id=c["cluster_id"], id_offset=c["id_offset"],
            data_offset=c["data_offset"], size=c["size"],
            known_size=c["known_size"], timecode_ticks=c["timecode_ticks"],
            prev_size=c["prev_size"], byte_range=c["byte_range"])
        cluster.blocks = [Block.from_dict(b) for b in c["blocks"]]
        clusters.append(cluster)
    doc = Document(
        source_sha256=d["source_sha256"], byte_length=d["byte_length"],
        segment=d["segment"], timecode_scale_ns=d["timecode_scale_ns"],
        duration_ticks=d.get("duration_ticks"), tracks=tracks,
        clusters=clusters,
        cues=[_cue_from_dict(c) for c in d["cues"]],
        frames=[FrameRow(**f) for f in d["frames"]],
        tree=[_tree_from_dict(t) for t in d["tree"]],
        diagnostics=[Diagnostic(**di) for di in d["diagnostics"]])
    return doc
