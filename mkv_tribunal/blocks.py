"""Block / SimpleBlock 头部与 lacing 解析。

守住的语义：
* relative timecode 始终是 16 位二补码有符号整数（matroska-block）；
* Xiph/EBML lacing 首字节是帧数-1；fixed lacing 没有帧数字节，只能靠
  payload 可整除性 + DefaultDuration 得到候选，不能唯一决定时保留全部合法候选；
* lacing 一旦损坏，只标记损坏字节范围，绝不猜测帧边界、不产出伪造帧。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .vint import decode as read_vint, decode_signed, VintError


@dataclass
class FrameCandidate:
    """一种可行的分帧解释。fixed lacing 下可能存在多个并列候选。"""
    count: int
    frame_sizes: list[int]
    complete: bool


@dataclass
class LacingResult:
    mode: str  # no / xiph / fixed / ebml
    candidates: list[FrameCandidate] = field(default_factory=list)
    damaged: bool = False
    damage_offset: int | None = None
    damage_end: int | None = None
    damage_code: str | None = None
    damage_message: str | None = None
    # 只有在所有候选一致时才有单一帧切分
    definitive: bool = False


@dataclass
class BlockHeader:
    track_number: int
    relative_timecode: int  # 有符号 16 位，单位 ms（Block 本地时基）
    relative_timecode_unsigned: int  # 原始无符号视图，留作证据
    flags: int
    keyframe: bool
    lacing: int
    payload_offset: int  # 相对整个 Block 元素数据


def parse_header(data: bytes, simple: bool) -> tuple[BlockHeader | None, str | None]:
    """解析 Block 头（track VINT + int16 + flags），失败返回 (None, 原因)。"""
    try:
        track_number, tn_width, _ = read_vint(data, 0)
    except VintError as exc:
        return None, "track VINT 损坏: %s" % exc
    need = tn_width + 3
    if len(data) < need:
        return None, "Block 头在 %d 字节处截断（需要 %d）" % (len(data), need)
    unsigned = int.from_bytes(data[tn_width:tn_width + 2], "big")
    rel = decode_signed(unsigned, 16)
    flags = data[tn_width + 2]
    lacing = (flags >> 1) & 0x3
    keyframe = bool(flags & 0x80) if simple else False
    return BlockHeader(
        track_number=track_number,
        relative_timecode=rel,
        relative_timecode_unsigned=unsigned,
        flags=flags,
        keyframe=keyframe,
        lacing=lacing,
        payload_offset=need,
    ), None


LACING_NAMES = {0: "no", 1: "xiph", 2: "ebml", 3: "fixed"}


def _xiph_sizes(payload: bytes, count: int) -> tuple[list[int] | None, int, str | None]:
    sizes: list[int] = []
    pos = 0
    for _ in range(count - 1):
        acc = 0
        while pos < len(payload):
            byte = payload[pos]
            pos += 1
            acc += byte
            if byte != 255:
                break
        else:
            return None, pos, "Xiph 尺寸序列越过 payload"
        sizes.append(acc)
    used = sum(sizes)
    last = len(payload) - pos - used
    if last < 0:
        return None, pos, "Xiph 已声明尺寸之和 %d 超过载荷长度 %d" % (used, len(payload))
    sizes.append(last)
    return sizes, pos, None


def _ebml_sizes(payload: bytes, count: int
                ) -> tuple[list[int] | None, int, str | None]:
    sizes: list[int] = []
    pos = 0
    first = None
    for i in range(count - 1):
        try:
            value, width, mask = read_vint(payload, pos)
        except VintError as exc:
            return None, pos, "EBML 尺寸 VINT 损坏: %s" % exc
        signed = decode_signed(value, 7 * width)
        if i == 0:
            first = signed
            total = first
        else:
            total = sizes[-1] + signed
        if total < 0:
            return None, pos, "EBML lacing 累计尺寸为负"
        sizes.append(total)
        pos += width
    assert first is not None
    used = sum(sizes)
    last = len(payload) - pos - used
    if last < 0:
        return None, pos, "EBML 已声明尺寸之和 %d 超过载荷 %d" % (used, len(payload))
    sizes.append(last)
    return sizes, pos, None


def parse_lacing(data: bytes, header: BlockHeader,
                 expected_frames: int | None = None) -> LacingResult:
    mode = LACING_NAMES[header.lacing]
    result = LacingResult(mode=mode)
    payload = data[header.payload_offset:]
    if mode == "no":
        result.candidates = [FrameCandidate(1, [len(payload)], True)]
        result.definitive = True
        return result
    if not payload:
        result.damaged = True
        result.damage_offset = header.payload_offset
        result.damage_end = len(data)
        result.damage_code = "EMPTY_LACING_PAYLOAD"
        result.damage_message = "%s lacing Block 没有载荷" % mode
        return result
    if mode in ("xiph", "ebml"):
        count_minus_one = payload[0]
        count = count_minus_one + 1
        body = payload[1:]
        if mode == "xiph":
            sizes, used, err = _xiph_sizes(body, count)
        else:
            sizes, used, err = _ebml_sizes(body, count)
        if err is not None:
            result.damaged = True
            result.damage_offset = header.payload_offset
            result.damage_end = len(data)
            result.damage_code = "LACING_%s_CORRUPT" % mode.upper()
            result.damage_message = err
            return result
        result.candidates = [FrameCandidate(count, sizes, True)]
        result.definitive = True
        return result
    # fixed lacing：无帧数字节。帧数 N>=2 时每帧等长，故 N 必须整除
    # len(payload)。整除性给出的全部候选都保留；当 BlockDuration/
    # DefaultDuration 提供了帧数证据 (expected_frames) 时用它筛选，
    # 仍有多个解时不收敛、不猜。
    n = len(payload)
    divisors: list[int] = []
    probe = 2
    while probe * probe <= n:
        if n % probe == 0:
            divisors.append(probe)
            if probe * probe != n and n // probe >= 2:
                divisors.append(n // probe)
        probe += 1
    if n >= 2:
        divisors.append(n)
    candidates: list[FrameCandidate] = [
        FrameCandidate(frames, [n // frames] * frames, True)
        for frames in sorted(divisors)
    ]
    if not candidates:
        # n==1 时无 N>=2 整除解：载荷不足以容纳 >=2 个等长帧
        result.damaged = True
        result.damage_offset = header.payload_offset
        result.damage_end = len(data)
        result.damage_code = "LACING_FIXED_NO_SOLUTION"
        result.damage_message = "fixed lacing 载荷 %d 字节无法分成 >=2 个等长帧" % n
        return result
    if expected_frames is not None:
        narrowed = [c for c in candidates if c.count == expected_frames]
        if len(narrowed) == 1:
            candidates = narrowed
    result.candidates = candidates
    result.definitive = len(candidates) == 1
    return result
