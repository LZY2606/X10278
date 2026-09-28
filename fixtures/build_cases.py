from __future__ import annotations

import json
from pathlib import Path

from domain import ids
from domain.ebml import el, encode_float, encode_signed, encode_unsigned


ROOT = Path(__file__).resolve().parent / "cases"


def text(value: str) -> bytes:
    return value.encode("utf-8")


def u32(value: int) -> bytes:
    return encode_unsigned(value, width=4)


def ebml_header() -> bytes:
    return el(
        ids.EBML_HEADER,
        [
            el(ids.EBML_VERSION, u32(1)),
            el(ids.EBML_READ_VERSION, u32(1)),
            el(ids.EBML_MAX_ID_LENGTH, u32(4)),
            el(ids.EBML_MAX_SIZE_LENGTH, u32(4)),
            el(ids.DOC_TYPE, text("matroska")),
            el(ids.DOC_TYPE_VERSION, u32(4)),
            el(ids.DOC_TYPE_READ_VERSION, u32(2)),
        ],
    ).raw


def block_payload(track_number: int, relative_ms: int, flags: int, frame: bytes = b"f") -> bytes:
    return bytes([track_number]) + encode_signed(relative_ms, 2) + bytes([flags]) + frame


def xiph_laced_block(track_number: int, relative_ms: int, sizes: list[int], frame_bytes: bytes = b"ABCD", corrupt: bool = False) -> bytes:
    header = bytes([track_number]) + encode_signed(relative_ms, 2) + bytes([0x02 | ((len(sizes) + 1) & 0x07)])
    size_bytes = b"".join(
        bytes([255 if size >= 255 else size]) + (b"" if size < 255 else bytes([size - 255]))
        for size in sizes
    )
    payload = header + size_bytes + frame_bytes
    return payload[:-2] if corrupt else payload


def info(scale: int = 1_000_000) -> "object":
    return el(
        ids.INFO,
        [
            el(ids.TIMECODE_SCALE, encode_unsigned(scale, width=8)),
            el(ids.DURATION, encode_float(3000.0)),
        ],
    )


def tracks(audio_scale: float = 1.0) -> "object":
    return el(
        ids.TRACKS,
        [
            el(
                ids.TRACK_ENTRY,
                [
                    el(ids.TRACK_NUMBER, bytes([1])),
                    el(ids.TRACK_UID, u32(101)),
                    el(ids.TRACK_TYPE, bytes([1])),
                    el(ids.DEFAULT_DURATION, encode_unsigned(40_000_000, width=4)),
                    el(ids.NAME, text("video")),
                    el(ids.CODEC_ID, text("V_TEST")),
                ],
            ),
            el(
                ids.TRACK_ENTRY,
                [
                    el(ids.TRACK_NUMBER, bytes([2])),
                    el(ids.TRACK_UID, u32(202)),
                    el(ids.TRACK_TYPE, bytes([2])),
                    el(ids.DEFAULT_DURATION, encode_unsigned(20_000_000, width=4)),
                    el(ids.TRACK_TIMESTAMP_SCALE, encode_float(audio_scale)),
                    el(ids.NAME, text("audio")),
                    el(ids.CODEC_ID, text("A_TEST")),
                ],
            ),
        ],
    )


def cue_point(cue_time_ms: int, track_number: int, cluster_position: int, relative: int = 0) -> "object":
    return el(
        ids.CUE_POINT,
        [
            el(ids.CUE_TIME, encode_unsigned(cue_time_ms, width=4)),
            el(
                ids.CUE_TRACK_POSITIONS,
                [
                    el(ids.CUE_TRACK, bytes([track_number])),
                    el(ids.CUE_CLUSTER_POSITION, encode_unsigned(cluster_position, width=4)),
                    el(ids.CUE_RELATIVE_POSITION, encode_unsigned(relative, width=4)),
                ],
            ),
        ],
    )


def clusters(lacing: str = "valid", discard_padding: int | None = None, gap: bool = False) -> list["object"]:
    first_block = xiph_laced_block(1, 0, [1, 1, 1])
    if lacing == "damaged":
        first_block = xiph_laced_block(1, 0, [1, 1, 1], corrupt=True)
    c1_children = [el(ids.TIMECODE, u32(0)), el(ids.SIMPLE_BLOCK, first_block)]
    if discard_padding is not None:
        c1_children = [
            el(ids.TIMECODE, u32(0)),
            el(
                ids.BLOCK_GROUP,
                [
                    el(ids.BLOCK, block_payload(2, 0, 0, b"a")),
                    el(ids.DISCARD_PADDING, encode_signed(discard_padding, 4)),
                ],
            ),
        ]
    return [
        el(ids.CLUSTER, c1_children),
        el(
            ids.CLUSTER,
            [
                el(ids.TIMECODE, u32(2000 if gap else 1000)),
                el(ids.SIMPLE_BLOCK, block_payload(1, 0, 0, b"v2")),
                el(ids.SIMPLE_BLOCK, block_payload(2, 10, 0, b"a2")),
            ],
        ),
        el(
            ids.CLUSTER,
            [
                el(ids.TIMECODE, u32(2000)),
                el(ids.SIMPLE_BLOCK, block_payload(1, 0, 0, b"v3")),
            ],
        ),
    ]


def build_segment_payload(
    *,
    unknown_segment: bool = False,
    unknown_cluster: bool = False,
    lacing: str = "valid",
    discard_padding: int | None = None,
    bad_cue: bool = False,
    audio_scale: float = 1.0,
    gap: bool = False,
) -> bytes:
    cs = clusters(lacing=lacing, discard_padding=discard_padding, gap=gap)
    if unknown_cluster:
        cs[0] = el(cs[0].id, cs[0].children, unknown=True, size_width=4)
    cues = el(ids.CUES, [cue_point(0, 1, 555 if bad_cue else 123), cue_point(1000, 1, 456)])
    segment = el(ids.SEGMENT, [info(), tracks(audio_scale), cues, *cs], unknown=unknown_segment, size_width=8 if unknown_segment else None)
    return segment.raw


def build_case(**kwargs) -> bytes:
    return ebml_header() + build_segment_payload(**kwargs)


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    variants = {
        "baseline": {},
        "unknown_length": {"unknown_segment": True, "unknown_cluster": True},
        "relative_overflow": {},
        "lacing_damaged": {"lacing": "damaged"},
        "discard_padding": {"discard_padding": 500_000},
        "cue_mispoint": {"bad_cue": True},
        "track_timebase": {"audio_scale": 2.0},
        "cluster_gap": {"gap": True},
    }
    for name, kwargs in variants.items():
        (ROOT / f"{name}.mkv").write_bytes(build_case(**kwargs))
    overflow = bytearray((ROOT / "baseline.mkv").read_bytes())
    marker = xiph_laced_block(1, 0, [1, 1, 1])[:6]
    position = bytes(overflow).find(marker)
    if position < 0:
        raise RuntimeError("relative timecode marker not found")
    overflow[position + 3 : position + 5] = encode_signed(32767, 2)
    (ROOT / "relative_overflow.mkv").write_bytes(overflow)
    manifest = {
        "case_id": "matroska-timecode-court",
        "title": "Matroska 时码庭：cluster、cue 索引与多轨帧时间证据",
        "fixtures": [
            {"name": name, "path": f"fixtures/cases/{name}.mkv", "direct_source_change": None if name == "baseline" else name}
            for name in variants
        ],
        "rule_version": "rules-2026.09.19",
        "coordinate_bases": {
            "file_time": "Matroska TimecodeScale normalized nanoseconds, origin at Segment timeline zero",
            "track_time": "file_time * TrackTimestampScale; scale is retained in original raw representation",
            "byte_range": "zero-based half-open file offsets",
        },
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
