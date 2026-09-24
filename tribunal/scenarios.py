"""The seven boundary scenarios.

Branches 2-6 patch one direct source region in the baseline bytes
(offsets are byte-identical everywhere outside the fragment); branches
1 and 7 rebuild one structural source.  Each scenario keeps an
``input_fragment`` describing exactly what changed.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import ebml, casebuild
from .casebuild import (assemble, cluster, simple_block, block_group,
                        cue_point)

# ---- baseline structure: 4 clusters @ t=0,200,400,700 ticks -----------
LACED_PAYLOAD = casebuild.xiph_payload(
    [10, 12, 8], [b"V00", b"V01xx", b"V02"])

BG1 = block_group(1, 0, True, b"KEY0", duration=40)
SB2 = simple_block(1, 200, False, LACED_PAYLOAD)
BG3 = block_group(1, 400, False, b"VID0003", duration=40,
                  discard_padding=250_000, references=[-400])
AB3 = simple_block(2, 410, True, b"AUD0000")
SB4V = simple_block(1, 700, False, b"VID0007")
SB4A = simple_block(2, 720, False, b"AUD0007")

ALL_CLUSTERS = [cluster(0, [BG1]),
                cluster(200, [SB2]),
                cluster(400, [BG3, AB3]),
                cluster(700, [SB4V, SB4A])]
GAP_CLUSTERS = [ALL_CLUSTERS[0], ALL_CLUSTERS[1], ALL_CLUSTERS[3]]


@dataclass
class BuiltCase:
    data: bytes
    cluster_offsets: list[int]
    cue_specs: list[tuple[int, int, int]]   # (time, track, cluster_index)


def _assemble(cluster_objs, *, unknown_segment=False, audio_scale=None,
             default_duration=40_000_000) -> BuiltCase:
    cluster_bytes = [casebuild.serialize(c) for c in cluster_objs]
    offsets: list[int] = []
    cursor = 0
    for blob in cluster_bytes:
        offsets.append(cursor)
        cursor += len(blob)
    # Cue relative position -> first block element of cluster 0
    rel0 = ALL_CLUSTERS[0].header_len + ALL_CLUSTERS[0].get_child(
        "Timecode").header_len
    cue_specs = [(0, 1, 0), (200, 1, 1), (410, 2, 2)]
    points = [
        cue_point(0, 1, offsets[0], relative=rel0),
        cue_point(200, 1, offsets[1]),
        cue_point(410, 2, offsets[2]),
    ]
    data = assemble(
        casebuild.BASELINE_SCALE_NS, cluster_bytes,
        [casebuild.serialize(p) for p in points],
        unknown_segment_width=4 if unknown_segment else None,
        audio_track_scale=audio_scale,
        video_default_duration=default_duration)
    return BuiltCase(data, offsets, cue_specs)


def build_baseline() -> BuiltCase:
    return _assemble(ALL_CLUSTERS)


def build_unknown_segment() -> BuiltCase:
    return _assemble(ALL_CLUSTERS, unknown_segment=True)


def build_track_timebase() -> BuiltCase:
    return _assemble(ALL_CLUSTERS, audio_scale=0.5)


def build_cluster_gap() -> BuiltCase:
    return _assemble(GAP_CLUSTERS)
    # note: 3 clusters -> offsets length 3; cue spec index 2 remapped below


def _cluster_offset(data: bytes, cluster_data_offset: int) -> int:
    """Absolute id offset of a cluster given its segment-relative offset."""
    roots = ebml.parse_document(data)
    seg = next(e for e in roots if e.name == "Segment")
    return seg.data_offset + cluster_data_offset


def patch_bytes(data: bytes, patches: list[tuple[int, bytes, bytes]],
               label: str) -> bytes:
    out = bytearray(data)
    for offset, old, new in patches:
        assert bytes(out[offset:offset + len(old)]) == old, (
            f"{label}: expected bytes at {offset}")
        out[offset:offset + len(old)] = new
    return bytes(out)


def _find_block(data: bytes, cluster_index: int, kind: str,
                occurrence: int = 0):
    """Return (absolute id_offset, element) for a block in a cluster."""
    roots = ebml.parse_document(data)
    seg = next(e for e in roots if e.name == "Segment")
    kids = ebml.parse_segment_children(seg, data)
    clusters = [e for e in kids if e.name == "Cluster"]
    target = clusters[cluster_index]
    groups = [c for c in target.children
              if c.name in (kind, "BlockGroup")]
    if kind == "Block":
        group = groups[occurrence]
        return group.get_child("Block").id_offset, group.get_child("Block")
    return groups[occurrence].id_offset, groups[occurrence]


def build_rel_overflow(base: BuiltCase) -> tuple[bytes, dict]:
    """Audio block in cluster 2: rel 0x019A (+410) -> 0x84D0 (-31536)."""
    off, el = _find_block(base.data, 2, "SimpleBlock", occurrence=1)
    payload_start = el.data_offset
    old = (410).to_bytes(2, "big", signed=True)
    new = (-31536).to_bytes(2, "big", signed=True)
    frag = {"offset": payload_start + 1, "old_hex": old.hex(),
            "new_hex": new.hex(), "length": 2}
    return patch_bytes(base.data,
                       [(payload_start + 1, old, new)],
                       "rel-overflow"), frag


def build_lacing_damage(base: BuiltCase) -> tuple[bytes, dict]:
    """Xiph lacing header claims 7 extra frames but only 2 exist."""
    off, el = _find_block(base.data, 1, "SimpleBlock")
    # block payload begins after track vint(1) + rel(2) + flags(1)
    lacing_off = el.data_offset + 1 + 2 + 1
    old = bytes([0x02])
    new = bytes([0x07])
    frag = {"offset": lacing_off, "old_hex": old.hex(),
            "new_hex": new.hex(), "length": 1}
    return patch_bytes(base.data, [(lacing_off, old, new)],
                       "lacing"), frag


def build_discard_change(base: BuiltCase) -> tuple[bytes, dict]:
    """DiscardPadding 250,000ns -> 1,000,000ns (same byte width)."""
    roots = ebml.parse_document(base.data)
    seg = next(e for e in roots if e.name == "Segment")
    kids = ebml.parse_segment_children(seg, base.data)
    target_el = None
    for c in (e for e in kids if e.name == "Cluster"):
        for g in c.find_all("DiscardPadding"):
            target_el = g
    assert target_el is not None
    old = (250_000).to_bytes(3, "big", signed=True)
    new = (1_000_000).to_bytes(3, "big", signed=True)
    assert target_el.raw == old
    frag = {"offset": target_el.data_offset, "old_hex": old.hex(),
            "new_hex": new.hex(), "length": 3}
    return patch_bytes(base.data,
                       [(target_el.data_offset, old, new)],
                       "discard"), frag


def build_cue_mispoint() -> BuiltCase:
    """Rebuild with cue 3 pointing at cluster 3 (t=700) for track 2."""
    built = _assemble(ALL_CLUSTERS)
    offsets = built.cluster_offsets
    points = [
        cue_point(0, 1, offsets[0], relative=rel0()),
        cue_point(200, 1, offsets[1]),
        cue_point(720, 2, offsets[3]),
    ]
    data = assemble(
        casebuild.BASELINE_SCALE_NS,
        [casebuild.serialize(c) for c in ALL_CLUSTERS],
        [casebuild.serialize(p) for p in points])
    return BuiltCase(data, offsets, built.cue_specs)


def rel0() -> int:
    return (ALL_CLUSTERS[0].header_len
            + ALL_CLUSTERS[0].get_child("Timecode").header_len)


SCENARIOS = [
    ("unknown_length", "未知长度", "container-rebuild",
     "R-UNKNOWN-LENGTH"),
    ("rel_overflow", "relative timecode 溢出", "byte-patch",
     "R-SIGNED-16"),
    ("lacing_corrupt", "lacing 损坏", "byte-patch",
     "R-LACING-NO-GUESS"),
    ("discard_padding", "discard padding 变化", "byte-patch",
     "R-UNIT-RAW"),
    ("cue_mispoint", "cue 错指", "container-rebuild",
     "R-CUE-EVIDENCE"),
    ("track_timebase", "轨道时基差异", "container-rebuild",
     "R-MULTITRACK-NO-FILE-ORDER"),
    ("cluster_gap", "中途 cluster 缺口", "container-rebuild",
     "R-MULTITRACK-NO-FILE-ORDER"),
]
