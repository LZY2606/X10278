"""派生层：把字节事实解释为带单位的时间结论。

原则：
- 所有领域量携带单位/坐标基准；TimecodeScale 缺失时保留 ticks 原值并附诊断，
  不强行换算。
- relative timecode 已经在 facts 层按有符号 16 位解释；这里处理跨簇溢出：
  若有符号 rel 使帧时间倒退超过半量程，唯一合理的解释是 +65536 包裹，
  这是可证明的单一候选；无法唯一决定时保留全部合法候选。
- cue 只是索引证据：与 cluster 真值不一致时记录分歧，绝不改写真值。
- 多轨不按文件顺序强对齐：每轨独立时间线，跨轨顺序仅作证据并列展示。
- lacing 损坏的块不产生帧时间（不猜边界），但保留块级时间与损坏字节范围。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional
import math

from .facts import SegmentFacts, BlockFact, ClusterFact

TICK_WRAP = 65536
DEFAULT_SCALE_NS = 1_000_000     # 规范缺省值；缺失时仍发诊断
GAP_THRESHOLD_MS = 500           # cluster 间隔超过该值（且无帧覆盖）记缺口


@dataclass
class Diag:
    code: str
    severity: str
    message: str
    subject_key: Optional[str] = None
    byte_start: Optional[int] = None
    byte_end: Optional[int] = None

    def to_dict(self):
        return asdict(self)


@dataclass
class FrameTime:
    block_key: str
    track_number: int
    index: int
    start_ns: Optional[int]            # None=无法唯一确定
    start_ns_candidates: list[int]
    duration_ns: Optional[int]
    duration_source: str
    discard_padding_ns: Optional[int]
    proven: bool
    byte_start: int
    byte_end: int

    def to_dict(self):
        return asdict(self)


@dataclass
class ClusterView:
    cluster_key: str
    byte_start: int
    byte_end: int
    unknown_size: bool
    tc_ticks: int
    tc_ns: Optional[int]
    block_count: int
    proven: bool

    def to_dict(self):
        return asdict(self)


@dataclass
class CueView:
    cue_key: str
    track_number: int
    cue_time_ns: Optional[int]
    raw_cluster_pos: int
    target_abs_offset: Optional[int]   # 按 segment-data 坐标换算的文件偏移
    resolved_cluster_key: Optional[str]
    resolved_cluster_tc_ns: Optional[int]
    mismatch_ns: Optional[int]
    points_to: Optional[str]          # cluster_key 或 None
    status: str                        # exact / mismatch / dangling / unscaled
    byte_start: int

    def to_dict(self):
        return asdict(self)


@dataclass
class TimelineResult:
    scale_ns: Optional[int]
    scale_source: str                  # declared / default
    scale_editable: bool
    duration_ns: Optional[float]
    clusters: list[ClusterView]
    frames: list[FrameTime]
    cues: list[CueView]
    tracks: list[dict]
    gaps: list[dict]
    diagnostics: list[Diag]
    coords: dict

    def to_dict(self):
        return {
            "scale_ns": self.scale_ns, "scale_source": self.scale_source,
            "scale_editable": self.scale_editable,
            "duration_ns": self.duration_ns,
            "clusters": [c.to_dict() for c in self.clusters],
            "frames": [f.to_dict() for f in self.frames],
            "cues": [c.to_dict() for c in self.cues],
            "tracks": self.tracks, "gaps": self.gaps,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "coords": self.coords,
        }


def derive(facts: SegmentFacts, scale_override: Optional[int] = None) -> TimelineResult:
    diags: list[Diag] = []
    if scale_override is not None:
        scale, source = scale_override, "revision"
    elif facts.timecode_scale_raw is not None:
        scale, source = facts.timecode_scale_raw, "declared"
    else:
        scale, source = DEFAULT_SCALE_NS, "default"
        diags.append(Diag("SCALE_MISSING", "warn",
                          "TimecodeScale 缺失，ticks 无法唯一换算为 ns；"
                          "展示采用规范缺省 1,000,000 ns/tick，原始 ticks 保留",
                          byte_start=facts.segment_start))
    if facts.timecode_scale_raw is None and scale_override is None:
        eff_scale: Optional[int] = None      # 真值未知：图上值标 default，不冒充
    else:
        eff_scale = scale

    clusters_by_key = {c.cluster_key: c for c in facts.clusters}
    cluster_views: list[ClusterView] = []
    for c in facts.clusters:
        cluster_views.append(ClusterView(
            cluster_key=c.cluster_key, byte_start=c.element_start,
            byte_end=c.raw_end, unknown_size=c.unknown_size, tc_ticks=c.tc_raw,
            tc_ns=c.tc_raw * eff_scale if eff_scale is not None else None,
            block_count=len(c.blocks), proven=not c.inferred_end))

    default_dur = {t.track_number: t.default_duration_ns for t in facts.tracks}
    frames: list[FrameTime] = []
    for c in facts.clusters:
        _derive_cluster_frames(c, default_dur, eff_scale, frames, diags)

    cues = _derive_cues(facts, clusters_by_key, eff_scale, diags)
    gaps = _derive_gaps(facts, frames, eff_scale, diags)

    tracks = [{
        "track_number": t.track_number, "track_uid": t.track_uid,
        "track_type": t.track_type, "codec_id": t.codec_id,
        "default_duration_ns": t.default_duration_ns,
        "element_start": t.element_start,
    } for t in facts.tracks]

    duration_ns = (facts.duration_ticks * eff_scale
                   if facts.duration_ticks is not None and eff_scale is not None
                   else None)

    return TimelineResult(
        scale_ns=eff_scale, scale_source=source,
        scale_editable=facts.timecode_scale_raw is None or True,
        duration_ns=duration_ns, clusters=cluster_views, frames=frames,
        cues=cues, tracks=tracks, gaps=gaps, diagnostics=diags,
        coords={
            "time": "ns since Segment time origin (t0=0 at first cluster tc)",
            "byte": "absolute file offset",
            "cue_position": "bytes relative to Segment data origin "
                            f"@offset {facts.segment_data_origin}",
        })


def _resolve_rel(cluster_tc: int, rel: int, eff_scale) -> tuple[list[int], bool]:
    """解析相对时码为绝对 ticks 候选。

    返回 (候选 ticks 列表, rel 是否被包裹)。
    - 直接解释 cluster_tc + rel。
    - 若结果明显倒退（<-32768 相对上簇不可能），+65536 包裹通常唯一；
      但当 cluster 时间本身不足以排除两种解释时，两个候选都保留。
    """
    direct = cluster_tc + rel
    wrapped = direct + TICK_WRAP
    # 简单的单调性证据：direct < cluster_tc - 32768 不可能（rel 已是有符号下限），
    # 因此 direct 总在 [tc-32768, tc+32767]。是否接受 wrapped 取决于：
    # wrapped 合法当且仅当 rel 为负且文件流中 cluster 可能跨越时码包裹边界——
    # 我们不掌握 muxer 的连续证据，所以：
    candidates = [direct]
    if rel < 0:
        # 两种解释都在合法有符号语义内，保留全部合法候选（不吞多解）
        candidates.append(wrapped)
    return candidates, rel < 0


def _derive_cluster_frames(c: ClusterFact, default_dur: dict, eff_scale,
                           frames: list[FrameTime], diags: list[Diag]):
    for b in c.blocks:
        cands, wrapped = _resolve_rel(c.tc_raw, b.rel_tc_signed, eff_scale)
        ns_cands = ([v * eff_scale for v in cands]
                    if eff_scale is not None else [])
        start_ns = ns_cands[0] if len(ns_cands) == 1 else None
        if len(ns_cands) > 1:
            diags.append(Diag(
                "REL_TC_AMBIGUOUS", "warn",
                f"block @{b.element_start} 有符号相对时码 {b.rel_tc_signed} "
                f"存在 {len(ns_cands)} 个合法绝对时间候选（含 65536-tick 包裹）",
                subject_key=b.block_key, byte_start=b.element_start,
                byte_end=b.raw_end))
        elif b.rel_tc_raw >= 0x8000:
            diags.append(Diag(
                "REL_TC_SIGNED", "info",
                f"block @{b.element_start} 原始 rel=0x{b.rel_tc_raw:04X} "
                f"按有符号解释为 {b.rel_tc_signed} ticks（非 {b.rel_tc_raw}）",
                subject_key=b.block_key, byte_start=b.element_start,
                byte_end=b.raw_end))

        if b.block_duration_ticks is not None and eff_scale is not None:
            dur = b.block_duration_ticks * eff_scale
            dur_source = "block_duration"
        elif default_dur.get(b.track_number) is not None:
            dur = default_dur[b.track_number]
            dur_source = "track_default_duration"
        else:
            dur = None
            dur_source = "unknown"

        if not b.parse_ok:
            # lacing 损坏：不猜帧边界——没有帧，只有块级时间与损坏范围
            for d in b.lace_diags:
                if d.get("code") == "LACING_CORRUPT":
                    diags.append(Diag(
                        "LACING_CORRUPT", "error",
                        f"block @{b.element_start} lacing 损坏: {d['message']}；"
                        "不猜测帧边界",
                        subject_key=b.block_key,
                        byte_start=b.element_start + d.get("byte_start", 0),
                        byte_end=b.element_start + d.get("byte_end", 0)))
            frames.append(FrameTime(
                block_key=b.block_key, track_number=b.track_number,
                index=-1, start_ns=start_ns, start_ns_candidates=ns_cands,
                duration_ns=None, duration_source="lacing_corrupt",
                discard_padding_ns=b.discard_padding_ns, proven=False,
                byte_start=b.element_start, byte_end=b.raw_end))
            continue

        if not b.frames:
            diags.append(Diag(
                "EMPTY_BLOCK", "warn",
                f"block @{b.element_start} 可解析但无帧范围",
                subject_key=b.block_key, byte_start=b.element_start,
                byte_end=b.raw_end))
        for fs in b.frames:
            frames.append(FrameTime(
                block_key=b.block_key, track_number=b.track_number,
                index=fs.index, start_ns=start_ns,
                start_ns_candidates=ns_cands,
                duration_ns=dur, duration_source=dur_source,
                discard_padding_ns=b.discard_padding_ns, proven=fs.proven,
                byte_start=fs.byte_start, byte_end=fs.byte_end))
        if b.discard_padding_ns is not None:
            diags.append(Diag(
                "DISCARD_PADDING", "info",
                f"block @{b.element_start} DiscardPadding="
                f"{b.discard_padding_ns} ns（解码器应在帧末尾丢弃）",
                subject_key=b.block_key, byte_start=b.element_start,
                byte_end=b.raw_end))


def _derive_cues(facts: SegmentFacts, clusters_by_key, eff_scale,
                 diags: list[Diag]) -> list[CueView]:
    views: list[CueView] = []
    for cue in facts.cues:
        abs_off = facts.segment_data_origin + cue.cluster_pos_raw
        target = None
        for c in facts.clusters:
            if c.element_start == abs_off:
                target = c
                break
        if target is None:
            status = "dangling"
            mismatch = None
            tc_ns = None
            diags.append(Diag(
                "CUE_MISPOINT", "error",
                f"cue @{cue.element_start} 指向文件偏移 {abs_off}，"
                "该位置没有 cluster（cue 仅为索引证据，真值不变）",
                subject_key=cue.cue_key, byte_start=cue.element_start,
                byte_end=cue.element_start + 32))
        else:
            tc_ns = (target.tc_raw * eff_scale
                     if eff_scale is not None else None)
            if eff_scale is None:
                status = "unscaled"
                mismatch = None
            else:
                want = cue.cue_time_ticks * eff_scale
                mismatch = want - tc_ns
                status = "exact" if mismatch == 0 else "mismatch"
                if status == "mismatch":
                    diags.append(Diag(
                        "CUE_MISPOINT", "warn",
                        f"cue @{cue.element_start} 声称 {want} ns，"
                        f"实际 cluster 时码 {tc_ns} ns，偏差 {mismatch} ns；"
                        "以 cluster 时码为准",
                        subject_key=cue.cue_key, byte_start=cue.element_start,
                        byte_end=cue.element_start + 32))
        views.append(CueView(
            cue_key=cue.cue_key, track_number=cue.track_number,
            cue_time_ns=(cue.cue_time_ticks * eff_scale
                         if eff_scale is not None else None),
            raw_cluster_pos=cue.cluster_pos_raw,
            target_abs_offset=abs_off,
            resolved_cluster_key=target.cluster_key if target else None,
            resolved_cluster_tc_ns=tc_ns, mismatch_ns=mismatch,
            points_to=target.cluster_key if target else None,
            status=status, byte_start=cue.element_start))
    return views


def _derive_gaps(facts: SegmentFacts, frames: list[FrameTime], eff_scale,
                 diags: list[Diag]) -> list[dict]:
    # 用"已证明帧"的时间跨度估计每簇实际覆盖终点；lacing 损坏块不参与
    # （不猜帧边界），其覆盖终点按块时码本身计。
    cover_end_ns: dict[str, int] = {}
    for f in frames:
        end = (f.start_ns if f.start_ns is not None else
               (f.start_ns_candidates[0] if f.start_ns_candidates else None))
        if end is None:
            continue
        if f.duration_ns and f.index >= 0:
            end = end + f.duration_ns
        cur = cover_end_ns.get(f.block_key)
        if cur is None or end > cur:
            cover_end_ns[f.block_key] = end
    block_to_cluster = {b.block_key: c.cluster_key
                        for c in facts.clusters for b in c.blocks}
    cluster_end: dict[str, int] = {}
    cluster_tc_ns: dict[str, int] = {}
    for c in facts.clusters:
        if eff_scale is not None:
            cluster_end[c.cluster_key] = c.tc_raw * eff_scale
            cluster_tc_ns[c.cluster_key] = c.tc_raw * eff_scale
    for bk, end_ns in cover_end_ns.items():
        ck = block_to_cluster[bk]
        cluster_end[ck] = max(cluster_end.get(ck, end_ns), end_ns)

    gaps = []
    ordered = sorted(facts.clusters, key=lambda c: c.element_start)
    for prev, nxt in zip(ordered, ordered[1:]):
        if eff_scale is None:
            continue
        delta_ns = (nxt.tc_raw * eff_scale) - cluster_end.get(
            prev.cluster_key, prev.tc_raw * eff_scale)
        byte_gap = nxt.element_start - prev.raw_end
        if delta_ns > GAP_THRESHOLD_MS * 1_000_000:
            gaps.append({
                "after_cluster_key": prev.cluster_key,
                "before_cluster_key": nxt.cluster_key,
                "gap_ns": delta_ns,
                "gap_ticks": delta_ns // eff_scale,
                "byte_start": prev.raw_end, "byte_end": nxt.element_start,
                "byte_gap": byte_gap,
            })
            diags.append(Diag(
                "CLUSTER_GAP", "warn",
                f"cluster 时间缺口 {delta_ns // 1_000_000} ms "
                f"(字节 {prev.raw_end}..{nxt.element_start})",
                byte_start=prev.raw_end, byte_end=nxt.element_start))
    return gaps
