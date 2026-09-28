"""Matroska 语义层：从 EBML 树提取轨道 / cluster / block / cue / 缺口事实。

所有域量都带单位或坐标基准：
* cluster.timecode_raw 是 TimecodeScale ticks（int，无单位漂移）；
* block.relative_ms_raw 是 Block 本地 16 位有符号毫秒；
* *_ns 是派生层规范化后的纳秒，且记录 basis（segment 或 legacy track）；
* TrackTimecodeScale != 1.0 时轨道使用 legacy 时基，保留并报警，
  多轨时间线不按文件顺序强对齐。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from . import blocks
from .parser import EBMLElement, Diagnostic


@dataclass
class Q:
    """带单位/基准的量。value 是规范化值，raw 是导入时原始表示。"""
    value: object
    unit: str
    basis: str
    raw: object = None
    raw_unit: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class FrameFact:
    block_id: str
    track_number: int
    index: int
    start_ns: int | None
    duration_ns: int | None
    basis: str
    provisional: bool  # 来自非唯一分帧时为 True


@dataclass
class BlockFact:
    block_id: str
    cluster_id: str
    element_offset: int
    byte_end: int
    container: str  # SimpleBlock / BlockGroup
    group_offset: int | None
    track_number: int
    relative_ms_raw: int
    relative_ms_unsigned: int
    flags: int
    keyframe: bool
    start_ns_segment: int
    basis: str
    lacing_mode: str
    lacing_definitive: bool
    frame_candidates: list[list[int]]  # 每个候选的帧尺寸列表
    candidate_counts: list[int]
    selected_candidate: int | None
    damaged: bool
    damage_code: str | None
    damage_offset: int | None
    damage_end: int | None
    block_duration_raw: int | None  # ticks
    block_duration_ns: int | None
    reference_blocks: list[int]
    discard_padding_raw: int | None  # signed int, 单位 ns（元素自带）
    discard_padding_ns: int | None
    header_error: str | None


@dataclass
class ClusterFact:
    cluster_id: str
    element_offset: int
    byte_end: int
    unknown_length: bool
    timecode_raw: int | None  # TimecodeScale ticks
    start_ns_segment: int | None
    block_ids: list[str]


@dataclass
class TrackFact:
    track_id: str
    element_offset: int
    number: int
    uid: int | None
    kind: str
    codec: str | None
    name: str | None
    default_duration_raw: int | None  # ns
    timecode_scale_raw: float | None  # 旧 TrackTimecodeScale
    legacy_timebase: bool
    flag_lacing: bool


@dataclass
class CueFact:
    cue_id: str
    element_offset: int
    cue_time_raw: int | None
    cue_time_ns_segment: int | None
    track_number: int | None
    cluster_position_raw: int | None  # Segment 相对偏移
    resolved_cluster_id: str | None
    resolved_block_offset: int | None
    pointed_start_ns: int | None
    status: str  # ok / mispoint / missing-position / unresolvable
    message: str
    duration_raw: int | None


@dataclass
class GapFact:
    gap_id: str
    earlier_cluster_id: str
    later_cluster_id: str
    kind: str  # gap / backwards
    span_ns: int
    earlier_start_ns: int | None
    later_start_ns: int | None


@dataclass
class SegmentFact:
    element_offset: int
    byte_end: int
    unknown_length: bool
    timecode_scale_raw: int  # ns per tick
    duration_raw: float | None


@dataclass
class MatroskaFactSet:
    segment: Optional[SegmentFact]
    tracks: list[TrackFact]
    clusters: list[ClusterFact]
    blocks: list[BlockFact]
    frames: list[FrameFact]
    cues: list[CueFact]
    gaps: list[GapFact]
    diagnostics: list[Diagnostic] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)

    def track_by_number(self, number: int) -> TrackFact | None:
        for track in self.tracks:
            if track.number == number:
                return track
        return None


TRACK_TYPE_NAMES = {1: "video", 2: "audio", 17: "subtitle", 0: "unknown"}


def _segment_of(doc_elements: list[EBMLElement]) -> EBMLElement | None:
    for el in doc_elements:
        if el.name == "Segment":
            return el
    return None


def build_fact_set(doc_elements: list[EBMLElement],
                   doc_diagnostics: list[Diagnostic],
                   gap_threshold_ns: int = 1_000_000_000) -> MatroskaFactSet:
    """从 EBML 树提取全部领域事实。"""
    warnings: list[Diagnostic] = []
    segment = _segment_of(doc_elements)
    if segment is None:
        warnings.append(Diagnostic("NO_SEGMENT", "文档中没有 Segment 元素", 0))
        return MatroskaFactSet(None, [], [], [], [], [], [],
                               list(doc_diagnostics), warnings)
    segment_data_start = segment.data_offset
    seg_unknown = segment.declared_length is None

    info = segment.child_by_name("Info")
    scale_raw = 1_000_000
    duration_raw = None
    if info is not None:
        tcs = info.child_by_name("TimecodeScale")
        if tcs is not None and isinstance(tcs.value, int):
            scale_raw = tcs.value
        dur = info.child_by_name("Duration")
        if dur is not None:
            duration_raw = dur.value
    if scale_raw <= 0:
        warnings.append(Diagnostic(
            "BAD_TIMESCALE",
            "TimecodeScale=%r 非法，留在原位并标记，不做规范化" % scale_raw,
            info.offset if info else 0))
        scale_raw = 1_000_000
    segment_fact = SegmentFact(segment.offset, segment.effective_end,
                               seg_unknown, scale_raw, duration_raw)

    tracks = _extract_tracks(segment, warnings)
    clusters, blocks, frames = _extract_clusters(
        segment, tracks, scale_raw, warnings)
    cues = _extract_cues(segment, segment_data_start, clusters, blocks,
                         tracks, scale_raw, warnings)
    gaps = _detect_gaps(clusters, gap_threshold_ns)

    return MatroskaFactSet(
        segment_fact, tracks, clusters, blocks, frames, cues, gaps,
        list(doc_diagnostics), warnings)


def _extract_tracks(segment: EBMLElement,
                    warnings: list[Diagnostic]) -> list[TrackFact]:
    tracks_root = segment.child_by_name("Tracks")
    out: list[TrackFact] = []
    if tracks_root is None:
        return out
    for entry in tracks_root.children_by_name("TrackEntry"):
        number_el = entry.child_by_name("TrackNumber")
        number = number_el.value if number_el else len(out) + 1
        kind = TRACK_TYPE_NAMES.get(
            entry.child_by_name("TrackType").value
            if entry.child_by_name("TrackType") is not None else 0,
            "other")
        ttcs_el = entry.child_by_name("TrackTimecodeScale")
        ttcs = ttcs_el.value if ttcs_el is not None else None
        legacy = ttcs is not None and abs(float(ttcs) - 1.0) > 1e-12
        if legacy:
            warnings.append(Diagnostic(
                "LEGACY_TRACK_TIMEBASE",
                "轨道 %d 使用已弃用 TrackTimecodeScale=%r，时间线采用轨道本地时基"
                % (number, ttcs),
                ttcs_el.offset))
        flag_lacing_el = entry.child_by_name("FlagLacing")
        out.append(TrackFact(
            track_id="track@%d" % entry.offset,
            element_offset=entry.offset,
            number=number,
            uid=entry.child_by_name("TrackUID").value
            if entry.child_by_name("TrackUID") else None,
            kind=kind,
            codec=entry.child_by_name("CodecID").value
            if entry.child_by_name("CodecID") else None,
            name=entry.child_by_name("Name").value
            if entry.child_by_name("Name") else None,
            default_duration_raw=entry.child_by_name("DefaultDuration").value
            if entry.child_by_name("DefaultDuration") is not None else None,
            timecode_scale_raw=ttcs,
            legacy_timebase=legacy,
            flag_lacing=bool(flag_lacing_el.value)
            if flag_lacing_el is not None else True,
        ))
    return out


def _block_start_ns(cluster_ticks: int | None, rel_ms: int,
                    scale_ns: int, ttcs: float | None) -> tuple[int, str]:
    base = (cluster_ticks or 0) * scale_ns
    rel_ns = rel_ms * 1_000_000
    if ttcs is not None:
        rel_ns = int(round(rel_ns * float(ttcs)))
        return base + rel_ns, "segment-scale + legacy TrackTimecodeScale"
    return base + rel_ns, "segment TimecodeScale"


def _frame_times(start_ns: int, count: int, block_duration_ns: int | None,
                 default_duration_ns: int | None
                 ) -> list[tuple[int | None, int | None]]:
    if block_duration_ns is not None:
        base = block_duration_ns // count
        rem = block_duration_ns % count
        out = []
        elapsed = 0
        for i in range(count):
            dur = base + (1 if i < rem else 0)
            out.append((start_ns + elapsed, dur))
            elapsed += dur
        return out
    if default_duration_ns is not None:
        return [(start_ns + i * default_duration_ns, default_duration_ns)
                for i in range(count)]
    return [(start_ns, None)] + [(None, None)] * (count - 1)


def _extract_clusters(segment: EBMLElement, tracks: list[TrackFact],
                      scale_ns: int,
                      warnings: list[Diagnostic]
                      ) -> tuple[list[ClusterFact], list[BlockFact],
                                 list[FrameFact]]:
    clusters: list[ClusterFact] = []
    blocks: list[BlockFact] = []
    frames: list[FrameFact] = []
    for cluster_el in [c for c in segment.children if c.name == "Cluster"]:
        cluster_id = "cluster@%d" % cluster_el.offset
        tc_el = cluster_el.child_by_name("Timecode")
        cluster_ticks = tc_el.value if tc_el is not None else None
        start_ns = (cluster_ticks * scale_ns
                    if isinstance(cluster_ticks, int) else None)
        block_ids: list[str] = []
        for child in cluster_el.children:
            if child.name not in ("SimpleBlock", "BlockGroup"):
                continue
            groups = [child] if child.name == "SimpleBlock" else [
                g for g in child.children if g.name == "Block"]
            for idx, block_el in enumerate(groups):
                group_el = None if child.name == "SimpleBlock" else child
                bid = "block@%d" % block_el.offset
                block_ids.append(bid)
                data: bytes = block_el.value or b""
                header, err = blocks.parse_header(
                    data, simple=child.name == "SimpleBlock")
                bf = _build_block(bid, cluster_id, block_el, group_el, data,
                                  header, err, tracks, cluster_ticks,
                                  scale_ns, warnings)
                blocks.append(bf)
                if header is not None and not bf.damaged:
                    frames.extend(_build_frames(bf, tracks))
        clusters.append(ClusterFact(
            cluster_id=cluster_id,
            element_offset=cluster_el.offset,
            byte_end=cluster_el.effective_end,
            unknown_length=cluster_el.declared_length is None,
            timecode_raw=cluster_ticks,
            start_ns_segment=start_ns,
            block_ids=block_ids,
        ))
    return clusters, blocks, frames


def _build_block(bid: str, cluster_id: str, block_el: EBMLElement,
                 group_el: EBMLElement | None, data: bytes,
                 header: blocks.BlockHeader | None, err: str | None,
                 tracks: list[TrackFact], cluster_ticks: int | None,
                 scale_ns: int,
                 warnings: list[Diagnostic]) -> BlockFact:
    group_offset = group_el.offset if group_el is not None else None
    if header is None:
        warnings.append(Diagnostic(
            "BLOCK_HEADER_UNREADABLE",
            "%s: %s" % (bid, err), block_el.offset, block_el.effective_end))
        return BlockFact(
            bid, cluster_id, block_el.offset, block_el.effective_end,
            "SimpleBlock" if group_el is None else "BlockGroup",
            group_offset, -1, 0, 0, 0, False, 0, "none", False,
            [], [], None, True, "BLOCK_HEADER_UNREADABLE",
            block_el.offset, block_el.effective_end, None, None, [],
            None, None, err)
    track = next((t for t in tracks if t.number == header.track_number), None)
    ttcs = track.timecode_scale_raw if track is not None else None
    start_ns, basis = _block_start_ns(cluster_ticks, header.relative_timecode,
                                      scale_ns, ttcs)

    block_duration_raw = None
    block_duration_ns = None
    reference_blocks: list[int] = []
    discard_raw = None
    if group_el is not None:
        bd_el = group_el.child_by_name("BlockDuration")
        if bd_el is not None and isinstance(bd_el.value, int):
            block_duration_raw = bd_el.value
            block_duration_ns = bd_el.value * scale_ns
        for rb in group_el.children_by_name("ReferenceBlock"):
            if isinstance(rb.value, int):
                reference_blocks.append(rb.value)
        dp_el = group_el.child_by_name("DiscardPadding")
        if dp_el is not None and isinstance(dp_el.value, int):
            discard_raw = dp_el.value

    expected_frames = None
    if block_duration_ns is not None and track is not None and \
            track.default_duration_raw:
        ratio = block_duration_ns / track.default_duration_raw
        if ratio.is_integer():
            expected_frames = int(ratio)
    lacing = blocks.parse_lacing(data, header, expected_frames)

    if discard_raw is not None:
        dp_el = group_el.child_by_name("DiscardPadding")
        if block_duration_ns is not None and discard_raw > block_duration_ns:
            warnings.append(Diagnostic(
                "DISCARD_PADDING_EXCEEDS_DURATION",
                "%s: discard padding %dns 超过 Block 时长 %dns"
                % (bid, discard_raw, block_duration_ns),
                dp_el.offset, dp_el.effective_end))
        if discard_raw < 0:
            warnings.append(Diagnostic(
                "DISCARD_PADDING_NEGATIVE",
                "%s: discard padding 为负 %d，编码器延迟保留" % (bid, discard_raw),
                dp_el.offset, dp_el.effective_end))

    if lacing.damaged:
        warnings.append(Diagnostic(
            lacing.damage_code or "LACING_CORRUPT",
            "%s: %s" % (bid, lacing.damage_message),
            lacing.damage_offset, lacing.damage_end))

    selected = 0 if lacing.definitive and lacing.candidates else None
    return BlockFact(
        block_id=bid,
        cluster_id=cluster_id,
        element_offset=block_el.offset,
        byte_end=block_el.effective_end,
        container="SimpleBlock" if group_el is None else "BlockGroup",
        group_offset=group_offset,
        track_number=header.track_number,
        relative_ms_raw=header.relative_timecode,
        relative_ms_unsigned=header.relative_timecode_unsigned,
        flags=header.flags,
        keyframe=header.keyframe,
        start_ns_segment=start_ns,
        basis=basis,
        lacing_mode=lacing.mode,
        lacing_definitive=lacing.definitive,
        frame_candidates=[c.frame_sizes for c in lacing.candidates],
        candidate_counts=[c.count for c in lacing.candidates],
        selected_candidate=selected,
        damaged=lacing.damaged,
        damage_code=lacing.damage_code,
        damage_offset=lacing.damage_offset,
        damage_end=lacing.damage_end,
        block_duration_raw=block_duration_raw,
        block_duration_ns=block_duration_ns,
        reference_blocks=reference_blocks,
        discard_padding_raw=discard_raw,
        discard_padding_ns=discard_raw,
        header_error=None)


def _build_frames(bf: BlockFact, tracks: list[TrackFact]) -> list[FrameFact]:
    if not bf.frame_candidates:
        return []
    idx = bf.selected_candidate if bf.selected_candidate is not None else 0
    bf.selected_candidate = idx
    sizes = bf.frame_candidates[idx]
    track = next((t for t in tracks if t.number == bf.track_number), None)
    dd = track.default_duration_raw if track is not None else None
    times = _frame_times(bf.start_ns_segment, len(sizes),
                         bf.block_duration_ns, dd)
    return [
        FrameFact(bf.block_id, bf.track_number, i, start, dur, bf.basis,
                  provisional=not bf.lacing_definitive)
        for i, (start, dur) in enumerate(times)
    ]


def _extract_cues(segment: EBMLElement, segment_data_offset: int,
                  clusters: list[ClusterFact], blocks: list[BlockFact],
                  tracks: list[TrackFact], scale_ns: int,
                  warnings: list[Diagnostic]) -> list[CueFact]:
    cues_root = segment.child_by_name("Cues")
    if cues_root is None:
        return []
    out: list[CueFact] = []
    cluster_by_file_offset = {c.element_offset: c for c in clusters}
    blocks_by_cluster: dict[str, list[BlockFact]] = {}
    for block in blocks:
        blocks_by_cluster.setdefault(block.cluster_id, []).append(block)

    for cp_el in cues_root.children_by_name("CuePoint"):
        cue_id = "cue@%d" % cp_el.offset
        ct = cp_el.child_by_name("CueTime")
        cue_time_raw = ct.value if ct is not None else None
        ctp = cp_el.child_by_name("CueTrackPositions")
        track_num = None
        pos_raw = None
        rel_pos = None
        duration_raw = None
        if ctp is not None:
            ct_track = ctp.child_by_name("CueTrack")
            track_num = ct_track.value if ct_track is not None else None
            ccp = ctp.child_by_name("CueClusterPosition")
            pos_raw = ccp.value if ccp is not None else None
            crp = ctp.child_by_name("CueRelativePosition")
            rel_pos = crp.value if crp is not None else None
            cd = ctp.child_by_name("CueDuration")
            duration_raw = cd.value if cd is not None else None

        status = "ok"
        message = ""
        resolved_cluster_id = None
        pointed_ns = None
        resolved_block_offset = None
        cue_time_ns = (cue_time_raw * scale_ns
                       if isinstance(cue_time_raw, int) else None)

        if pos_raw is None:
            status = "missing-position"
            message = "CueTrackPositions 缺少 CueClusterPosition，只有时间无法定位"
            warnings.append(Diagnostic(
                "CUE_MISSING_POSITION", "%s: %s" % (cue_id, message),
                cp_el.offset, cp_el.effective_end))
        else:
            target_abs = segment_data_offset + pos_raw
            cluster = cluster_by_file_offset.get(target_abs)
            if cluster is None:
                status = "unresolvable"
                message = ("CueClusterPosition %d (绝对偏移 %d) 不落在任何 "
                           "Cluster 起始处" % (pos_raw, target_abs))
                warnings.append(Diagnostic(
                    "CUE_UNRESOLVABLE", "%s: %s" % (cue_id, message),
                    cp_el.offset, cp_el.effective_end))
            else:
                resolved_cluster_id = cluster.cluster_id
                cue_track = next((t for t in tracks
                                  if t.number == track_num), None)
                ttcs = (cue_track.timecode_scale_raw
                        if cue_track is not None else None)
                target_blocks = [
                    b for b in blocks_by_cluster.get(cluster.cluster_id, [])
                    if track_num is None or b.track_number == track_num]
                target = target_blocks[0] if target_blocks else None
                if target is not None:
                    pointed_ns = target.start_ns_segment
                    resolved_block_offset = target.element_offset
                if isinstance(cue_time_raw, int) and target is not None:
                    rel_ns = cue_time_raw * scale_ns
                    delta = target.start_ns_segment - rel_ns
                    if delta != 0:
                        status = "mispoint"
                        message = ("CueTime %d ticks 与所指 %s 起点相差 %d ns；"
                                   "cue 是索引证据，时间以 cluster/block 为准"
                                   % (cue_time_raw, target.block_id, delta))
                        warnings.append(Diagnostic(
                            "CUE_MISPOINT", "%s: %s" % (cue_id, message),
                            cp_el.offset, cp_el.effective_end))
        out.append(CueFact(
            cue_id=cue_id,
            element_offset=cp_el.offset,
            cue_time_raw=cue_time_raw,
            cue_time_ns_segment=cue_time_ns,
            track_number=track_num,
            cluster_position_raw=pos_raw,
            resolved_cluster_id=resolved_cluster_id,
            resolved_block_offset=resolved_block_offset,
            pointed_start_ns=pointed_ns,
            status=status,
            message=message,
            duration_raw=duration_raw))
    return out


def _detect_gaps(clusters: list[ClusterFact],
                 threshold_ns: int) -> list[GapFact]:
    gaps: list[GapFact] = []
    timed = [c for c in clusters if c.start_ns_segment is not None]
    for earlier, later in zip(timed, timed[1:]):
        span = later.start_ns_segment - earlier.start_ns_segment
        if span < 0:
            gaps.append(GapFact(
                "gap@%d" % later.element_offset,
                earlier.cluster_id, later.cluster_id, "backwards", span,
                earlier.start_ns_segment, later.start_ns_segment))
        elif span > threshold_ns:
            gaps.append(GapFact(
                "gap@%d" % later.element_offset,
                earlier.cluster_id, later.cluster_id, "gap", span,
                earlier.start_ns_segment, later.start_ns_segment))
    return gaps
