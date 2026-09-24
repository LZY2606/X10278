"""Generate ``cases/matroska-timecode-court``.

The baseline file is a real (if tiny) Matroska container.  Mutation variants
are produced by patching the baseline bytes at coordinates recorded here, so
every 'counter example' is traceable to a single direct source change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mke import catalog as C
from mke import writer
from mke.parser import Element, flatten, parse_document


CASE_DIR = Path(__file__).resolve().parents[1] / "cases" / "matroska-timecode-court"


def _find(elements: list[Element], element_id: int, nth: int = 0) -> Element:
    matches = [e for e in flatten(elements) if e.element_id == element_id]
    return matches[nth]


def _find_all(elements: list[Element], element_id: int) -> list[Element]:
    return [e for e in flatten(elements) if e.element_id == element_id]


def _child(elements: list[Element], element_id: int, nth: int = 0) -> Element:
    matches = [e for e in elements if e.element_id == element_id]
    return matches[nth]


def build_baseline() -> tuple[bytes, dict]:
    # Frames: video keyframes at cluster +0; audio laced (xiph) pairs.
    v0 = writer.simple_video_block(0, b"VID00000")
    a0 = writer.audio_block_group(
        0, [20, 20, 20], b"A" * 60, discard_padding_ns=20_000_000
    )
    v1 = writer.simple_video_block(0, b"VID11111")
    a1 = writer.audio_block_group(50, [20, 20], b"A" * 40)
    v2 = writer.simple_video_block(0, b"VID22222")
    a2 = writer.audio_block_group(120, [20, 20], b"A" * 40)

    c0 = writer.build_cluster(0, [v0, a0])
    c1 = writer.build_cluster(1000, [v1, a1])
    c2 = writer.build_cluster(2000, [v2, a2])

    header = writer.build_ebml_header()
    info = writer.build_info(1_000_000)
    tracks = writer.build_tracks()

    # Segment-relative position of first cluster.
    seg_payload_prefix = len(info) + len(tracks)
    # First, lay out without Cues to learn where clusters live.
    segment_payload_no_cues = info + tracks + c0 + c1 + c2
    cluster_positions = []
    running = 0
    for cluster in (c0, c1, c2):
        cluster_positions.append(seg_payload_prefix + running)
        running += len(cluster)

    cue_points = b"".join([
        writer.cue_point(0, 1, cluster_positions[0], 0),
        writer.cue_point(0, 2, cluster_positions[0], 0),
        writer.cue_point(1000, 1, cluster_positions[1], 0),
        writer.cue_point(2000, 1, cluster_positions[2], 0),
    ])
    cues = writer.build_cues(cue_points)
    payload = info + tracks + c0 + c1 + c2 + cues
    segment = writer.build_segment(payload)
    data = header + segment

    # Parse the freshly built file to record absolute coordinates.
    doc = parse_document(data)
    seg_elem = _find(doc, C.SEGMENT)
    clusters = _find_all(doc, C.CLUSTER)
    blocks = _find_all(doc, C.BLOCK)
    simple_blocks = _find_all(doc, C.SIMPLE_BLOCK)
    cue_points_elems = _find_all(doc, C.CUE_POINT)
    discard = _find(doc, C.DISCARD_PADDING)
    tc_scale = _find(doc, C.TIMESTAMP_SCALE)

    # The laced audio block of cluster 0 is the first Block element.
    first_audio_block = blocks[0]
    landmarks = {
        "segment_size_pos": seg_elem.size_pos,
        "segment_data_start": seg_elem.data_start,
        "timecode_scale": {"size_pos": tc_scale.size_pos,
                           "data_start": tc_scale.data_start,
                           "data_end": tc_scale.data_end},
        "clusters": [
            {
                "timecode": _find(e.children, C.CLUSTER_TIMESTAMP).value,
                "start": e.start,
                "data_start": e.data_start,
                "data_end": e.data_end,
                "size_pos": e.size_pos,
            }
            for e in clusters
        ],
        "first_audio_block": {
            "start": first_audio_block.start,
            "size_pos": first_audio_block.size_pos,
            "data_start": first_audio_block.data_start,
            "data_end": first_audio_block.data_end,
        },
        "discard_padding": {
            "data_start": discard.data_start,
            "data_end": discard.data_end,
        },
        "first_cue_cluster_position": {
            "data_start": _find(cue_points_elems[0].children, C.CUE_CLUSTER_POSITION).data_start,
            "data_end": _find(cue_points_elems[0].children, C.CUE_CLUSTER_POSITION).data_end,
            "size_pos": _find(cue_points_elems[0].children, C.CUE_CLUSTER_POSITION).size_pos,
        },
        "third_cluster_timecode_elem": {
            "data_start": _child(clusters[2].children, C.CLUSTER_TIMESTAMP).data_start,
            "data_end": _child(clusters[2].children, C.CLUSTER_TIMESTAMP).data_end,
            "size_pos": _child(clusters[2].children, C.CLUSTER_TIMESTAMP).size_pos,
        },
        "simple_blocks": [{"data_start": b.data_start} for b in simple_blocks],
    }
    return data, landmarks


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


MUTATIONS = {
    "unknown_length": "segment size bytes rewritten to all-ones unknown length",
    "relative_tc_overflow": "video SimpleBlock relative timecode set to 0x7FFF raw",
    "lacing_corrupt": "first audio lace size table forced beyond payload",
    "discard_padding": "DiscardPadding rewritten to a large negative value",
    "cue_mispoint": "first CueClusterPosition points into cluster payload",
    "track_timebase_divergence": "source annotation: audio track scale differs (parameter decision)",
    "cluster_gap": "cluster 3 timecode jumps from 2000 to 9000 ticks",
}


def make_variant(name: str, data: bytes, landmarks: dict) -> bytes:
    buf = bytearray(data)
    if name == "unknown_length":
        sp = landmarks["segment_size_pos"]
        buf[sp:sp + 4] = b"\x1f\xff\xff\xff"
    elif name == "relative_tc_overflow":
        sb = landmarks["simple_blocks"][1]
        # track(1) + tc(2); second video block is at cluster 1.  Raw 0xFFFF
        # is -1 when read as the signed 16-bit field, but +65535 under the
        # unsigned misreading that the court must reject.
        buf[sb["data_start"] + 1: sb["data_start"] + 3] = b"\xff\xff"
    elif name == "lacing_corrupt":
        ab = landmarks["first_audio_block"]
        # The xiph table begins at block payload start; set first size to 255
        # markers that consume more than the payload.
        pos = ab["data_start"] + 4  # track(1) + tc(2) + flags(1)
        buf[pos:pos + 3] = b"\xff\xff\xfe"
    elif name == "discard_padding":
        dp = landmarks["discard_padding"]
        size = dp["data_end"] - dp["data_start"]
        buf[dp["data_start"]:dp["data_end"]] = (-(1 << (8 * size - 1))).to_bytes(size, "big", signed=True)
    elif name == "cue_mispoint":
        cp = landmarks["first_cue_cluster_position"]
        bad = int.from_bytes(buf[cp["data_start"]:cp["data_end"]], "big") + 7
        buf[cp["data_start"]:cp["data_end"]] = bad.to_bytes(cp["data_end"] - cp["data_start"], "big")
    elif name == "track_timebase_divergence":
        # No byte change; this variant is exercised via parameter decision.
        pass
    elif name == "cluster_gap":
        tc = landmarks["third_cluster_timecode_elem"]
        raw = int.from_bytes(buf[tc["data_start"]:tc["data_end"]], "big")
        buf[tc["data_start"]:tc["data_end"]] = (9000).to_bytes(tc["data_end"] - tc["data_start"], "big")
    else:
        raise KeyError(name)
    return bytes(buf)


def main() -> None:
    CASE_DIR.mkdir(parents=True, exist_ok=True)
    data, landmarks = build_baseline()
    (CASE_DIR / "baseline.mkv").write_bytes(data)
    manifest = {
        "name": "Matroska 时码庭：cluster、cue 索引与多轨帧时间证据",
        "doc_type": "matroska",
        "baseline_sha256": _sha(data),
        "baseline_size": len(data),
        "timecode_scale_ns": 1_000_000,
        "units": {
            "timecode_scale": "ns/tick",
            "cluster_timecode": "tick",
            "relative_timecode": "signed tick @2-byte field width",
            "cue_time": "tick (segment scale)",
            "discard_padding": "ns",
            "default_duration": "ns",
            "byte_range": "absolute file byte offset",
        },
        "landmarks": landmarks,
        "narrative_steps": [
            "import baseline and verify every raw coordinate is retained",
            "revise TimecodeScale parameter (local, affects time projections)",
            "mutate one direct source and inspect damage containment",
            "two reviewers make conflicting decisions on the same version",
        ],
        "mutations": MUTATIONS,
    }
    (CASE_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    variants = {}
    for name in MUTATIONS:
        vdata = make_variant(name, data, landmarks)
        (CASE_DIR / f"{name}.mkv").write_bytes(vdata)
        variants[name] = _sha(vdata)
    (CASE_DIR / "variants.json").write_text(
        json.dumps(variants, indent=2), encoding="utf-8"
    )
    print(json.dumps({"baseline": len(data), "variants": list(variants)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
