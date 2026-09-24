"""Domain rule tests: signed timecode, lacing, cues, multi-track, units."""
import os

from domain import fixtures
from domain.blocks import decode_block, _interpret_signed_width
from domain.builder import block_payload
from domain.extract import extract_bytes
from service import Service
import tempfile


def _service_with(case):
    s = Service(tempfile.mktemp(suffix=".db"))
    s.import_bytes(case.data, case.name + ".mkv")
    return s


def test_relative_timecode_signed_width():
    signed, overflow = _interpret_signed_width(0xFFFF, 16)
    assert signed == -1
    assert _interpret_signed_width(0x8000, 16)[0] == -32768
    assert _interpret_signed_width(0x7FFF, 16)[0] == 32767
    # raw 0xFFFF must appear as raw uint AND signed -1 on the block
    s = _service_with(fixtures.case_tc_overflow())
    bts = [b for cl in s.state()["document"]["clusters"] for b in cl["blocks"]]
    extreme = next(b for b in bts if b["rel_tc_raw"] == 0xFFFF)
    assert extreme["rel_tc_signed"] == -1
    assert extreme["tc_width_bits"] == 16
    node = next(n for n in s.state()["nodes"]
                if n["kind"] == "block_time" and n["value"]["overflow"])
    assert node["status"] == "uncertain"
    assert node["value"]["rel_tc_raw"] == 0xFFFF
    assert node["value"]["rel_tc_signed"] == -1


def test_lacing_corrupt_does_not_guess_boundaries():
    s = _service_with(fixtures.case_lacing_corrupt())
    bad = next(b for cl in s.state()["document"]["clusters"]
               for b in cl["blocks"] if b["corrupt"])
    # a single untrusted whole-span frame, not fabricated split
    assert len(bad["frames"]) == 1
    assert bad["frames"][0]["trusted"] is False
    assert any("not guessed" in d or "not trusted" in d for d in bad["diagnostics"])
    node = next(n for n in s.state()["nodes"] if n["kind"] == "frame_time"
                and n["status"] == "uncertain")
    assert node["candidates"] and node["value"]["frame_times"] == []
    assert node["candidates"][0]["trusted"] is False


def test_valid_xiph_and_fixed_lacing_splits_frames():
    frames = [b"abc", b"defg", b"hi"]
    payload = block_payload(1, 10, 0x80, frames, lacing=1)
    info = decode_block(payload)
    assert not info.corrupt and info.lacing == 1
    assert [payload[f.start:f.end] for f in info.frames] == frames
    fixed = block_payload(1, 0, 0x80, [b"xx", b"xx", b"xx"], lacing=2)
    info2 = decode_block(fixed)
    assert not info2.corrupt and len(info2.frames) == 3


def test_cue_is_evidence_not_truth():
    s = _service_with(fixtures.case_cue_mispoint())
    checks = [n for n in s.state()["nodes"] if n["kind"] == "cue_check"]
    bad = next(c for c in checks if not c["value"]["position_match"])
    assert bad["value"]["evidence_only"] is True
    assert bad["status"] == "uncertain"
    # actual cluster coordinate retained as a legal alternative candidate
    assert any(c["label"] == "actual-cluster" for c in bad["candidates"])


def test_tracks_have_independent_time_bases():
    s = _service_with(fixtures.case_track_timebase())
    tls = {n["value"]["track"]: n["value"]["points"]
            for n in s.state()["nodes"] if n["kind"] == "track_timeline"}
    # audio block rel -20 in cluster tc40 with scale 0.5 -> 10ms
    audio_starts = [p["start_ns"] for p in tls[2]]
    assert 10_000_000 in audio_starts
    video_starts = [p["start_ns"] for p in tls[1]]
    assert 40_000_000 in video_starts
    for n in s.state()["nodes"]:
        if n["kind"] == "track_timeline":
            assert "not file-order aligned" in n["value"]["alignment"]


def test_baseline_has_no_gap_gap_case_does():
    base = _service_with(fixtures.build_baseline())
    assert not [n for n in base.state()["nodes"] if n["kind"] == "gap"]
    gapped = _service_with(fixtures.case_cluster_gap())
    gaps = [n for n in gapped.state()["nodes"] if n["kind"] == "gap"]
    assert gaps and all(n["value"]["gap_ns"] > 0 for n in gaps)


def test_discard_padding_preserved_with_unit():
    s = _service_with(fixtures.build_baseline())
    dp = [n for n in s.state()["nodes"] if n["kind"] == "discard_padding"]
    assert dp and dp[0]["value"]["raw"] == 5_000_000
    assert "nanoseconds" in dp[0]["value"]["unit"]


def test_quantities_carry_units_and_raw_representation():
    s = _service_with(fixtures.build_baseline())
    bt = next(n for n in s.state()["nodes"] if n["kind"] == "block_time")
    assert bt["value"]["unit"] == "ns"
    assert "TimecodeScale" in bt["value"]["basis"]



def test_evidence_fields_are_preserved():
    case = fixtures.build_baseline()
    doc = extract_bytes("fid", case.data)
    assert doc.timecode_scale_present is True
    assert len(doc.tracks) == 2 and len(doc.clusters) == 2 and len(doc.cues) == 2
    assert any(u["element"] == "Segment" for u in doc.unknown_lengths)
    for cl in doc.clusters:
        assert cl["raw_range"] == [cl["start"], cl["end"]]
        assert isinstance(cl["segment_relative_pos"], int)
