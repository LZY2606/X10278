"""Deterministic synthetic Matroska bytes used by the offline case pack.

Nothing here is a "generic table": the bytes are genuine EBML-encoded Matroska
structures (EBML header, Segment with Info/Tracks/Clusters/Cues) so every
offset, lacing byte and cue position is real and re-parseable.
"""
from __future__ import annotations

from . import ebml
from . import ids as I


def _simple_block(track: int, rel_tc: int, frames,
                  lacing: str = "none", keyframe: bool = False,
                  flags_extra: int = 0) -> bytes:
    head = ebml.encode_vint(track)
    head += int(rel_tc).to_bytes(2, "big", signed=True)
    frame_list = list(frames) if isinstance(frames, (list, tuple)) else [frames]
    if lacing == "none":
        payload = b"".join(frame_list)
        flags = 0x00
    else:
        code = {"xiph": 1, "fixed": 2, "ebml": 3}[lacing]
        sizes = [len(f) for f in frame_list]
        lace = bytes([len(frame_list) - 1]) + _xiph_sizes(sizes) + b"".join(frame_list)
        payload = lace
        flags = code << 1
    if keyframe:
        flags |= 0x80
    flags |= flags_extra
    head += bytes([flags])
    return ebml.el(I.SIMPLE_BLOCK, head + payload)


def _xiph_sizes(sizes: list[int]) -> bytes:
    out = b""
    for sz in sizes[:-1]:
        n, rem = divmod(sz, 255)
        out += b"\xff" * n + bytes([rem])
    return out


def _block(track: int, rel_tc: int, frame: bytes) -> bytes:
    head = ebml.encode_vint(track)
    head += int(rel_tc).to_bytes(2, "big", signed=True)
    head += b"\x00"
    return ebml.el(I.BLOCK, head + frame)


def _block_group(track: int, rel_tc: int, frame: bytes, duration: int,
                 discard_padding: int | None = None) -> bytes:
    inner = _block(track, rel_tc, frame)
    inner += ebml.u(I.BLOCK_DURATION, duration)
    if discard_padding is not None:
        inner += ebml.el(I.DISCARD_PADDING,
                         int(discard_padding).to_bytes(
                             max(1, (discard_padding.bit_length() + 8) // 8),
                             "big", signed=True))
    inner += ebml.el(I.REFERENCE_BLOCK, (0).to_bytes(1, "big", signed=True))
    return ebml.el(I.BLOCK_GROUP, inner)


def _track_entry(number: int, uid: int, ttype: int, codec: str, name: str,
                 default_duration: int | None, video=None, audio=None,
                 flag_lacing=1) -> bytes:
    body = ebml.u(I.TRACK_NUMBER, number)
    body += ebml.u(I.TRACK_UID, uid)
    body += ebml.u(I.TRACK_TYPE, ttype)
    body += ebml.el(I.CODEC_ID, codec.encode())
    body += ebml.el(I.NAME, name.encode())
    if default_duration is not None:
        body += ebml.u(I.DEFAULT_DURATION, default_duration)
    body += ebml.u(I.FLAG_LACING, flag_lacing, 1)
    if video:
        v = ebml.u(I.PIXEL_WIDTH, video[0]) + ebml.u(I.PIXEL_HEIGHT, video[1])
        body += ebml.el(I.VIDEO, v)
    if audio:
        a = ebml.f(I.SAMPLING_FREQ, float(audio[0]))
        a += ebml.u(I.CHANNELS, audio[1], 1)
        body += ebml.el(I.AUDIO, a)
    return ebml.el(I.TRACK_ENTRY, body)


def build_baseline(*, timecode_scale: int = 1_000_000,
                   unknown_segment: bool = False,
                   cue_offset_delta: int = 0,
                   gap_shift: int = 0) -> bytes:
    """The initial, clean case document.

    Video 40ms frames (DefaultDuration 40_000_000 ns), audio 20ms blocks.
    Four clusters at 0/1000/2000/3000 scale units (1 s each at scale 1ms).
    Includes a 3-frame Xiph-laced video block and a BlockGroup with
    DiscardPadding.
    """
    hdr = ebml.el(I.EBML_HEADER,
                  ebml.u(I.EBML_READ_VERSION, 1, 1)
                  + ebml.el(I.EBML_DOC_TYPE, b"matroska")
                  + ebml.u(I.EBML_DOC_TYPE_VERSION, 4, 1))

    info = ebml.u(I.TIMECODE_SCALE, timecode_scale, 4)
    info += ebml.el(I.MUXING_APP, b"mkv-tribunal")
    info += ebml.el(I.WRITING_APP, b"mkv-tribunal-fixture")
    info += ebml.f(I.DURATION, 4.0)
    info = ebml.el(I.INFO, info)

    tracks = _track_entry(1, 101, 1, "V_UNCOMPRESSED", "video",
                          40_000_000, video=(320, 240))
    tracks += _track_entry(2, 202, 2, "A_PCM/INT/LIT", "audio", None,
                           audio=(48000.0, 2))
    tracks = ebml.el(I.TRACKS, tracks)

    def cluster(tc: int, blocks: bytes, unknown: bool = False) -> bytes:
        body = ebml.u(I.CLUSTER_TIMECODE, tc, 4) + blocks
        if unknown:
            return ebml.encode_id(I.CLUSTER) + ebml.encode_vint(0, unknown=True) + body
        return ebml.el(I.CLUSTER, body)

    # cluster 0: keyframe simple block + xiph-laced 3-frame block
    c0 = _simple_block(1, 0, b"V" * 16, keyframe=True)
    c0 += _simple_block(1, 40, [b"V" * 12, b"V" * 12, b"V" * 12], "xiph")
    c0 += _simple_block(2, 0, b"A" * 8)
    c0 += _simple_block(2, 20, b"A" * 8)
    c0 = cluster(0, c0)

    # cluster 1000: block group with duration + discard padding
    c1 = _simple_block(1, 0, b"V" * 16, keyframe=True)
    c1 += _block_group(2, 0, b"A" * 8, duration=20, discard_padding=2_000_000)
    c1 = cluster(1000, c1)

    # cluster 2000 (its timecode can be shifted for the gap scenario)
    c2 = _simple_block(1, 0, b"V" * 16, keyframe=True)
    c2 += _simple_block(2, 0, b"A" * 8)
    c2 = cluster(2000 + gap_shift, c2)

    c3 = _simple_block(1, 0, b"V" * 16, keyframe=True)
    c3 += _simple_block(2, 0, b"A" * 8)
    c3 = cluster(3000, c3)

    # Cues: point at cluster 0 and cluster 2000.
    # CueClusterPosition is relative to segment data start; we patch exact
    # offsets after assembly, so reserve with placeholders via post-step.
    segment_body_placeholder = info + tracks + b"\x00CLUSTERS\x00"
    seg_size_field_len = 4
    seg = ebml.encode_id(I.SEGMENT)
    seg_data_start = len(hdr) + len(seg) + seg_size_field_len
    clusters_all = c0 + c1 + c2 + c3
    c_off = len(info) + len(tracks)
    offsets = []
    for c in (c0, c1, c2, c3):
        offsets.append(c_off)
        c_off += len(c)
    cues = _cues([(0, offsets[0], None), (2000 + gap_shift, offsets[2], None)],
                 cue_offset_delta)
    # Cues are placed before clusters; that shifts cluster offsets, so rebuild.
    c_off = len(info) + len(tracks) + len(cues)
    offsets = []
    for c in (c0, c1, c2, c3):
        offsets.append(c_off)
        c_off += len(c)
    cues = _cues([(0, offsets[0], None), (2000 + gap_shift, offsets[2], None)],
                 cue_offset_delta)
    c_off2 = len(info) + len(tracks) + len(cues)
    if c_off2 != offsets[0]:
        # rebuild once more against stable cue size (cue delta constant width)
        c_off = len(info) + len(tracks) + len(cues)
        offsets = []
        for c in (c0, c1, c2, c3):
            offsets.append(c_off)
            c_off += len(c)
        cues = _cues([(0, offsets[0], None), (2000 + gap_shift, offsets[2], None)],
                     cue_offset_delta)

    body = info + tracks + cues + clusters_all
    body += ebml.el(I.VOID, b"\x00" * 8)
    if unknown_segment:
        segment = ebml.encode_id(I.SEGMENT) + ebml.encode_vint(0, unknown=True) + body
    else:
        segment = ebml.el(I.SEGMENT, body)
    return hdr + segment


def _cues(points, delta: int) -> bytes:
    body = b""
    for cue_time, cluster_pos, rel_pos in points:
        pos = ebml.u(I.CUE_TRACK, 1, 1)
        pos += ebml.u(I.CUE_CLUSTER_POS, cluster_pos + delta, 4)
        if rel_pos is not None:
            pos += ebml.u(I.CUE_REL_POS, rel_pos, 4)
        cp = ebml.u(I.CUE_TIME, cue_time, 4) + ebml.el(I.CUE_TRACK_POSITIONS, pos)
        body += ebml.el(I.CUE_POINT, cp)
    return ebml.el(I.CUES, body)
