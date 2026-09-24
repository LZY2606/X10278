"""离线案例包：Matroska 时码庭专用领域材料（非通用表格）。

baseline 完整保留：EBML 元素偏移、未知长度、track 参数、cluster/block 时码、
discard padding、cue 与原字节范围。随后每个变体只改动一个直接来源，检验影响
是否被限制在正确范围，并同时保留被采纳与未采纳候选。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from . import ids
from . import encoder as E
from .parser import parse_document
from . import engine

CASEPACK_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "casepack")

SCALE = 1_000_000  # 1ms/tick

# 三轨：视频(1) 默认 40ms；音频(2) 默认 20ms 且有正 TrackOffset；字幕(3)
VIDEO_DD = 40_000_000
AUDIO_DD = 20_000_000
AUDIO_OFFSET_TICKS = 5  # +5ms 轨道时基偏移（历史 TrackOffset）


def _ebml_header() -> bytes:
    p = b"".join([
        E.uint_elem(ids.EBMLVersion, 1),
        E.uint_elem(ids.EBMLReadVersion, 1),
        E.uint_elem(ids.EBMLMaxIDLength, 4),
        E.uint_elem(ids.EBMLMaxSizeLength, 8),
        E.string_elem(ids.DocType, "matroska"),
        E.uint_elem(ids.DocTypeVersion, 4),
        E.uint_elem(ids.DocTypeReadVersion, 2),
    ])
    return E.elem(ids.EBML, p)


def _info(scale=SCALE, muxer="mkvlab-casepack") -> bytes:
    return E.elem(ids.Info,
                  E.uint_elem(ids.TimecodeScale, scale) +
                  E.string_elem(ids.MuxingApp, muxer) +
                  E.string_elem(ids.WritingApp, "mkvlab"))


def _tracks(video_dd=VIDEO_DD, audio_dd=AUDIO_DD,
            audio_offset=AUDIO_OFFSET_TICKS) -> bytes:
    v = b"".join([
        E.uint_elem(ids.TrackNumber, 1),
        E.uint_elem(ids.TrackUID, 101),
        E.uint_elem(ids.TrackType, 1),
        E.uint_elem(ids.FlagDefault, 1),
        E.uint_elem(ids.FlagLacing, 1),
        E.uint_elem(ids.DefaultDuration, video_dd),
        E.string_elem(ids.CodecID, "V_MPEG4/ISO/AVC"),
        E.string_elem(ids.Name, "video"),
    ])
    a = b"".join([
        E.uint_elem(ids.TrackNumber, 2),
        E.uint_elem(ids.TrackUID, 202),
        E.uint_elem(ids.TrackType, 2),
        E.uint_elem(ids.FlagDefault, 1),
        E.uint_elem(ids.FlagLacing, 1),
        E.uint_elem(ids.DefaultDuration, audio_dd),
        E.sint_elem(ids.TrackOffset, audio_offset),
        E.string_elem(ids.CodecID, "A_OPUS"),
        E.string_elem(ids.Name, "audio"),
        E.elem(ids.Audio, E.float_elem(ids.SamplingFrequency, 48000.0) +
               E.uint_elem(ids.Channels, 2)),
    ])
    s = b"".join([
        E.uint_elem(ids.TrackNumber, 3),
        E.uint_elem(ids.TrackUID, 303),
        E.uint_elem(ids.TrackType, 17),
        E.uint_elem(ids.FlagDefault, 0),
        E.string_elem(ids.CodecID, "S_TEXT/UTF8"),
        E.string_elem(ids.Name, "subs"),
    ])
    return E.elem(ids.Tracks, E.elem(ids.TrackEntry, v) +
                  E.elem(ids.TrackEntry, a) + E.elem(ids.TrackEntry, s))


def _cluster(timecode, entries, unknown_size=False, prev_size=None) -> bytes:
    """entries: [(track, rel_tc, kind, payload_spec), ...]"""
    body = E.uint_elem(ids.Timecode, timecode)
    if prev_size is not None:
        body += E.uint_elem(ids.PrevSize, prev_size)
    for spec in entries:
        body += _block_from_spec(spec)
    if unknown_size:
        return E.elem(ids.Cluster, body, unknown_size=True,
                      size_width_override=4)
    return E.elem(ids.Cluster, body)


def _block_from_spec(spec):
    track, rel, kind, opt = spec
    if kind == "simple":
        frames = opt.get("frames", [b"vframe"])
        lacing = opt.get("lacing", E.LACING_NONE)
        return E.simple_block(track, rel, frames, lacing=lacing,
                              keyframe=opt.get("keyframe", True))
    if kind == "group":
        frames = opt.get("frames", [b"aframe"])
        lacing = opt.get("lacing", E.LACING_NONE)
        bg = E.block(track, rel, frames, lacing=lacing,
                     invisible=opt.get("invisible", False))
        if opt.get("duration") is not None:
            bg += E.uint_elem(ids.BlockDuration, opt["duration"])
        if opt.get("reference") is not None:
            bg += E.sint_elem(ids.ReferenceBlock, opt["reference"])
        if opt.get("discard") is not None:
            bg += E.sint_elem(ids.DiscardPadding, opt["discard"])
        return E.elem(ids.BlockGroup, bg)
    if kind == "raw":
        return E.binary_elem(ids.SimpleBlock, opt["bytes"])
    raise ValueError(kind)


def _cue(cue_time, track, cluster_seg_offset, duration=None,
         relative=None, block_num=None) -> bytes:
    p = (E.uint_elem(ids.CueTime, cue_time) +
         E.elem(ids.CueTrackPositions,
                E.uint_elem(ids.CueTrack, track) +
                E.uint_elem(ids.CueClusterPosition, cluster_seg_offset) +
                (E.uint_elem(ids.CueRelativePosition, relative)
                 if relative is not None else b"") +
                (E.uint_elem(ids.CueDuration, duration)
                 if duration is not None else b"") +
                (E.uint_elem(ids.CueBlockNumber, block_num)
                 if block_num is not None else b"")))
    return E.elem(ids.CuePoint, p)


def _baseline_clusters():
    c0 = _cluster(0, [
        (1, 0, "simple", {"frames": [b"v-key-0"], "keyframe": True}),
        (2, 0, "group", {"frames": [b"a0"], "duration": 20,
                         "discard": 3, "reference": None}),
        (1, 40, "simple", {"frames": [b"v-p-40"], "keyframe": False}),
    ])
    c1 = _cluster(100, [
        (1, 0, "simple", {"frames": [b"l0", b"l1", b"l2"],
                          "lacing": E.LACING_XIPH, "keyframe": True}),
        (2, 0, "group", {"frames": [b"a100"], "duration": 20}),
        (2, 20, "group", {"frames": [b"a120"], "duration": 20,
                          "discard": -2}),
    ], unknown_size=True)
    c2 = _cluster(200, [
        (1, 0, "simple", {"frames": [b"v200"], "keyframe": True}),
        (2, 0, "group", {"frames": [b"a200"], "duration": 20}),
        (1, 40, "simple", {"frames": [b"v240"], "keyframe": False}),
    ])
    c3 = _cluster(300, [
        (1, 0, "simple", {"frames": [b"v300"], "keyframe": True}),
        (3, 0, "simple", {"frames": [b"sub-300"], "keyframe": True}),
    ])
    return [c0, c1, c2, c3]


def _segment_unknown(body: bytes, seg_size_width=8) -> bytes:
    return E.encode_id(ids.Segment) + E.encode_unknown_size(seg_size_width) + body


def build_baseline(scale=SCALE) -> dict:
    """组装 baseline。返回 blob 与构造期坐标标记。"""
    header = _ebml_header()
    info = _info(scale)
    tracks = _tracks()
    clusters = _baseline_clusters()
    seg_body_offsets = {}
    body = info + tracks
    seg_body_offsets["info"] = 0
    seg_body_offsets["tracks"] = len(info)
    seg_body_offsets["clusters"] = []
    for c in clusters:
        seg_body_offsets["clusters"].append((len(body), len(body) + len(c)))
        body += c
    seg = _segment_unknown(body)
    # cue 放在 cluster 之后（仍在 Segment 内），用 seg-relative 偏移
    cue_payload = b""
    for i, (a, b) in enumerate(seg_body_offsets["clusters"]):
        cue_payload += _cue(i * 100, 1, a, duration=40 if i == 0 else None)
    cues_el = E.elem(ids.Cues, cue_payload)
    cues_seg_offset = len(body)
    body += cues_el
    # 重新组装 segment（cue 已入 body）
    seg = _segment_unknown(body)
    blob = header + seg
    return {
        "blob": blob,
        "seg_header_len": len(header),
        "seg_data_start": len(header) + len(E.encode_id(ids.Segment)) + 8,
        "layout": {
            "clusters_seg_offsets": seg_body_offsets["clusters"],
            "cues_seg_offset": cues_seg_offset,
        },
    }


def _patch(blob: bytes, start: int, new: bytes) -> bytes:
    return blob[:start] + new + blob[start + len(new):]


# ---- 七个“只改动一个直接来源”的反例 ----
def mutate_unknown_length(blob, parsed, layout):
    """变体 1：把一个已知长度 Cluster 改为未知长度（唯一改动源）。

    baseline 的 cluster0/2/3 是已知长度。改写 cluster0 size VINT 为未知长度 4 字节，
    并在其后补 3 字节 Void 占位以保持后续偏移（仅结构等价填充）。
    """
    seg = parsed["segment"]
    cl = [c for c in seg.children if c.id == ids.Cluster and not c.unknown_size][0]
    # size VINT 等宽替换为未知长度，blob 总长与后续偏移保持不变
    sw = cl.payload_offset - cl.size_offset
    new_size = E.encode_unknown_size(sw)
    start = cl.size_offset
    changed = _patch(blob, start, new_size)
    return {"blob": changed,
            "ranges": [(start, start + sw)],
            "change": "cluster0 size -> unknown length",
            "expected_codes": ["UNKNOWN_LENGTH"]}


def mutate_rel_tc_overflow(blob, parsed, layout):
    """变体 2：单字节来源——将 cluster0 第二个 video block 的 relative timecode
    由 40 改为接近 int16 上溢的 32000。"""
    seg = parsed["segment"]
    target = _find_block_by_content(seg, b"v-p-40")
    tc_off = target.payload_offset + 1  # track VINT 宽 1
    new_tc = (32000).to_bytes(2, "big", signed=True)
    changed = _patch(blob, tc_off, new_tc)
    return {"blob": changed, "ranges": [(tc_off, tc_off + 2)],
            "change": "relative timecode 40 -> 32000（有符号近边界）",
            "expected_codes": ["REL_TC_NEAR_OVERFLOW"]}


def _blocks_in_segment(seg):
    from .parser import Element
    out = []
    for c in seg.children:
        if c.id == ids.Cluster:
            for b in c.children:
                if b.id == ids.SimpleBlock:
                    out.append(b)
                elif b.id == ids.BlockGroup:
                    for b2 in b.children:
                        if b2.id == ids.Block:
                            out.append(b2)
    return out


def _find_block_by_content(seg, frame_marker: bytes):
    for b in _blocks_in_segment(seg):
        if frame_marker in b.payload:
            return b
    raise LookupError(frame_marker)


def mutate_lace_corruption(blob, parsed, layout):
    """变体 3：lacing 损坏——破坏 cluster1 三帧 Xiph lace 的尺寸表首字节。"""
    target = _find_block_by_content(parsed["segment"], b"l0")
    # payload: track(1) + tc(2) + flags(1) + lacesize(1) + sizes(2) ...
    lace_size_off = target.payload_offset + 4
    start = lace_size_off + 1
    changed = _patch(blob, start, b"\xff")
    return {"blob": changed, "ranges": [(start, start + 1)],
            "change": "Xiph lace 尺寸表首字节置 0xFF 且无终止字节（截断）",
            "expected_codes": ["LACE_CORRUPTION", "XIPH_LACE_TRUNCATED"]}


def mutate_discard_padding(blob, parsed, layout):
    """变体 4：把 cluster1 第二个音频块的 DiscardPadding 改成大负值（唯一来源）。"""
    seg = parsed["segment"]
    disc = None
    for c in seg.children:
        if c.id != ids.Cluster:
            continue
        for bg in c.children:
            if bg.id != ids.BlockGroup:
                continue
            d = next((x for x in bg.children if x.id == ids.DiscardPadding), None)
            if d is not None and d.value == -2:
                disc = d
    assert disc is not None
    new = E.sint_payload(-2000)
    # 长度可能变化：在 DiscardPadding 元素位置做等长安全替换（重编码元素）
    old_bytes = blob[disc.id_offset:disc.end_offset]
    new_elem = E.sint_elem(ids.DiscardPadding, -2000)
    if len(new_elem) > len(old_bytes):
        # 缩短数值以保证等宽
        new_elem = E.elem(ids.DiscardPadding,
                          (-2000).to_bytes(len(disc.payload), "big", signed=True))
    changed = blob[:disc.id_offset] + new_elem + blob[disc.id_offset + len(new_elem):]
    return {"blob": changed,
            "ranges": [(disc.id_offset, disc.id_offset + len(new_elem))],
            "change": "DiscardPadding -2 -> -2000 ticks",
            "expected_codes": ["NEGATIVE_DISCARD_PADDING"]}


def mutate_cue_mispoint(blob, parsed, layout):
    """变体 5：cue 错指——把第 2 个 cue point 的 CueClusterPosition 指向
    一个不存在的 segment-relative 偏移（唯一来源）。"""
    from .parser import find_all
    cues = find_all(parsed["roots"], ids.CuePoint)
    cp = cues[1]
    ccp = next(x for x in _walk(cp) if x.id == ids.CueClusterPosition)
    bogus = 9_999_999
    payload = E.uint_payload(bogus)
    new_elem = E.elem(ids.CueClusterPosition, payload,
                      size_width_override=ccp.payload_offset + len(ccp.payload) -
                      ccp.size_offset)
    old_len = ccp.end_offset - ccp.id_offset
    assert len(new_elem) == old_len, (len(new_elem), old_len)
    changed = blob[:ccp.id_offset] + new_elem + blob[ccp.end_offset:]
    return {"blob": changed,
            "ranges": [(ccp.id_offset, ccp.end_offset)],
            "change": "CueClusterPosition -> 9999999（悬空错指）",
            "expected_codes": ["CUE_DANGLING"]}


def _walk(el):
    yield el
    for c in el.children:
        yield from _walk(c)


def mutate_track_timebase(blob, parsed, layout):
    """变体 6：轨道时基差异——把音频轨 DefaultDuration 从 20ms 改为 23ms。

    来源是 track 参数本身；期望影响仅限音频轨的帧时长与时间线缺口判断，
    视频/字幕轨结论保持不变。
    """
    seg = parsed["segment"]
    dd = None
    for te in seg.children:
        if te.id != ids.Tracks:
            continue
        for trk in te.children:
            num = next((x.value for x in trk.children
                        if x.id == ids.TrackNumber), None)
            if num == 2:
                dd = next(x for x in trk.children
                          if x.id == ids.DefaultDuration)
    assert dd is not None
    new_val = 23_000_000
    new_elem = E.uint_elem(ids.DefaultDuration, new_val)
    old_len = dd.end_offset - dd.id_offset
    assert len(new_elem) == old_len
    changed = blob[:dd.id_offset] + new_elem + blob[dd.end_offset:]
    return {"blob": changed,
            "ranges": [(dd.id_offset, dd.end_offset)],
            "change": "audio DefaultDuration 20ms -> 23ms（仅音频时基）",
            "expected_codes": ["TRACK_TIME_GAP"],
            "rule_overrides": None}


def mutate_cluster_gap(blob, parsed, layout):
    """变体 7：中途 cluster 缺口——删除 cluster2 的 Timecode 元素以外的整个
    cluster 块，制造字节层面的中途 cluster 缺口，cue 随之错指（单一来源：字节删除）。"""
    seg = parsed["segment"]
    cls = [c for c in seg.children if c.id == ids.Cluster]
    victim = cls[2]
    start, end = victim.id_offset, victim.end_offset
    changed = blob[:start] + blob[end:]
    return {"blob": changed, "ranges": [(start, end)],
            "removed": True,
            "change": "删除 cluster2（200ms）制造中途 cluster 缺口",
            "expected_codes": ["MIDSTREAM_CLUSTER_GAP", "CUE_MISPOINT",
                               "CUE_DANGLING"]}


MUTATIONS = [
    ("unknown_length", mutate_unknown_length),
    ("rel_tc_overflow", mutate_rel_tc_overflow),
    ("lace_corruption", mutate_lace_corruption),
    ("discard_padding", mutate_discard_padding),
    ("cue_mispoint", mutate_cue_mispoint),
    ("track_timebase", mutate_track_timebase),
    ("cluster_gap", mutate_cluster_gap),
]
