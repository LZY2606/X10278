"""语义引擎：从 EBML 树推导 track 参数、cluster/block 时码、discard padding、
cue 指向、多轨时间线与 cluster 缺口。

纪律：
- relative timecode 按有符号位宽（int16）解释；无符号解释仅保留为未采纳候选。
- lacing 损坏不猜帧边界（沿用 block.unlace 的 proven 前缀）。
- cue 是索引证据而非时间真值：用 cue 指向的字节回查 cluster/block，矛盾记为异常。
- 多轨不按文件顺序强对齐：每帧时间独立由 cluster tick + rel tick + track offset
  计算；track 间文件交错不构成时间对齐依据。
- 所有领域量带单位/坐标基准，导入层原值保留在 raw_fields。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import ids
from .parser import Element, find_all, segment_data_start
from .block import unlace, rel_tc_candidates
from . import units as U

DEFAULT_TIMESCALE_NS = 1_000_000
RULESET = "rules/matroska-timecode/2026-09"
RULE_SEQ = "rule/signed-relative-tc"
RULE_LACE = "rule/no-guess-on-lace-corruption"
RULE_CUE = "rule/cue-is-index-evidence"
RULE_MULTI = "rule/multitrack-no-fileorder-alignment"


@dataclass
class Track:
    uid: str
    number: int
    track_uid: Optional[int]
    track_type: Optional[int]
    name: Optional[str]
    codec: Optional[str]
    default_duration_ns: Optional[int]
    track_timecode_scale: Optional[float]
    track_offset_ticks: Optional[int]
    flag_default: Optional[bool]
    raw_fields: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)


@dataclass
class Frame:
    uid: str
    block_uid: str
    track_number: int
    ordinal: int
    start_ns: Optional[int]
    duration_ns: Optional[int]
    track_offset_ns: int
    rel_tc_tick: int
    rel_tc_raw: int
    discard_padding_ns: Optional[int]
    raw_range: tuple[int, int]
    keyframe: Optional[bool]
    diagnostics: list = field(default_factory=list)


@dataclass
class Block:
    uid: str
    cluster_uid: str
    track_number: int
    simple: bool
    rel_tc_tick: int
    rel_tc_raw: int
    absolute_ns: Optional[int]
    duration_ticks: Optional[int]
    duration_ns: Optional[int]
    discard_padding_ticks: Optional[int]
    discard_padding_ns: Optional[int]
    reference: Optional[int]
    raw_range: tuple[int, int]
    lace: dict
    frames: list[Frame] = field(default_factory=list)
    diagnostics: list = field(default_factory=list)


@dataclass
class Cluster:
    uid: str
    ordinal: int
    timecode_ticks: Optional[int]
    start_ns: Optional[int]
    raw_range: tuple[int, int]
    payload_range: tuple[int, int]
    unknown_size: bool
    prev_size: Optional[int]
    blocks: list[Block] = field(default_factory=list)
    diagnostics: list = field(default_factory=list)


@dataclass
class CueRef:
    uid: str
    cue_time_ticks: Optional[int]
    cue_time_ns: Optional[int]
    track_number: int
    cluster_position_bytes: Optional[int]
    relative_position_bytes: Optional[int]
    cue_duration_ticks: Optional[int]
    block_number: Optional[int]
    claimed_abs_offset: Optional[int]
    resolved_cluster_uid: Optional[str]
    resolved_cluster_start_ns: Optional[int]
    agreement: Optional[bool]
    raw_range: tuple[int, int]
    diagnostics: list = field(default_factory=list)


@dataclass
class Gap:
    uid: str
    kind: str
    track_number: Optional[int]
    after_block_uid: str
    expected_next_ns: int
    actual_next_ns: Optional[int]
    missing_ns: Optional[int]
    byte_gap_range: Optional[tuple[int, int]]
    diagnostics: list = field(default_factory=list)


@dataclass
class Anomaly:
    uid: str
    code: str
    severity: str
    message: str
    object_uid: Optional[str]
    byte_range: Optional[tuple[int, int]]
    evidence: dict = field(default_factory=dict)


def _child(el: Element, eid: int) -> Optional[Element]:
    for c in el.children:
        if c.id == eid:
            return c
    return None


def _children(el: Element, eid: int):
    return [c for c in el.children if c.id == eid]


def stable_id(prefix: str, *parts) -> str:
    import hashlib
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]
    return f"{prefix}_{h}"


def derive(parsed: dict, blob_sha: str, rule_overrides: Optional[dict] = None,
           source_edits: Optional[dict] = None) -> dict:
    """运行全部语义推导，返回版本化派生结果。

    rule_overrides: 参数修订，如 {"default_duration": {"<trackNumber>": ns}}，
                    {"timecode_scale_ns": n}，{"track_offset_ns": {"n": ns}}
    source_edits: 源字节改动说明 {"ranges": [(start,end,reason)], "note"}
    """
    rule_overrides = rule_overrides or {}
    source_edits = source_edits or {}
    segment: Optional[Element] = parsed["segment"]
    anomalies: list[Anomaly] = []
    deps: dict[str, list[str]] = {}

    info = _find_segment_child(segment, ids.Info)
    ts_el = _child(info, ids.TimecodeScale) if info else None
    raw_ts = ts_el.value if ts_el else None
    timecode_scale_ns = rule_overrides.get("timecode_scale_ns",
                                           raw_ts or DEFAULT_TIMESCALE_NS)
    ts_quantity = U.Quantity(
        raw=raw_ts if ts_el else None, basis=U.Basis(
            "ns/tick", origin="segment", note="TimecodeScale 原始值"),
        value=timecode_scale_ns, converted=True)
    if ts_el is None:
        ts_quantity.diagnostics.append(
            "DEFAULT_TIMESCALE:缺少 TimecodeScale，采用 Matroska 默认 1,000,000 ns")

    tracks = _derive_tracks(segment, rule_overrides, blob_sha, deps)
    track_by_num = {t.number: t for t in tracks}
    clusters = _derive_clusters(segment, timecode_scale_ns, track_by_num,
                                rule_overrides, blob_sha, deps, anomalies)
    cues = _derive_cues(segment, timecode_scale_ns, clusters, blob_sha,
                        deps, anomalies)
    gaps, tl_gap_anoms = _derive_gaps(clusters, track_by_num, timecode_scale_ns)
    anomalies.extend(tl_gap_anoms)

    timeline = _derive_timeline(clusters, track_by_num)

    # 未知长度与溢出的汇总异常在子推导中已添加；这里补全局相对时码范围检查
    for cl in clusters:
        for blk in cl.blocks:
            if abs(blk.rel_tc_tick) >= 0x7000:
                anomalies.append(Anomaly(
                    uid=stable_id("anom", "relrange", blk.uid),
                    code="REL_TC_NEAR_OVERFLOW", severity="warning",
                    message=("relative timecode 接近 int16 边界 "
                             f"{blk.rel_tc_tick}，已按有符号位宽解释并保留无符号候选"),
                    object_uid=blk.uid, byte_range=blk.raw_range,
                    evidence={"signed": blk.rel_tc_tick,
                              "unsigned": blk.rel_tc_raw,
                              "rule": RULE_SEQ}))
            for d in blk.lace.get("diagnostics", []):
                anomalies.append(Anomaly(
                    uid=stable_id("anom", d, blk.uid),
                    code="LACE_CORRUPTION" if "LACE" in d else "BLOCK_DIAG",
                    severity="error", message=d, object_uid=blk.uid,
                    byte_range=(blk.lace.get("corrupt_range") or blk.raw_range),
                    evidence={"rule": RULE_LACE}))

    _mark_dependencies(deps, segment, timecode_scale_ns, clusters, cues, tracks)

    return {
        "ruleset": RULESET,
        "rule_overrides": rule_overrides,
        "source_edits": source_edits,
        "timecode_scale": ts_quantity.to_dict(),
        "tracks": [_track_dict(t) for t in tracks],
        "clusters": [_cluster_dict(c) for c in clusters],
        "cues": [_cue_dict(c) for c in cues],
        "gaps": [g.__dict__ for g in gaps],
        "timeline": timeline,
        "anomalies": [a.__dict__ for a in anomalies],
        "dependencies": deps,
    }


def _find_segment_child(segment, eid):
    if segment is None:
        return None
    return _child(segment, eid)


def _derive_tracks(segment, overrides, blob_sha, deps) -> list[Track]:
    out = []
    tracks_el = _find_segment_child(segment, ids.Tracks)
    if not tracks_el:
        return out
    for i, te in enumerate(_children(tracks_el, ids.TrackEntry)):
        num_el = _child(te, ids.TrackNumber)
        num = num_el.value if num_el else i + 1
        uid_el = _child(te, ids.TrackUID)
        dd_el = _child(te, ids.DefaultDuration)
        tts_el = _child(te, ids.TrackTimecodeScale)
        off_el = _child(te, ids.TrackOffset)
        dd_override = overrides.get("default_duration", {}).get(str(num))
        off_override = overrides.get("track_offset_ns", {}).get(str(num))
        t = Track(
            uid=stable_id("trk", blob_sha, te.id_offset),
            number=num,
            track_uid=uid_el.value if uid_el else None,
            track_type=_child(te, ids.TrackType).value if _child(te, ids.TrackType) else None,
            name=_child(te, ids.Name).value if _child(te, ids.Name) else None,
            codec=_child(te, ids.CodecID).value if _child(te, ids.CodecID) else None,
            default_duration_ns=dd_override if dd_override is not None else dd_el.value if dd_el else None,
            track_timecode_scale=tts_el.value if tts_el else None,
            track_offset_ticks=off_el.value if off_el else 0,
            flag_default=bool(_child(te, ids.FlagDefault).value) if _child(te, ids.FlagDefault) else None,
            raw_fields={
                "track_number": _raw(num_el),
                "track_uid": _raw(uid_el),
                "track_type": _raw(_child(te, ids.TrackType)),
                "default_duration": _raw(dd_el),
                "default_duration_unit": "ns" if dd_el else None,
                "track_timecode_scale": _raw(tts_el),
                "track_offset": _raw(off_el),
                "track_offset_unit": "tick(scale)" if off_el else None,
                "codec_id": _raw(_child(te, ids.CodecID)),
            },
            source={"element_offset": te.id_offset,
                    "raw_range": list(te.raw_range)})
        if off_override is not None:
            t.raw_fields["track_offset_override_ns"] = off_override
        deps[t.uid] = [f"offset:{te.id_offset}", "rule:track-params"]
        out.append(t)
    return out


def _raw(el: Optional[Element]):
    if el is None:
        return None
    return {"value": el.value, "offset": el.id_offset,
            "raw_range": list(el.raw_range),
            "hex": el.payload.hex() if el.payload else ""}


def _track_dict(t: Track):
    return {"uid": t.uid, "number": t.number, "track_uid": t.track_uid,
            "track_type": t.track_type, "name": t.name, "codec": t.codec,
            "default_duration_ns": t.default_duration_ns,
            "track_timecode_scale": t.track_timecode_scale,
            "track_offset_ticks": t.track_offset_ticks,
            "flag_default": t.flag_default, "raw_fields": t.raw_fields,
            "source": t.source}


def _block_groups(cluster_el):
    for b in cluster_el.children:
        if b.id == ids.SimpleBlock:
            yield b, None
        elif b.id == ids.BlockGroup:
            blk = _child(b, ids.Block)
            if blk is not None:
                yield blk, b


def _derive_clusters(segment, ts_ns, track_by_num, overrides, blob_sha,
                     deps, anomalies) -> list[Cluster]:
    out: list[Cluster] = []
    if segment is None:
        return out
    for ordinal, ce in enumerate(_children(segment, ids.Cluster)):
        tc_el = _child(ce, ids.Timecode)
        tc_raw = tc_el.value if tc_el else None
        start_ns = U.ticks_to_ns(tc_raw, ts_ns) if tc_raw is not None else None
        cu = stable_id("clu", blob_sha, ce.id_offset)
        cl = Cluster(
            uid=cu, ordinal=ordinal, timecode_ticks=tc_raw, start_ns=start_ns,
            raw_range=ce.raw_range, payload_range=(ce.payload_offset,
                                                    ce.end_offset or ce.payload_offset),
            unknown_size=ce.unknown_size,
            prev_size=_child(ce, ids.PrevSize).value if _child(ce, ids.PrevSize) else None)
        if ce.unknown_size:
            cl.diagnostics.append(
                "UNKNOWN_LENGTH:Cluster 使用未知长度，边界由兄弟元素扫描确定")
        if tc_el is None:
            cl.diagnostics.append("MISSING_CLUSTER_TIMECODE:缺少 Cluster Timecode")
        block_ord = 0
        for blk_el, bg_el in _block_groups(ce):
            blk = _derive_block(blk_el, bg_el, cu, cl, block_ord, ts_ns,
                                track_by_num, overrides, blob_sha, deps,
                                anomalies)
            cl.blocks.append(blk)
            block_ord += 1
        deps[cu] = [f"offset:{ce.id_offset}", "rule:cluster-timecode",
                    f"timescale:{ts_ns}"]
        out.append(cl)
    return out


def _derive_block(blk_el, bg_el, cluster_uid, cl: Cluster, ordinal, ts_ns,
                  track_by_num, overrides, blob_sha, deps, anomalies) -> Block:
    simple = blk_el.id == ids.SimpleBlock
    payload = blk_el.payload
    hdr, lace = unlace(payload, simple=simple)
    bu = stable_id("blk", blob_sha, blk_el.id_offset)

    dur_el = _child(bg_el, ids.BlockDuration) if bg_el else None
    ref_el = _child(bg_el, ids.ReferenceBlock) if bg_el else None
    disc_el = _child(bg_el, ids.DiscardPadding) if bg_el else None
    dur_ticks = dur_el.value if dur_el else None
    disc_ticks = disc_el.value if disc_el else None

    # 有符号位宽解释；保留无符号候选
    tc_cands = rel_tc_candidates(hdr.rel_tc_raw)
    rel_tick = hdr.rel_tc_signed
    if cl.timecode_ticks is not None:
        abs_tick = cl.timecode_ticks + rel_tick
        abs_ns = U.ticks_to_ns(abs_tick, ts_ns)
    else:
        abs_ns = None

    track = track_by_num.get(hdr.track_num)
    track_offset_ns = 0
    if track is not None:
        off_ns_override = overrides.get("track_offset_ns", {}).get(str(track.number))
        if off_ns_override is not None:
            track_offset_ns = int(off_ns_override)
        else:
            track_offset_ns = U.ticks_to_ns(track.track_offset_ticks or 0, ts_ns)

    dur_ns = U.ticks_to_ns(dur_ticks, ts_ns) if dur_ticks is not None else None
    if dur_ns is None and track is not None and track.default_duration_ns:
        dur_ns = track.default_duration_ns
    disc_ns = U.ticks_to_ns(disc_ticks, ts_ns) if disc_ticks is not None else None
    if disc_ns is not None and disc_ns < 0:
        anomalies.append(Anomaly(
            uid=stable_id("anom", "discard", bu),
            code="NEGATIVE_DISCARD_PADDING", severity="warning",
            message=f"DiscardPadding 为负 {disc_ns}ns，按规范保留并标记",
            object_uid=bu, byte_range=(disc_el.id_offset, disc_el.end_offset),
            evidence={"raw_ticks": disc_ticks, "unit": "ns"}))

    blk = Block(
        uid=bu, cluster_uid=cluster_uid, track_number=hdr.track_num,
        simple=simple, rel_tc_tick=rel_tick, rel_tc_raw=hdr.rel_tc_raw,
        absolute_ns=(abs_ns + track_offset_ns) if abs_ns is not None else None,
        duration_ticks=dur_ticks, duration_ns=dur_ns,
        discard_padding_ticks=disc_ticks, discard_padding_ns=disc_ns,
        reference=ref_el.value if ref_el else None,
        raw_range=blk_el.raw_range,
        lace=lace.to_dict())
    blk.lace["tc_candidates"] = tc_cands
    if hdr.lacing != 0 and lace.frames is None:
        blk.diagnostics.append(
            "FRAME_BOUNDARIES_UNKNOWN:lacing 损坏，帧时间线仅使用块起点，不猜帧边界")
    # 每帧（损坏时无帧，绝不编造）
    ranges = lace.frame_ranges if lace.frames is not None else []
    for fi, (a, b) in enumerate(ranges):
        f_start = abs_ns + track_offset_ns if abs_ns is not None else None
        if dur_ns is not None and lace.frames is not None and len(lace.frames) > 1:
            per = dur_ns // len(lace.frames)
            f_dur = per
        else:
            f_dur = dur_ns
        abs_ra = (blk_el.payload_offset + a, blk_el.payload_offset + b)
        blk.frames.append(Frame(
            uid=stable_id("frm", bu, fi), block_uid=bu,
            track_number=hdr.track_num, ordinal=ordinal,
            start_ns=f_start, duration_ns=f_dur,
            track_offset_ns=track_offset_ns, rel_tc_tick=rel_tick,
            rel_tc_raw=hdr.rel_tc_raw, discard_padding_ns=disc_ns,
            raw_range=abs_ra, keyframe=hdr.keyframe))
    deps[bu] = [f"offset:{blk_el.id_offset}", cluster_uid,
                f"timescale:{ts_ns}", RULE_SEQ,
                f"track:{track.uid}" if track else "track:missing"]
    return blk


def _cluster_dict(c: Cluster):
    return {"uid": c.uid, "ordinal": c.ordinal,
            "timecode_ticks": c.timecode_ticks, "start_ns": c.start_ns,
            "raw_range": list(c.raw_range),
            "payload_range": list(c.payload_range),
            "unknown_size": c.unknown_size, "prev_size": c.prev_size,
            "diagnostics": c.diagnostics,
            "blocks": [_block_dict(b) for b in c.blocks]}


def _block_dict(b: Block):
    return {"uid": b.uid, "cluster_uid": b.cluster_uid,
            "track_number": b.track_number, "simple": b.simple,
            "rel_tc_tick": b.rel_tc_tick, "rel_tc_raw": b.rel_tc_raw,
            "absolute_ns": b.absolute_ns, "duration_ticks": b.duration_ticks,
            "duration_ns": b.duration_ns,
            "discard_padding_ticks": b.discard_padding_ticks,
            "discard_padding_ns": b.discard_padding_ns,
            "reference": b.reference, "raw_range": list(b.raw_range),
            "diagnostics": b.diagnostics, "lace": b.lace,
            "frames": [f.__dict__ | {"raw_range": list(f.raw_range)}
                       for f in b.frames]}


def _derive_cues(segment, ts_ns, clusters: list[Cluster], blob_sha,
                 deps, anomalies) -> list[CueRef]:
    out: list[CueRef] = []
    cues_el = _find_segment_child(segment, ids.Cues)
    if not cues_el or segment is None:
        return out
    seg_data = segment_data_start(segment)
    # 用 cluster 的 segment-relative 偏移做回查（cue 的坐标基准）
    cl_by_offset = {}
    for c_el in _children(segment, ids.Cluster):
        cl_by_offset[c_el.id_offset - seg_data] = c_el
    for cp in _children(cues_el, ids.CuePoint):
        ct_el = _child(cp, ids.CueTime)
        for ctp in _children(cp, ids.CueTrackPositions):
            trk_el = _child(ctp, ids.CueTrack)
            ccp_el = _child(ctp, ids.CueClusterPosition)
            crp_el = _child(ctp, ids.CueRelativePosition)
            cd_el = _child(ctp, ids.CueDuration)
            cbn_el = _child(ctp, ids.CueBlockNumber)
            ccp = ccp_el.value if ccp_el else None
            claimed_abs = seg_data + ccp if ccp is not None else None
            resolved_cl = None
            resolved_start = None
            agreement = None
            cl_el = cl_by_offset.get(ccp) if ccp is not None else None
            if cl_el is not None:
                cl_obj = next((c for c in clusters
                               if c.raw_range[0] == cl_el.id_offset), None)
                resolved_cl = cl_obj.uid if cl_obj else None
                resolved_start = cl_obj.start_ns if cl_obj else None
                cue_time_ns = U.ticks_to_ns(ct_el.value, ts_ns) if ct_el else None
                if cue_time_ns is not None and resolved_start is not None:
                    # 索引证据 vs 实际 cluster 起点（cluster 起点应 <= cue 时间）
                    agreement = resolved_start <= cue_time_ns <= resolved_start + ts_ns * 32767
                    if not agreement:
                        anomalies.append(Anomaly(
                            uid=stable_id("anom", "cuemis", cp.id_offset,
                                          trk_el.value if trk_el else 0),
                            code="CUE_MISPOINT", severity="error",
                            message=("Cue 索引时间与所指 Cluster 时码矛盾："
                                     f"cue={cue_time_ns}ns cluster={resolved_start}ns；"
                                     "以 block 时码为准，cue 仅作索引证据"),
                            object_uid=resolved_cl,
                            byte_range=(cp.id_offset, cp.end_offset),
                            evidence={"cue_time_ns": cue_time_ns,
                                      "cluster_start_ns": resolved_start,
                                      "rule": RULE_CUE}))
            elif ccp is not None:
                anomalies.append(Anomaly(
                    uid=stable_id("anom", "cuedangle", cp.id_offset, ccp),
                    code="CUE_DANGLING", severity="error",
                    message=f"CueClusterPosition={ccp} 回查不到任何 Cluster",
                    object_uid=None, byte_range=(cp.id_offset, cp.end_offset),
                    evidence={"claimed_abs_offset": claimed_abs,
                              "rule": RULE_CUE}))
            cue = CueRef(
                uid=stable_id("cue", blob_sha, cp.id_offset,
                              trk_el.value if trk_el else 0),
                cue_time_ticks=ct_el.value if ct_el else None,
                cue_time_ns=U.ticks_to_ns(ct_el.value, ts_ns) if ct_el else None,
                track_number=trk_el.value if trk_el else None,
                cluster_position_bytes=ccp,
                relative_position_bytes=crp_el.value if crp_el else None,
                cue_duration_ticks=cd_el.value if cd_el else None,
                block_number=cbn_el.value if cbn_el else None,
                claimed_abs_offset=claimed_abs,
                resolved_cluster_uid=resolved_cl,
                resolved_cluster_start_ns=resolved_start,
                agreement=agreement, raw_range=cp.raw_range)
            out.append(cue)
            deps[cue.uid] = [f"offset:{cp.id_offset}", "rule:cue-evidence",
                             f"timescale:{ts_ns}"]
    return out


def _cue_dict(c: CueRef):
    d = c.__dict__.copy()
    d["raw_range"] = list(c.raw_range)
    return d


def _derive_gaps(clusters: list[Cluster], track_by_num, ts_ns):
    """两个层面：track 时间线缺口（时长不连续）与字节层面的 cluster 缺口。

    多轨不按文件顺序强对齐：每条 track 独立用绝对时间排序检测。
    """
    gaps: list[Gap] = []
    anomalies: list[Anomaly] = []
    # 1) 字节层面：相邻 Cluster 不连续（中间被挖掉/Void）
    for a, b in zip(clusters, clusters[1:]):
        if a.raw_range[1] != b.raw_range[0]:
            gap_range = (a.raw_range[1], b.raw_range[0])
            gaps.append(Gap(
                uid=stable_id("gap", "byte", a.uid, b.uid), kind="byte_cluster_gap",
                track_number=None, after_block_uid=a.uid,
                expected_next_ns=b.start_ns or 0, actual_next_ns=b.start_ns,
                missing_ns=None, byte_gap_range=gap_range,
                diagnostics=["MIDSTREAM_CLUSTER_GAP:相邻 Cluster 字节不连续，"
                             "时间链可能不完整，不假设被删内容"]))
            anomalies.append(Anomaly(
                uid=stable_id("anom", "bytegap", a.uid, b.uid),
                code="MIDSTREAM_CLUSTER_GAP", severity="error",
                message=f"Cluster 之间存在字节缺口 {gap_range[0]}..{gap_range[1]}",
                object_uid=b.uid, byte_range=gap_range,
                evidence={"prev": a.uid, "next": b.uid}))
    # 2) track 时间线：按绝对时间排序，相邻帧时间差 != 预期帧长
    by_track: dict[int, list[Block]] = {}
    for c in clusters:
        for blk in c.blocks:
            by_track.setdefault(blk.track_number, []).append(blk)
    for num, blks in by_track.items():
        ordered = sorted(blks, key=lambda x: (x.absolute_ns is None,
                                              x.absolute_ns or 0))
        for prev, nxt in zip(ordered, ordered[1:]):
            if prev.absolute_ns is None or nxt.absolute_ns is None:
                continue
            track = track_by_num.get(num)
            expected_dur = prev.duration_ns or (
                track.default_duration_ns if track else None)
            if expected_dur is None:
                continue
            delta = nxt.absolute_ns - prev.absolute_ns
            if delta > expected_dur + max(1, expected_dur // 1000):
                gaps.append(Gap(
                    uid=stable_id("gap", "time", num, prev.uid, nxt.uid),
                    kind="track_time_gap", track_number=num,
                    after_block_uid=prev.uid,
                    expected_next_ns=prev.absolute_ns + expected_dur,
                    actual_next_ns=nxt.absolute_ns,
                    missing_ns=delta - expected_dur, byte_gap_range=None,
                    diagnostics=["TRACK_TIME_GAP:该轨道时间线出现空隙，"
                                 "不依据其它轨道填补"]))
    return gaps, anomalies


def _derive_timeline(clusters: list[Cluster], track_by_num) -> dict:
    """每轨独立时间线（numpy 做排序/量纲计算），不按文件顺序强对齐。"""
    import numpy as np
    tracks_series: dict[int, list[tuple]] = {}
    for c in clusters:
        for blk in c.blocks:
            if blk.absolute_ns is None:
                continue
            tracks_series.setdefault(blk.track_number, []).append(
                (blk.absolute_ns, blk.duration_ns, blk.uid,
                 c.ordinal, blk.raw_range[0]))
    series = {}
    for num, rows in tracks_series.items():
        arr = np.array([(r[0], r[1] or 0, r[4]) for r in rows],
                       dtype=np.int64)
        order = np.argsort(arr[:, 0], kind="stable")
        arr = arr[order]
        series[str(num)] = {
            "track_number": num,
            "track_name": track_by_num[num].name if num in track_by_num else None,
            "unit": "ns",
            "points": [{"start_ns": int(arr[i, 0]),
                        "duration_ns": int(arr[i, 1]),
                        "block_uid": rows[int(order[i])][2],
                        "cluster_ordinal": rows[int(order[i])][3],
                        "file_offset": int(arr[i, 2])}
                       for i in range(arr.shape[0])],
            "note": "按绝对 ns 排序；文件顺序只存于 file_offset，不作时间对齐依据"}
    return {"tracks": series}


def _mark_dependencies(deps, segment, ts_ns, clusters, cues, tracks):
    deps["timescale"] = [f"offset:{_find_segment_child(segment, ids.TimecodeScale).id_offset}"
                         if _find_segment_child(segment, ids.TimecodeScale) else "default"]
    deps["timeline"] = [b.uid for c in clusters for b in c.blocks] + [
        f"timescale:{ts_ns}"]
    deps.setdefault("cues", [c.uid for c in cues])
