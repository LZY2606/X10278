"""Semantic layer: turns the raw EBML tree into tracks, clusters, blocks,
frames and cues while keeping every raw value (and its unit / coordinate
basis) intact.  Normalisation happens only in the derived fields; values
that cannot be converted stay in place with a diagnostic.

Invariants enforced here:
  * Block relative timecodes are signed 16-bit big-endian.
  * When lacing data is corrupt, frame boundaries are NOT guessed.
  * Cues are index evidence, never time truth.
  * Tracks are never force-aligned by file order.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from . import ebml


@dataclass
class Diagnostic:
    code: str            # machine-readable, e.g. "LACING_CORRUPT"
    message: str
    subject: str         # stable id of the affected object
    raw_range: tuple[int, int] | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Frame:
    id: str
    index: int
    size: int | None     # None => boundary unknown (corrupt lacing)


@dataclass
class Block:
    id: str
    element_offset: int
    raw_range: tuple[int, int]
    track_number: int
    rel_timecode: int            # signed int16, as stored
    rel_timecode_unit: str       # "cluster-tick"
    keyframe: bool
    lacing: str                  # none|xiph|fixed|ebml
    lacing_corrupt: bool
    frames: list[Frame]          # empty when boundaries unknown
    discard_padding_ns: int | None = None   # raw value, unit ns
    block_duration_ticks: int | None = None
    in_block_group: bool = False


@dataclass
class Cluster:
    id: str
    index: int
    element_offset: int
    raw_range: tuple[int, int]
    timecode: int | None         # ticks; None when Timecode element absent
    unknown_size: bool
    blocks: list[Block]


@dataclass
class Track:
    id: str
    number: int
    uid: int
    codec: str
    track_type: int
    default_duration_ns: int | None
    timecode_scale: float        # TrackTimecodeScale, default 1.0


@dataclass
class CuePoint:
    id: str
    time_ticks: int
    track: int
    cluster_position: int        # bytes, relative to segment payload start
    block_number: int | None


@dataclass
class SegmentModel:
    timecode_scale_ns: int       # raw TimecodeScale, unit ns
    tracks: list[Track]
    clusters: list[Cluster]
    cues: list[CuePoint]
    segment_data_offset: int
    diagnostics: list[Diagnostic]


def parse_block_payload(payload: bytes, block_id: str, element_offset: int,
                        raw_range, in_group: bool) -> tuple[Block, list[Diagnostic]]:
    diags: list[Diagnostic] = []
    track, tlen, _ = ebml.read_vint(payload, 0)
    rel = int.from_bytes(payload[tlen:tlen + 2], "big", signed=True)
    flags = payload[tlen + 2]
    keyframe = bool(flags & 0x80)
    lace_mode = (flags & 0x06) >> 1
    lace_name = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}[lace_mode]
    body = payload[tlen + 3:]
    sizes: list[int] = []
    corrupt = False
    if lace_mode == 0:
        sizes = [len(body)]
    elif lace_mode == 2:  # fixed-size lacing
        if len(body) < 1:
            corrupt = True
        else:
            n = body[0] + 1
            rest = len(body) - 1
            if n == 0 or rest % n != 0:
                corrupt = True
            else:
                sizes = [rest // n] * n
    elif lace_mode == 1:  # xiph
        if len(body) < 1:
            corrupt = True
        else:
            n = body[0] + 1
            i = 1
            ok = True
            for _ in range(n - 1):
                s = 0
                while True:
                    if i >= len(body):
                        ok = False
                        break
                    b = body[i]
                    i += 1
                    s += b
                    if b < 255:
                        break
                if not ok:
                    break
                sizes.append(s)
            if not ok or sum(sizes) > len(body) - i:
                corrupt = True
                sizes = []
            else:
                sizes.append(len(body) - i - sum(sizes))
    elif lace_mode == 3:  # ebml lacing
        if len(body) < 2:
            corrupt = True
        else:
            n = body[0] + 1
            try:
                first, used, _ = ebml.read_vint(body, 1)
                sizes = [first]
                i = 1 + used
                prev = first
                for _ in range(n - 2):
                    raw, used, _ = ebml.read_vint(body, i)
                    bias = (1 << (7 * used - 1)) - 1
                    diff = raw - bias
                    sizes.append(prev + diff)
                    prev += diff
                    i += used
                consumed = i + sum(sizes)
                if consumed > len(body) or any(s < 0 for s in sizes):
                    corrupt = True
                    sizes = []
                else:
                    sizes.append(len(body) - consumed)
            except ebml.ParseError:
                corrupt = True
                sizes = []
    if corrupt:
        diags.append(Diagnostic(
            code="LACING_CORRUPT",
            message="lacing sizes inconsistent with payload; frame boundaries not guessed",
            subject=block_id, raw_range=tuple(raw_range),
            detail={"lacing": lace_name, "payload_size": len(payload)}))
    frames = [Frame(id=f"{block_id}/f{k}", index=k, size=s)
              for k, s in enumerate(sizes)] if not corrupt else []
    blk = Block(id=block_id, element_offset=element_offset, raw_range=tuple(raw_range),
                track_number=track, rel_timecode=rel,
                rel_timecode_unit="cluster-tick", keyframe=keyframe,
                lacing=lace_name, lacing_corrupt=corrupt, frames=frames,
                in_block_group=in_group)
    return blk, diags


def build_segment_model(data: bytes, roots: list[ebml.Element]) -> SegmentModel:
    diags: list[Diagnostic] = []
    seg = next((e for e in roots if e.eid == 0x18538067), None)
    if seg is None:
        raise ebml.ParseError("no Segment element")
    seg_end = seg.raw_range(len(data))[1]

    scale = 1_000_000
    info = ebml.first_child(seg, 0x1549A966)
    if info is not None:
        ts = ebml.first_child(info, 0x2AD7B1)
        if ts is not None:
            scale = ebml.uint_value(data, ts)

    tracks: list[Track] = []
    tracks_el = ebml.first_child(seg, 0x1654AE6B)
    if tracks_el is not None:
        for te in tracks_el.children:
            if te.eid != 0xAE:
                continue
            def u(eid, default=None):
                c = ebml.first_child(te, eid)
                return ebml.uint_value(data, c) if c is not None else default
            tcs_el = ebml.first_child(te, 0x23314F)
            tcs = ebml.float_value(data, tcs_el) if tcs_el is not None else 1.0
            codec_el = ebml.first_child(te, 0x86)
            num = u(0xD7, 0)
            tracks.append(Track(
                id=f"trk:{num}", number=num, uid=u(0x73C5, 0),
                codec=ebml.str_value(data, codec_el) if codec_el else "",
                track_type=u(0x83, 0), default_duration_ns=u(0x23E383),
                timecode_scale=tcs))
            if tcs != 1.0:
                diags.append(Diagnostic(
                    code="TRACK_TIMEBASE_DIFF",
                    message=f"track {num} has TrackTimecodeScale {tcs}; "
                            "not force-aligned with other tracks",
                    subject=f"trk:{num}",
                    raw_range=tcs_el.raw_range(seg_end) if tcs_el else None,
                    detail={"track_timecode_scale": tcs}))

    clusters: list[Cluster] = []
    blocks_by_cluster: list[Block] = []
    cidx = 0
    for child in seg.children:
        if child.eid != 0x1F43B675:
            continue
        cid = f"clu:{cidx}"
        tc_el = ebml.first_child(child, 0xE7)
        tc = ebml.uint_value(data, tc_el) if tc_el is not None else None
        if tc is None:
            diags.append(Diagnostic(
                code="CLUSTER_TIMECODE_MISSING",
                message="cluster has no Timecode element; absolute position undecidable",
                subject=cid, raw_range=child.raw_range(seg_end)))
        if child.unknown_size:
            diags.append(Diagnostic(
                code="UNKNOWN_SIZE_ELEMENT",
                message="cluster uses unknown-size vint; extent inferred from valid children",
                subject=cid, raw_range=child.raw_range(seg_end)))
        blocks: list[Block] = []
        bidx = 0
        for item in child.children:
            if item.eid == 0xA3:
                payload = data[item.data_offset:item.raw_range(seg_end)[1]]
                blk, bd = parse_block_payload(payload, f"{cid}/b{bidx}",
                                              item.offset, item.raw_range(seg_end), False)
                blocks.append(blk); diags.extend(bd); bidx += 1
            elif item.eid == 0xA0:
                blk_el = ebml.first_child(item, 0xA1)
                if blk_el is None:
                    continue
                payload = data[blk_el.data_offset:blk_el.raw_range(seg_end)[1]]
                blk, bd = parse_block_payload(payload, f"{cid}/b{bidx}",
                                              blk_el.offset, blk_el.raw_range(seg_end), True)
                dp_el = ebml.first_child(item, 0x75A2)
                if dp_el is not None:
                    blk.discard_padding_ns = ebml.int_value(data, dp_el)
                    diags.append(Diagnostic(
                        code="DISCARD_PADDING",
                        message=f"BlockGroup carries DiscardPadding {blk.discard_padding_ns} ns",
                        subject=blk.id, raw_range=dp_el.raw_range(seg_end),
                        detail={"discard_padding_ns": blk.discard_padding_ns, "unit": "ns"}))
                dur_el = ebml.first_child(item, 0x9B)
                if dur_el is not None:
                    blk.block_duration_ticks = ebml.uint_value(data, dur_el)
                blocks.append(blk); diags.extend(bd); bidx += 1
        clusters.append(Cluster(id=cid, index=cidx, element_offset=child.offset,
                                raw_range=child.raw_range(seg_end), timecode=tc,
                                unknown_size=child.unknown_size, blocks=blocks))
        cidx += 1

    # cluster gap detection (needs cluster timecodes + block extents)
    for prev, cur in zip(clusters, clusters[1:]):
        if prev.timecode is None or cur.timecode is None:
            continue
        prev_end = prev.timecode
        for b in prev.blocks:
            prev_end = max(prev_end, prev.timecode + b.rel_timecode)
        if cur.timecode - prev_end > 1:
            diags.append(Diagnostic(
                code="CLUSTER_GAP",
                message=f"no cluster covers ticks ({prev_end}, {cur.timecode})",
                subject=cur.id, raw_range=cur.raw_range,
                detail={"gap_start_tick": prev_end, "gap_end_tick": cur.timecode}))

    cues: list[CuePoint] = []
    cues_el = ebml.first_child(seg, 0x1C53BB6B)
    if cues_el is not None:
        for i, cp in enumerate(cues_el.children):
            if cp.eid != 0xBB:
                continue
            t_el = ebml.first_child(cp, 0xB3)
            tp = ebml.first_child(cp, 0xB7)
            cues.append(CuePoint(
                id=f"cue:{i}",
                time_ticks=ebml.uint_value(data, t_el) if t_el else 0,
                track=ebml.uint_value(data, ebml.first_child(tp, 0xF7)) if tp and ebml.first_child(tp, 0xF7) else 0,
                cluster_position=ebml.uint_value(data, ebml.first_child(tp, 0xF1)) if tp and ebml.first_child(tp, 0xF1) else 0,
                block_number=(ebml.uint_value(data, ebml.first_child(tp, 0x5378))
                              if tp and ebml.first_child(tp, 0x5378) else None)))

    # cue verification: evidence only, never overrides block timecodes
    cluster_offsets = {c.element_offset - seg.data_offset: c for c in clusters}
    for cue in cues:
        target = cluster_offsets.get(cue.cluster_position)
        if target is None:
            diags.append(Diagnostic(
                code="CUE_MISPOINT",
                message=f"cue {cue.id} points at payload+{cue.cluster_position} "
                        "which is not a cluster start; cue treated as evidence only",
                subject=cue.id,
                detail={"cue_time_ticks": cue.time_ticks,
                        "claimed_position": cue.cluster_position}))
        elif target.timecode is not None and target.timecode != cue.time_ticks:
            diags.append(Diagnostic(
                code="CUE_TIME_MISMATCH",
                message=f"cue {cue.id} time {cue.time_ticks} != cluster timecode "
                        f"{target.timecode}; cluster timecode kept as truth",
                subject=cue.id,
                detail={"cue_time_ticks": cue.time_ticks,
                        "cluster_timecode": target.timecode}))

    # signed-16-bit boundary check on relative timecodes
    for c in clusters:
        for b in c.blocks:
            if b.rel_timecode in (-32768, 32767):
                diags.append(Diagnostic(
                    code="REL_TIMECODE_BOUNDARY",
                    message=f"block relative timecode {b.rel_timecode} sits at the "
                            "signed int16 boundary; interpreted as signed per spec",
                    subject=b.id, raw_range=b.raw_range,
                    detail={"rel_timecode": b.rel_timecode, "interpretation": "signed-int16",
                            "rejected_alternative": {"interpretation": "uint16",
                                                     "value": b.rel_timecode & 0xFFFF}}))
            if c.timecode is not None and c.timecode + b.rel_timecode < 0:
                diags.append(Diagnostic(
                    code="NEGATIVE_ABSOLUTE_TIMECODE",
                    message="cluster timecode + signed relative timecode is negative",
                    subject=b.id, raw_range=b.raw_range,
                    detail={"cluster_timecode": c.timecode,
                            "rel_timecode": b.rel_timecode}))

    return SegmentModel(timecode_scale_ns=scale, tracks=tracks, clusters=clusters,
                        cues=cues, segment_data_offset=seg.data_offset,
                        diagnostics=diags)


def to_dict(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj
