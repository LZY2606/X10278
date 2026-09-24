"""离线案例包: "Matroska 时码庭" fixture 字节构造器。

生成一段确定性的 Matroska 字节流, 覆盖反例档案要求的全部情形:
- Segment 与 Cluster#2 使用未知长度(尺寸位全 1)
- Cluster#2 内 SimpleBlock 的 relative timecode = 0x8000 (有符号溢出边界)
- Cluster#2 内另一 SimpleBlock 的 Xiph lacing 尺寸和超过负载(损坏)
- Cluster#1 的 BlockGroup 携带 DiscardPadding (有符号, 单位 ns)
- Cue#2 的 CueClusterPosition 故意错指(索引证据 ≠ 时间真值)
- 两条轨道的时基不同(DefaultDuration 与 TrackTimecodeScale 均不同),
  且块在文件中的顺序与时间顺序不一致(多轨不按文件顺序强对齐)
- Cluster#2 (t=1000 tick) 与 Cluster#3 (t=5000 tick) 之间存在中途缺口
所有量保留原始表示; 每个元素的字节范围可由解析器读回。
"""
from __future__ import annotations

EID = {
    "EBML": b"\x1a\x45\xdf\xa3", "EBMLVersion": b"\x42\x86",
    "EBMLReadVersion": b"\x42\xf7", "EBMLMaxIDLength": b"\x42\xf2",
    "EBMLMaxSizeLength": b"\x42\xf3", "DocType": b"\x42\x82",
    "DocTypeVersion": b"\x42\x87", "DocTypeReadVersion": b"\x42\x85",
    "Segment": b"\x18\x53\x80\x67", "Info": b"\x15\x49\xa9\x66",
    "TimecodeScale": b"\x2a\xd7\xb1", "MuxingApp": b"\x4d\x80",
    "Tracks": b"\x16\x54\xae\x6b", "TrackEntry": b"\xae",
    "TrackNumber": b"\xd7", "TrackUID": b"\x73\xc5", "TrackType": b"\x83",
    "FlagLacing": b"\x9c", "DefaultDuration": b"\x23\xe3\x83",
    "CodecID": b"\x86", "Name": b"\x53\x6e",
    "TrackTimecodeScale": b"\x23\x31\x4f",
    "Cluster": b"\x1f\x43\xb6\x75", "Timestamp": b"\xe7",
    "SimpleBlock": b"\xa3", "BlockGroup": b"\xa0", "Block": b"\xa1",
    "BlockDuration": b"\x9b", "DiscardPadding": b"\x75\xa2",
    "Cues": b"\x1c\x53\xbb\x6b", "CuePoint": b"\xbb", "CueTime": b"\xb3",
    "CueTrackPositions": b"\xb7", "CueTrack": b"\xf7",
    "CueClusterPosition": b"\xf1",
}

TIMECODE_SCALE = 1_000_000          # 1 tick = 1_000_000 ns = 1 ms
VIDEO_DEFAULT_DURATION = 40_000_000  # ns, 25 fps
AUDIO_DEFAULT_DURATION = 21_333_333  # ns, ~46.875 fps
AUDIO_TRACK_TIMECODE_SCALE = 1.5     # 轨道时基差异


def _size_vint(n: int, length: int | None = None) -> bytes:
    if length is None:
        length = 1
        while n >= (1 << (7 * length)) - 1:
            length += 1
    return (n | (1 << (7 * length))).to_bytes(length, "big")


def _unknown_size(length: int = 8) -> bytes:
    # EBML 未知长度: 尺寸 vint 的有效位全 1 (标记位保留在最高位)
    return b"\x01" + b"\xff" * (length - 1)


def el(name: str, payload: bytes, unknown: bool = False) -> bytes:
    size = _unknown_size() if unknown else _size_vint(len(payload))
    return EID[name] + size + payload


def uint(name: str, value: int, length: int | None = None) -> bytes:
    if length is None:
        length = max(1, (value.bit_length() + 7) // 8)
    return el(name, value.to_bytes(length, "big"))


def sint(name: str, value: int, length: int = 8) -> bytes:
    return el(name, value.to_bytes(length, "big", signed=True))


def f64(name: str, value: float) -> bytes:
    import struct
    return el(name, struct.pack(">d", value))


def string(name: str, text: str) -> bytes:
    return el(name, text.encode())


def block(track: int, rel: int, flags: int, body: bytes) -> bytes:
    """构造 Block/SimpleBlock 数据负载。rel 按有符号 16 位写入。"""
    assert -32768 <= rel <= 32767
    head = bytes([0x80 | track]) + rel.to_bytes(2, "big", signed=True) + bytes([flags])
    return head + body


def xiph_laced(sizes: list[int], fill: int = 0x41) -> bytes:
    """Xiph lacing: 除末帧外逐帧编码尺寸, 后接各帧数据。"""
    assert len(sizes) >= 2
    out = bytes([len(sizes) - 1])
    for s in sizes[:-1]:
        q, r = divmod(s, 255)
        out += b"\xff" * q + bytes([r])
    for i, s in enumerate(sizes):
        out += bytes([fill + i]) * s
    return out


def build_case_bytes() -> bytes:
    """构造完整案例文件。返回 (bytes)。布局见模块 docstring。"""
    header = el("EBML",
                uint("EBMLVersion", 1) + uint("EBMLReadVersion", 1)
                + uint("EBMLMaxIDLength", 4) + uint("EBMLMaxSizeLength", 8)
                + string("DocType", "matroska")
                + uint("DocTypeVersion", 4) + uint("DocTypeReadVersion", 2))

    info = el("Info",
              uint("TimecodeScale", TIMECODE_SCALE, 4)
              + string("MuxingApp", "timecode-court-fixture/1.0"))

    video_track = el("TrackEntry",
                     uint("TrackNumber", 1) + uint("TrackUID", 0x11)
                     + uint("TrackType", 1) + uint("FlagLacing", 1)
                     + uint("DefaultDuration", VIDEO_DEFAULT_DURATION, 4)
                     + string("CodecID", "V_TIMECODE_COURT")
                     + string("Name", "video-25fps"))
    audio_track = el("TrackEntry",
                     uint("TrackNumber", 2) + uint("TrackUID", 0x22)
                     + uint("TrackType", 2) + uint("FlagLacing", 1)
                     + uint("DefaultDuration", AUDIO_DEFAULT_DURATION, 4)
                     + f64("TrackTimecodeScale", AUDIO_TRACK_TIMECODE_SCALE)
                     + string("CodecID", "A_TIMECODE_COURT")
                     + string("Name", "audio-46.875fps"))
    tracks = el("Tracks", video_track + audio_track)

    # --- Cluster#1, Timestamp 0 ---
    # 视频块: rel=0, Xiph lacing 2 帧 (keyframe)
    v1 = el("SimpleBlock", block(1, 0, 0x80 | 0x02, xiph_laced([10, 6])))
    # 音频 BlockGroup: rel=5, 无 lacing, BlockDuration + DiscardPadding
    a1_body = bytes([20])  # 20 字节音频帧
    a1_block = el("Block", block(2, 5, 0x00, a1_body))
    bg1 = el("BlockGroup",
             a1_block + uint("BlockDuration", 21)
             + sint("DiscardPadding", 500_000, 8))   # 单位 ns, 有符号
    # 音频块: rel=30 —— 文件顺序在视频块之后, 时间不强制对齐
    a2 = el("SimpleBlock", block(2, 30, 0x02, xiph_laced([8, 8], fill=0x61)))
    cluster1 = el("Cluster", uint("Timestamp", 0, 2) + v1 + bg1 + a2)

    # --- Cluster#2, Timestamp 1000, 未知长度 ---
    # relative timecode = 0x8000 -> 有符号解释 -32768 (溢出边界)
    v2 = el("SimpleBlock", block(1, -32768, 0x00, bytes([0x90]) * 12))
    # Xiph lacing 损坏: 声明 3 帧, 尺寸和 8+8+末帧 超过实际负载
    corrupt_body = bytes([2]) + bytes([8, 8]) + bytes([0xA0]) * 6  # 需 >=16, 实 6
    v3 = el("SimpleBlock", block(1, 100, 0x02, corrupt_body))
    cluster2 = el("Cluster", uint("Timestamp", 1000, 2) + v2 + v3, unknown=True)

    # --- Cluster#3, Timestamp 5000 (与 cluster2 之间留中途缺口) ---
    a3 = el("SimpleBlock", block(2, 0, 0x04, bytes([3 - 1]) + bytes([0xC0]) * 10))
    cluster3 = el("Cluster", uint("Timestamp", 5000, 2) + a3)

    # --- Cues: cue1 正确指向 cluster1; cue2 错指(cluster1 内偏移) ---
    seg_body_prefix = info + tracks
    # CueClusterPosition 相对 Segment 数据起点, 需要布局后回填 -> 两遍构造
    def segment_with_cues(cue1_pos: int, cue2_pos: int) -> bytes:
        cues = el("Cues",
                  el("CuePoint", uint("CueTime", 0, 2)
                     + el("CueTrackPositions",
                          uint("CueTrack", 1) + uint("CueClusterPosition", cue1_pos, 4)))
                  + el("CuePoint", uint("CueTime", 5000, 2)
                     + el("CueTrackPositions",
                          uint("CueTrack", 1) + uint("CueClusterPosition", cue2_pos, 4))))
        body = seg_body_prefix + cluster1 + cluster2 + cluster3 + cues
        return el("Segment", body, unknown=True)

    seg = segment_with_cues(0, 0)
    seg_data_off = len(EID["Segment"]) + 8  # seg 不含 EBML 头: id + 8 字节未知尺寸
    c1_off = seg.find(cluster1, len(seg_body_prefix))
    cue1_pos = c1_off - seg_data_off
    cue2_pos = cue1_pos + 4          # 故意错指: 落在 cluster1 头部中间
    seg = segment_with_cues(cue1_pos, cue2_pos)
    return header + seg


CASE_KIND = "matroska-timecode-court"
CASE_TITLE = "Matroska 时码庭: cluster、cue 索引与多轨帧时间证据"
