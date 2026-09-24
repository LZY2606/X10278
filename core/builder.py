"""Builds the offline case package "Matroska timecode court".

The baseline is a hand-assembled, spec-shaped EBML byte stream. Every mutant is
a same-length, in-place patch of exactly one direct source, so element offsets
(and therefore stable uids) are preserved while the targeted boundary condition
is triggered.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import ebml as E


def encode_vint(value: int, width: Optional[int] = None) -> bytes:
    if width is None:
        width = max(1, (value.bit_length() + 6) // 7)
    marker = 1 << (7 * width)
    return (marker | value).to_bytes(width, "big")


def unknown_size(width: int = 8) -> bytes:
    return bytes([0x01] + [0x00] * (width - 2) + [0xFF]) \
        if width > 1 else b"\xFF"


def elem(eid: int, content: bytes, size: Optional[int] = None) -> bytes:
    eid_bytes = eid.to_bytes({1: 1, 2: 2, 3: 3, 4: 4}[
        max(1, (eid.bit_length() + 7) // 8)], "big")
    if size is None:
        return eid_bytes + encode_vint(len(content)) + content
    if size < 0:
        return eid_bytes + unknown_size(abs(size)) + content
    return eid_bytes + encode_vint(size) + content


def u(eid: int, value: int, width: Optional[int] = None) -> bytes:
    if width is None:
        width = max(1, (value.bit_length() + 7) // 8)
    return elem(eid, value.to_bytes(width, "big"))


def si(eid: int, value: int, width: Optional[int] = None) -> bytes:
    if width is None:
        width = max(1, (value.bit_length() + 8) // 8)
    return elem(eid, value.to_bytes(width, "big", signed=True))


def flt(eid: int, value: float) -> bytes:
    import struct
    return elem(eid, struct.pack(">d", value))


def st(eid: int, value: str) -> bytes:
    return elem(eid, value.encode("utf-8"))


def block_content(track_num: int, rel_signed: int, flags: int,
                  payload: bytes) -> bytes:
    return encode_vint(track_num) + (rel_signed & 0xFFFF).to_bytes(
        2, "big") + bytes([flags]) + payload


def xiph_block(track_num: int, rel_signed: int, flags: int,
               frame_sizes: list[int]) -> bytes:
    body = b"".join(bytes([0x41 + i]) * s for i, s in enumerate(frame_sizes))
    hdr = encode_vint(track_num) + (rel_signed & 0xFFFF).to_bytes(
        2, "big") + bytes([flags])
    lace = bytes([len(frame_sizes) - 1])
    for s in frame_sizes[:-1]:
        while s >= 0xFF:
            lace += b"\xFF"
            s -= 0xFF
        lace += bytes([s])
    return hdr + lace + body


@dataclass
class BuildResult:
    data: bytes
    patch_points: dict
    segment_data_pos: int


def track_entry(number, uid, ttype, codec, name, tts: Optional[float] = None,
                default_duration: Optional[int] = None) -> bytes:
    parts = [u(E.TRACK_NUMBER, number), u(E.TRACK_UID_EL, uid, 4),
             u(E.TRACK_TYPE, ttype), u(E.FLAG_DEFAULT, 1 if number == 1 else 0),
             st(E.CODEC_ID, codec), st(E.NAME, name)]
    if tts is not None:
        parts.append(flt(E.TRACK_TIMESTAMP_SCALE, tts))
    if default_duration is not None:
        parts.append(u(E.DEFAULT_DURATION, default_duration))
    return elem(E.TRACK_ENTRY, b"".join(parts))


def build_baseline() -> BuildResult:
    """Assemble the canonical case: unknown-length Segment, two tracks with
    different timebases, signed relative timecodes, Xiph lacing, DiscardPadding,
    a mid-stream cluster gap, and Cues."""
    points: dict = {}

    ebml_header = elem(E.EBML, b"".join([
        elem(0x4286, b"\x01"),                       # EBMLVersion
        elem(0x42F7, b"\x01"),                       # EBMLReadVersion
        elem(0x42F2, b"\x04"),                       # EBMLMaxIDLength
        elem(0x42F3, b"\x08"),                       # EBMLMaxSizeLength
        st(0x4282, "matroska"),
        st(0x4287, "matroska"),
        elem(0x4285, b"\x02"),
    ]))

    info = elem(E.INFO, b"".join([
        u(E.TIMESTAMP_SCALE, 1_000_000, 4),
        flt(E.DURATION_EL, 4000.0),
        st(E.MUXING_APP, "court-builder"),
        st(E.WRITING_APP, "court-builder-1"),
    ]))

    tracks = elem(E.TRACKS, b"".join([
        track_entry(1, 1001, 1, "V_MPEG4/ISO/AVC", "video", tts=1.0,
                    default_duration=40_000_000),
        track_entry(2, 2002, 2, "A_OPUS", "audio", tts=0.5),
    ]))

    # Cluster 0 at tick 0: video keyframe, unknown-length master.
    blk0 = elem(E.SIMPLE_BLOCK, block_content(1, 0, 0x80, b"V" * 16))
    clu0_inner = u(E.TIMESTAMP_CLUSTER, 0, 2) + blk0
    points["cluster0"] = {"timecode_pos": None}
    clu0 = elem(E.CLUSTER, clu0_inner, size=-8)

    # Cluster 1 at tick 100: audio block with signed relative -1 (0xFFFF),
    # Xiph lacing (3 frames) and discard padding.
    blk1_simple = elem(E.SIMPLE_BLOCK, block_content(1, 120, 0x80, b"V" * 12))
    xiph = xiph_block(2, -1, 0x00 | (1 << 1), [4, 3, 5])
    points["audioblock_rel_offset"] = None
    dpad = si(E.DISCARD_PADDING, -2_000_000, 4)
    bg = elem(E.BLOCK_GROUP,
              elem(E.BLOCK, xiph) + u(E.BLOCK_DURATION, 20) +
              si(E.REFERENCE_BLOCK, -1, 2) + dpad)
    clu1_inner = u(E.TIMESTAMP_CLUSTER, 100, 2) + blk1_simple + bg
    clu1 = elem(E.CLUSTER, clu1_inner)

    # Cluster 2 at tick 500 -> 400-tick gap after cluster 1 end.
    blk2 = elem(E.SIMPLE_BLOCK, block_content(1, 0, 0x80, b"V" * 10))
    clu2 = elem(E.CLUSTER, u(E.TIMESTAMP_CLUSTER, 500, 2) + blk2)

    # Cues: one correct, one intentionally correct for baseline (mutant flips).
    def cue_point(track, ctime, cluster_file_pos):
        seg_data = len(ebml_header) + 4 + 8  # Segment id(4)+size(8)
        return elem(E.CUE_POINT,
                    u(E.CUE_TIME, ctime, 4) +
                    elem(E.CUE_TRACK_POSITIONS,
                         u(E.CUE_TRACK, track) +
                         u(E.CUE_CLUSTER_POSITION,
                           cluster_file_pos - seg_data, 4) +
                         u(E.CUE_RELATIVE_POSITION, 0, 2)))

    seg_children_no_cues = info + tracks + clu0 + clu1 + clu2
    seg_data_pos = len(ebml_header) + 4 + 8
    clu0_file = seg_data_pos
    clu1_file = clu0_file + len(clu0)
    clu2_file = clu1_file + len(clu1)
    cue0 = cue_point(1, 0, clu0_file)
    cue1 = cue_point(2, 99, clu1_file)   # 100 + (-1) == 99
    cue2 = cue_point(1, 500, clu2_file)
    cues = elem(E.CUES, cue0 + cue1 + cue2)

    segment = elem(E.SEGMENT, seg_children_no_cues + cues, size=-8)
    data = ebml_header + segment

    # Patch points (file offsets) ------------------------------------------
    def find_content_offset(blob, eid, occurrence=0):
        from .ebml import read_element_id, read_vint
        pos = 0
        seen = 0
        while pos < len(blob):
            try:
                eidv, idl = read_element_id(blob, pos)
                _, sl, _ = read_vint(blob, pos + idl)
            except Exception:
                break
            dpos = pos + idl + sl
            if eidv == eid:
                if seen == occurrence:
                    return dpos
                seen += 1
            pos += 1
        return None

    # locate the signed relative timecode bytes inside the audio Block payload
    aud = data.find(xiph[:4])  # track vint + first 2 of tc (prefix search)
    # robustly: find bytes pattern for track 2 vint 0x82 + tc FF FF
    idx = data.find(bytes([0x82, 0xFF, 0xFF]))
    points["audio_rel_tc"] = {"offset": idx + 1, "bytes": "ffff",
                              "note": "signed -1; mutant -> 0x7FFF overflow"}
    # lacing flags byte sits right after the 2-byte tc
    points["audio_flags"] = {"offset": idx + 3, "byte": "02"}
    # xiph size run begins after lace-count byte
    points["xiph_sizes"] = {"offset": idx + 5}
    points["discard_padding"] = {
        "offset": data.find((-2_000_000).to_bytes(4, "big", signed=True))}
    points["cluster2_timecode"] = {
        "offset": None}
    points["cue1_cluster_pos"] = {"offset": None}
    points["track2_tts"] = {"offset": None}
    points["cluster1_size"] = {"offset": None}

    return BuildResult(data=data, patch_points=points,
                       segment_data_pos=seg_data_pos)


def locate_patch_points(data: bytes, br: BuildResult) -> dict:
    """Compute exact same-length patch locations for every mutant."""
    from . import ebml as EB
    roots = EB.parse_document(data)
    seg = next(r for r in roots if r.id == EB.SEGMENT)
    clusters = [c for c in seg.children if c.id == EB.CLUSTER]
    tracks_el = next(c for c in seg.children if c.id == EB.TRACKS)
    cues_el = next(c for c in seg.children if c.id == EB.CUES)

    pts = {}
    # cluster0 unknown-length: size-vint starts right after its 4-byte id
    c0 = clusters[0]
    pts["cluster0_size"] = c0.pos + 4
    pts["cluster0_unknown"] = c0.size is None
    # cluster1 size descriptor (known length in baseline) for gap mutation
    c1, c2 = clusters[1], clusters[2]
    pts["cluster1_id"] = c1.pos
    pts["cluster2_timecode"] = EB.find_first(c2, EB.TIMESTAMP_CLUSTER).data_pos
    pts["cluster2_timecode_width"] = 2
    pts["cluster2_pos"] = c2.pos

    # audio block relative tc / flags / xiph sizes / discard padding
    bg = next(ch for ch in c1.children if ch.id == EB.BLOCK_GROUP)
    block = EB.find_first(bg, EB.BLOCK)
    dp = block.data_pos
    pts["audio_block"] = block.pos
    pts["audio_rel_tc"] = dp + 1
    pts["audio_flags"] = dp + 3
    pts["xiph_count"] = dp + 4
    pts["xiph_sizes"] = dp + 5
    dpad = EB.find_first(bg, EB.DISCARD_PADDING)
    pts["discard_padding_value"] = dpad.data_pos
    pts["discard_padding_width"] = dpad.data_end - dpad.data_pos

    # track2 TrackTimestampScale float64
    t2 = tracks_el.children[1]
    tts = EB.find_first(t2, EB.TRACK_TIMESTAMP_SCALE)
    pts["track2_tts"] = tts.data_pos

    # cue1 (audio) CueClusterPosition bytes
    cue_points = [cp for cp in cues_el.children if cp.id == EB.CUE_POINT]
    ctp = EB.find_first(cue_points[1], EB.CUE_TRACK_POSITIONS)
    ccp = EB.find_first(ctp, EB.CUE_CLUSTER_POSITION)
    pts["cue1_cluster_pos"] = ccp.data_pos
    pts["cue1_cluster_pos_width"] = ccp.data_end - ccp.data_pos
    return pts


def apply_mutant(data: bytes, name: str, pts: dict) -> tuple[bytes, dict]:
    """Return patched bytes + a description. Patches are same length so every
    EBML offset stays identical."""
    out = bytearray(data)
    frag = {}
    if name == "unknown-length":
        # Flip cluster0's size VINT to an all-ones unknown length (8 bytes),
        # preserving the exact width.
        off = pts["cluster0_size"]
        frag = {"offset": off, "before": data[off:off + 8].hex(),
                "after": (b"\x01\x00\x00\x00\x00\x00\x00\xff").hex()}
        out[off:off + 8] = b"\x01\x00\x00\x00\x00\x00\x00\xff"
    elif name == "relative-timecode-overflow":
        off = pts["audio_rel_tc"]
        frag = {"offset": off, "before": data[off:off + 2].hex(),
                "after": "7fff"}
        out[off:off + 2] = (0x7FFF).to_bytes(2, "big")
    elif name == "lacing-corruption":
        # Overstate the first Xiph frame size beyond payload -> corrupt lace.
        off = pts["xiph_sizes"]
        frag = {"offset": off, "before": data[off:off + 1].hex(),
                "after": "ff"}
        out[off] = 0xFF  # size run now overruns the tiny block
    elif name == "discard-padding":
        off = pts["discard_padding_value"]
        w = pts["discard_padding_width"]
        frag = {"offset": off, "width": w,
                "before": data[off:off + w].hex(),
                "after": (0x7FFFFFFF).to_bytes(w, "big", signed=False).hex()}
        out[off:off + w] = (0x7FFFFFFF).to_bytes(w, "big", signed=False)
    elif name == "cue-mispoint":
        off = pts["cue1_cluster_pos"]
        w = pts["cue1_cluster_pos_width"]
        bad = (0xDEAD).to_bytes(w, "big")
        frag = {"offset": off, "width": w,
                "before": data[off:off + w].hex(), "after": bad.hex()}
        out[off:off + w] = bad
    elif name == "track-timebase-difference":
        import struct
        off = pts["track2_tts"]
        frag = {"offset": off, "before": data[off:off + 8].hex(),
                "after": struct.pack(">d", 0.25).hex()}
        out[off:off + 8] = struct.pack(">d", 0.25)
    elif name == "mid-cluster-gap":
        off = pts["cluster2_timecode"]
        frag = {"offset": off, "before": data[off:off + 2].hex(),
                "after": (900).to_bytes(2, "big").hex()}
        out[off:off + 2] = (900).to_bytes(2, "big")
    else:
        raise KeyError(name)
    return bytes(out), {"name": name, "fragment": frag}
