"""VINT + evidence-preserving parser tests."""
import pytest

from domain import ebml
from domain.ebml import read_vint, encode_vint, parse_document
from domain.fixtures import build_baseline, case_unknown_length


def test_vint_roundtrip_known_values():
    for v in [0, 1, 126, 127, 128, 163, 166, 300, 1 << 20]:
        b = encode_vint(v)
        got = read_vint(b, 0)
        assert got.value == v and got.unknown is False


def test_unknown_size_all_canonical_widths():
    # 1-byte unknown FF; wider unknown encodings carry the width marker.
    special = {2: bytes.fromhex("7fff"), 3: bytes.fromhex("3fffff"),
               4: bytes.fromhex("1fffffff")}
    got = read_vint(bytes.fromhex("ff"), 0)
    assert got.unknown is True and got.length == 1
    for width, b in special.items():
        got = read_vint(b, 0)
        assert got.unknown is True and got.length == width


def test_zero_size_is_not_unknown():
    assert read_vint(encode_vint(0), 0).unknown is False


def test_baseline_preserves_offsets_and_ranges():
    data = build_baseline().data
    doc_roots = parse_document(data)
    seg = next(r for r in doc_roots if r.id == "18538067")
    assert seg.unknown_size is True
    # every element range is within file and start < end
    for r in doc_roots:
        def walk(e):
            assert e.start < e.end or (e.end == e.data_start)
            assert 0 <= e.start <= len(data)
            assert 0 <= e.end <= len(data)
            for c in e.children:
                walk(c)
        walk(r)


def test_unknown_length_cluster_bounded_by_grammar():
    data = case_unknown_length().data
    roots = parse_document(data)
    seg = next(r for r in roots if r.id == "18538067")
    clusters = [c for c in seg.children if c.id == "1f43b675"]
    assert len(clusters) == 2
    assert all(c.unknown_size in (True, False) for c in clusters)
    unknown = [c for c in clusters if c.unknown_size]
    assert unknown and unknown[0].end == clusters[1].start
