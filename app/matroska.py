"""Matroska 语义层:Block/lacing/时码/cue/多轨时间线。

语义红线(不可为得到单一答案而吞掉多解或不确定):
- Block relative timecode 一律按**有符号** int16 解释。
- lacing 损坏时不猜帧边界:保留原始帧区字节,帧列表标记为 unknown。
- cue 是索引证据而不是时间真值:错指只产生诊断,不改写时间线。
- 多轨不按文件顺序强对齐:每条轨道独立时间线,各自按时间码排序。
所有派生量携带单位/坐标基准;原始表示在导入层保留,这里才做规范化。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ebml import Element, read_vint, EBMLFormatError

DEFAULT_TIMECODE_SCALE_NS = 1_000_000  # 1 tick = 1ms,单位 ns

LACING_NONE = 0
LACING_XIPH = 1
LACING_FIXED = 2
LACING_EBML = 3


@dataclass
class Frame:
    index: int
    offset: int           # 文件字节偏移
    size: int | None      # None => 边界未知(lacing 损坏)
    timecode_rel: int     # 有符号相对时码(单位: tick)
    timestamp_ns: int | None  # 派生: 绝对时间,单位 ns;无法确定时为 None
    keyframe: bool
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class Block:
    track_number: int
    timecode_rel: int            # 有符号 int16,单位: tick(基准: cluster 时码)
    offset: int                  # 元素文件偏移
    end: int
    lacing: int
    keyframe: bool
    frames: list[Frame] = field(default_factory=list)
    block_duration: int | None = None   # tick
    discard_padding_ns: int | None = None  # 单位 ns(Matroska 规定)
    simple: bool = True
    corrupt: bool = False
    diagnostics: list[str] = field(default_factory=list)
    raw_frame_area: tuple[int, int] | None = None  # 帧区原始字节范围(保留)


def parse_block_payload(el: Element, payload: bytes) -> Block:
    """解析 Block/SimpleBlock 载荷。损坏时保留原字节,不猜边界。"""
    base = el.data_offset
    blk = Block(track_number=-1, timecode_rel=0, offset=el.offset, end=el.end,
                lacing=LACING_NONE, keyframe=False, simple=(el.name == "SimpleBlock"))
    try:
        track, tlen = read_vint(payload, 0, keep_marker=False)
        blk.track_number = track
    except EBMLFormatError as exc:
        blk.corrupt = True
        blk.diagnostics.append(f"block_header_corrupt: {exc}")
        blk.raw_frame_area = (base, el.end)
        return blk
    if len(payload) < tlen + 3:
        blk.corrupt = True
        blk.diagnostics.append("block_header_corrupt: 头部不足 4 字节")
        blk.raw_frame_area = (base, el.end)
        return blk
    # 关键语义: relative timecode 按有符号 int16 解释
    blk.timecode_rel = int.from_bytes(payload[tlen:tlen + 2], "big", signed=True)
    flags = payload[tlen + 2]
    blk.keyframe = bool(flags & 0x80) if blk.simple else True
    blk.lacing = (flags >> 1) & 0x03
    frame_area_off = tlen + 3
    frame_area = payload[frame_area_off:]
    blk.raw_frame_area = (base + frame_area_off, el.end)

    def corrupt(msg: str):
        blk.corrupt = True
        blk.diagnostics.append(f"lacing_corrupt: {msg}; 不猜帧边界,帧区原字节保留")
        blk.frames = [Frame(index=0, offset=base + frame_area_off,
                            size=None, timecode_rel=blk.timecode_rel,
                            timestamp_ns=None, keyframe=blk.keyframe,
                            diagnostics=["frame_boundaries_unknown"])]

    if blk.lacing == LACING_NONE:
        blk.frames = [Frame(index=0, offset=base + frame_area_off, size=len(frame_area),
                            timecode_rel=blk.timecode_rel, timestamp_ns=None,
                            keyframe=blk.keyframe)]
    elif blk.lacing == LACING_FIXED:
        if not frame_area:
            return corrupt("fixed lacing 缺帧数字节") or blk
        count = frame_area[0] + 1
        body = frame_area[1:]
        if count <= 0 or len(body) % count != 0:
            return corrupt(f"fixed lacing: {len(body)} 字节无法均分为 {count} 帧") or blk
        each = len(body) // count
        blk.frames = [Frame(index=i, offset=base + frame_area_off + 1 + i * each,
                            size=each, timecode_rel=blk.timecode_rel,
                            timestamp_ns=None, keyframe=blk.keyframe)
                      for i in range(count)]
    elif blk.lacing == LACING_XIPH:
        if not frame_area:
            return corrupt("xiph lacing 缺帧数字节") or blk
        count = frame_area[0] + 1
        pos = 1
        sizes: list[int] = []
        ok = True
        for _ in range(count - 1):
            acc = 0
            while True:
                if pos >= len(frame_area):
                    ok = False
                    break
                b = frame_area[pos]
                pos += 1
                acc += b
                if b != 255:
                    break
            if not ok:
                break
            sizes.append(acc)
        if not ok or sum(sizes) > len(frame_area) - pos:
            return corrupt("xiph lacing: 尺寸表越界或超过帧区") or blk
        last = len(frame_area) - pos - sum(sizes)
        sizes.append(last)
        off = base + frame_area_off + pos
        frames = []
        for i, s in enumerate(sizes):
            frames.append(Frame(index=i, offset=off, size=s,
                                timecode_rel=blk.timecode_rel, timestamp_ns=None,
                                keyframe=blk.keyframe and i == 0))
            off += s
        blk.frames = frames
    else:  # EBML lacing
        if not frame_area:
            return corrupt("ebml lacing 缺帧数字节") or blk
        count = frame_area[0] + 1
        pos = 1
        try:
            first_size, n = read_vint(frame_area, pos, keep_marker=False)
            pos += n
        except EBMLFormatError:
            return corrupt("ebml lacing: 首个帧尺寸 VINT 损坏") or blk
        sizes = [first_size]
        ok = True
        for _ in range(count - 2):
            try:
                diff_raw, n = read_vint(frame_area, pos, keep_marker=False)
            except EBMLFormatError:
                ok = False
                break
            pos += n
            # EBML lacing 尺寸差是有符号值,偏置 (2^(7n-1) - 1)
            bias = (1 << (7 * n - 1)) - 1
            diff = diff_raw - bias
            nxt = sizes[-1] + diff
            if nxt < 0:
                ok = False
                break
            sizes.append(nxt)
        if not ok or sum(sizes) > len(frame_area) - pos:
            return corrupt("ebml lacing: 尺寸序列非法或超过帧区") or blk
        last = len(frame_area) - pos - sum(sizes)
        if last < 0:
            return corrupt("ebml lacing: 末帧尺寸为负") or blk
        sizes.append(last)
        off = base + frame_area_off + pos
        frames = []
        for i, s in enumerate(sizes):
            frames.append(Frame(index=i, offset=off, size=s,
                                timecode_rel=blk.timecode_rel, timestamp_ns=None,
                                keyframe=blk.keyframe and i == 0))
            off += s
        blk.frames = frames
    return blk


def collect_blocks(cluster: Element, data: bytes) -> list[Block]:
    """收集 cluster 内全部 Block(含 BlockGroup),并挂接 BlockDuration/DiscardPadding。"""
    blocks: list[Block] = []
    for child in cluster.children:
        if child.name == "SimpleBlock":
            blocks.append(parse_block_payload(child, data[child.data_offset:child.end]))
        elif child.name == "BlockGroup":
            inner = None
            duration = None
            discard = None
            for gc in child.children:
                if gc.name == "Block":
                    inner = gc
                elif gc.name == "BlockDuration":
                    duration = gc.value
                elif gc.name == "DiscardPadding":
                    discard = gc.value  # 单位 ns,有符号
            if inner is not None:
                blk = parse_block_payload(inner, data[inner.data_offset:inner.end])
                blk.block_duration = duration
                blk.discard_padding_ns = discard
                blocks.append(blk)
    return blocks


def frame_timestamps_ns(cluster_timecode: int, blk: Block, timecode_scale_ns: int,
                        track_timecode_scale: float, default_duration_ns: int | None,
                        frame_index_stride: bool = True) -> None:
    """就地计算帧绝对时间(单位 ns)。

    公式: (cluster_timecode + block.timecode_rel) * timecode_scale_ns * track_timecode_scale
    lacing 多帧时按 default_duration 均步进;无法确定时保留 None 并附诊断。
    """
    base_ticks = cluster_timecode + blk.timecode_rel  # timecode_rel 有符号
    scale = timecode_scale_ns * track_timecode_scale
    n = len(blk.frames)
    for i, fr in enumerate(blk.frames):
        if fr.size is None:
            fr.timestamp_ns = None
            fr.diagnostics.append("timestamp_unknown: lacing 损坏,不猜帧边界")
            continue
        ticks = base_ticks
        if frame_index_stride and n > 1 and default_duration_ns:
            step_ticks = default_duration_ns / scale
            ticks = base_ticks + round(i * step_ticks)
        fr.timecode_rel = blk.timecode_rel
        fr.timestamp_ns = round(ticks * scale)


def detect_cluster_gaps(clusters_info: list[dict], default_duration_ns: int | None,
                        timecode_scale_ns: int) -> list[dict]:
    """检测中途 cluster 缺口:相邻 cluster 起始时码间隔异常(>2 倍默认帧时长)。"""
    gaps = []
    threshold_ns = (default_duration_ns or 40_000_000) * 2
    ordered = sorted(clusters_info, key=lambda c: c["timecode"])
    for prev, cur in zip(ordered, ordered[1:]):
        delta_ns = (cur["timecode"] - prev["timecode"]) * timecode_scale_ns
        if delta_ns > threshold_ns:
            gaps.append({
                "type": "cluster_gap",
                "after_cluster": prev["index"],
                "before_cluster": cur["index"],
                "gap_start_ns": prev["timecode"] * timecode_scale_ns,
                "gap_end_ns": cur["timecode"] * timecode_scale_ns,
                "unit": "ns",
                "basis": "cluster_timecode*TimecodeScale",
                "note": "中途 cluster 缺口:不插值、不补帧,仅标记区间",
            })
    return gaps


def validate_cues(cues: list[dict], track_timelines: dict[int, list[Frame]],
                  timecode_scale_ns: int, tolerance_ns: int = 1_000_000) -> list[dict]:
    """cue 是索引证据而非时间真值:只比对、出诊断,不改写时间线。

    返回每个 cue point 的裁定: match / mispoint / unverifiable。
    """
    results = []
    for i, cue in enumerate(cues):
        cue_ns = cue["cue_time"] * timecode_scale_ns
        track = cue.get("cue_track")
        frames = track_timelines.get(track, [])
        known = [f.timestamp_ns for f in frames if f.timestamp_ns is not None]
        entry = {
            "cue_index": i,
            "cue_time_ticks": cue["cue_time"],
            "cue_time_ns": cue_ns,
            "cue_track": track,
            "cue_cluster_position": cue.get("cue_cluster_position"),
            "unit": "ns",
            "basis": "CueTime*TimecodeScale; CueClusterPosition 相对 Segment 数据起点",
        }
        if not known:
            entry["verdict"] = "unverifiable"
            entry["note"] = "对应轨道无可用帧时间(lacing 损坏或轨道缺失),cue 仅作索引证据保留"
        elif any(abs(t - cue_ns) <= tolerance_ns for t in known):
            entry["verdict"] = "match"
        else:
            nearest = min(known, key=lambda t: abs(t - cue_ns))
            entry["verdict"] = "mispoint"
            entry["nearest_frame_ns"] = nearest
            entry["delta_ns"] = cue_ns - nearest
            entry["note"] = "cue 错指:作为诊断保留,时间线不被 cue 改写"
        results.append(entry)
    return results
