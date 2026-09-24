"""从 EBML 元素树抽取领域事实对象（轨道/簇/块/cue）。

事实层只搬运字节级证据并附单位；时间规范化（ns）在 domain 层做，
原始表示始终保留（raw_tc / raw_scale / unit）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from . import blocks as B
from .parser import EbmlElement

ID = {
    "segment": 0x18538067, "info": 0x1549A966, "tracks": 0x1654AE6B,
    "track_entry": 0xAE, "cluster": 0x1F43B675, "tc": 0xE7,
    "simple_block": 0xA3, "block_group": 0xA0, "block": 0xA1,
    "block_duration": 0x9B, "discard_padding": 0x75A2,
    "reference_block": 0xFB, "cues": 0x1C53BB6B, "cue_point": 0xBB,
    "cue_time": 0xB3, "cue_track_pos": 0xB7, "cue_track": 0xF7,
    "cue_cluster_pos": 0xF1, "cue_block_number": 0x5378,
    "cue_duration": 0xB2, "timecode_scale": 0x2AD7B1, "duration": 0x4489,
    "track_number": 0xD7, "track_uid": 0x73C5, "track_type": 0x83,
    "codec_id": 0x86, "default_duration": 0x23E383,
}


@dataclass
class Fact:
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FrameSpan(Fact):
    index: int
    byte_start: int          # 相对文件
    byte_end: int
    proven: bool             # False=边界未被严格证明（lacing 损坏）


@dataclass
class BlockFact(Fact):
    block_key: str           # 元素稳定身份
    cluster_key: str
    block_kind: str          # simple | blockgroup
    element_start: int
    raw_end: int
    track_number: int
    rel_tc_raw: int          # 无符号原始 16 位表示
    rel_tc_signed: int       # 有符号解释
    flags: int
    lacing: str
    frame_count: Optional[int]
    frames: list[FrameSpan]
    block_duration_ticks: Optional[int]
    discard_padding_ns: Optional[int]
    reference_block_rel: Optional[int]
    lace_diags: list[dict]
    parse_ok: bool


@dataclass
class ClusterFact(Fact):
    cluster_key: str
    element_start: int
    data_start: int
    raw_end: int
    unknown_size: bool
    inferred_end: bool
    tc_raw: int
    blocks: list[BlockFact]


@dataclass
class TrackFact(Fact):
    track_number: int
    track_uid: Optional[int]
    track_type: Optional[int]
    codec_id: Optional[str]
    default_duration_ns: Optional[int]
    element_start: int


@dataclass
class CueFact(Fact):
    cue_key: str
    element_start: int
    cue_time_ticks: int
    track_number: int
    cluster_pos_raw: int                 # 原始坐标：segment-data-relative
    block_number: Optional[int]
    duration_ticks: Optional[int]


@dataclass
class SegmentFacts:
    segment_start: int
    segment_data_origin: int
    segment_unknown_size: bool
    timecode_scale_raw: Optional[int]
    duration_ticks: Optional[float]
    tracks: list[TrackFact]
    clusters: list[ClusterFact]
    cues: list[CueFact]
    element_count: int


def _children(els: dict[str, EbmlElement], key: Optional[str]) -> list[EbmlElement]:
    if key is None:
        return []
    parent = els.get(key)
    return [els[k] for k in parent.children if k in els]


def _child_by_id(els, key, eid) -> Optional[EbmlElement]:
    for c in _children(els, key):
        if c.id == eid:
            return c
    return None


def _find_all(elements: list[EbmlElement], eid: int,
              parent: Optional[EbmlElement] = None) -> list[EbmlElement]:
    out = []
    for e in elements:
        if e.id == eid and (parent is None or e.parent_key == parent.key):
            out.append(e)
    return out


def extract(elements: list[EbmlElement]) -> SegmentFacts:
    els = {e.key: e for e in elements}
    seg = next((e for e in elements if e.id == ID["segment"]), None)
    if seg is None:
        raise ValueError("未找到 Segment 元素")

    info = next((e for e in elements if e.id == ID["info"]
                 and e.parent_key == seg.key), None)
    tcs_el = _child_by_id(els, info.key if info else None, ID["timecode_scale"])
    dur_el = _child_by_id(els, info.key if info else None, ID["duration"])

    tracks: list[TrackFact] = []
    tracks_parent = next((e for e in elements if e.id == ID["tracks"]
                          and e.parent_key == seg.key), None)
    for te in _children(els, tracks_parent.key if tracks_parent else None):
        if te.id != ID["track_entry"]:
            continue
        tnum = _child_by_id(els, te.key, ID["track_number"])
        tuid = _child_by_id(els, te.key, ID["track_uid"])
        ttype = _child_by_id(els, te.key, ID["track_type"])
        codec = _child_by_id(els, te.key, ID["codec_id"])
        dd = _child_by_id(els, te.key, ID["default_duration"])
        tracks.append(TrackFact(
            track_number=tnum.value if tnum else -1,
            track_uid=tuid.value if tuid else None,
            track_type=ttype.value if ttype else None,
            codec_id=codec.value if codec else None,
            default_duration_ns=dd.value if dd else None,
            element_start=te.id_start,
        ))

    clusters: list[ClusterFact] = []
    for ce in elements:
        if ce.id != ID["cluster"] or ce.parent_key != seg.key:
            continue
        tcel = _child_by_id(els, ce.key, ID["tc"])
        tc_raw = tcel.value if tcel and tcel.value is not None else 0
        blocks: list[BlockFact] = []
        for ch in _children(els, ce.key):
            if ch.id == ID["simple_block"]:
                blocks.append(_block_fact(ch, ce.key, "simple"))
            elif ch.id == ID["block_group"]:
                blk = _child_by_id(els, ch.key, ID["block"])
                if blk is not None:
                    bf = _block_fact(blk, ce.key, "blockgroup",
                                     wrapper=ch, els=els)
                    blocks.append(bf)
        clusters.append(ClusterFact(
            cluster_key=ce.key, element_start=ce.id_start,
            data_start=ce.data_start, raw_end=ce.raw_end,
            unknown_size=ce.unknown_size, inferred_end=ce.inferred_end,
            tc_raw=tc_raw, blocks=blocks,
        ))

    cues: list[CueFact] = []
    cues_parent = next((e for e in elements if e.id == ID["cues"]
                        and e.parent_key == seg.key), None)
    for cp in _children(els, cues_parent.key if cues_parent else None):
        if cp.id != ID["cue_point"]:
            continue
        ctime = _child_by_id(els, cp.key, ID["cue_time"])
        for pos in _children(els, cp.key):
            if pos.id != ID["cue_track_pos"]:
                continue
            t = _child_by_id(els, pos.key, ID["cue_track"])
            cc = _child_by_id(els, pos.key, ID["cue_cluster_pos"])
            bn = _child_by_id(els, pos.key, ID["cue_block_number"])
            cd = _child_by_id(els, pos.key, ID["cue_duration"])
            cues.append(CueFact(
                cue_key=cp.key, element_start=cp.id_start,
                cue_time_ticks=ctime.value if ctime else 0,
                track_number=t.value if t else -1,
                cluster_pos_raw=cc.value if cc else 0,
                block_number=bn.value if bn else None,
                duration_ticks=cd.value if cd else None,
            ))

    return SegmentFacts(
        segment_start=seg.id_start,
        segment_data_origin=seg.data_start,
        segment_unknown_size=seg.unknown_size,
        timecode_scale_raw=tcs_el.value if tcs_el else None,
        duration_ticks=dur_el.value if dur_el else None,
        tracks=tracks, clusters=clusters, cues=cues,
        element_count=len(elements),
    )


def _block_fact(el: EbmlElement, cluster_key: str, kind: str,
                wrapper: Optional[EbmlElement] = None,
                els: Optional[dict] = None) -> BlockFact:
    raw = bytes.fromhex(el.value_raw_hex or "")
    try:
        bp = B.parse_block(raw)
        parse_ok = not any(d.code == "LACING_CORRUPT" or
                           d.code == "LACING_HEADER_MISSING" or
                           d.code == "LACING_RESERVED"
                           for d in bp.diagnostics)
    except ValueError:
        bp = None
        parse_ok = False

    duration = pad = ref = None
    if wrapper is not None and els is not None:
        d = _child_by_id(els, wrapper.key, ID["block_duration"])
        p = _child_by_id(els, wrapper.key, ID["discard_padding"])
        r = _child_by_id(els, wrapper.key, ID["reference_block"])
        duration = d.value if d else None
        pad = p.value if p else None
        ref = r.value if r else None

    frames = []
    if bp is not None:
        for i, (s, e) in enumerate(bp.frame_ranges):
            frames.append(FrameSpan(
                index=i,
                byte_start=el.id_start + s,
                byte_end=el.id_start + e, proven=True))
    return BlockFact(
        block_key=el.key, cluster_key=cluster_key, block_kind=kind,
        element_start=el.id_start, raw_end=el.raw_end,
        track_number=bp.track_number if bp else -1,
        rel_tc_raw=int.from_bytes(bytes.fromhex(bp.rel_tc_raw_hex), "big")
        if bp else 0,
        rel_tc_signed=bp.rel_tc_signed if bp else 0,
        flags=bp.flags if bp else 0,
        lacing=bp.lacing_name if bp else "unparsed",
        frame_count=bp.frame_count if bp else None,
        frames=frames,
        block_duration_ticks=duration,
        discard_padding_ns=pad,
        reference_block_rel=ref,
        lace_diags=[d.to_dict() for d in bp.diagnostics] if bp else
        [{"code": "BLOCK_UNREADABLE", "byte_start": 0, "byte_end": 0,
          "message": "block 头部无法解析"}],
        parse_ok=parse_ok,
    )
