"""Single-source counterexample mutations.

Each mutation flips exactly one direct source in the bytes and records the byte
range touched, so the tribunal can check that blast radius stays inside the
dependent subgraph.  All mutations preserve total file length (same-width
rewrites) so stable identities/offsets are unaffected.
"""
from __future__ import annotations

from . import ebml
from . import ids as I


def mutate_unknown_length_segment(data: bytes) -> tuple[bytes, tuple[int, int]]:
    """Rewrite Segment size VINT to the unknown-length marker."""
    # Segment id starts after the EBML header; find by scanning id.
    pos = data.index(ebml.encode_id(I.SEGMENT))
    _, id_len = ebml.read_id(data, pos)
    size_pos = pos + id_len
    _, width, _ = ebml.read_vint(data, size_pos)
    marker = ebml.encode_vint(0, width=width, unknown=True)
    out = data[:size_pos] + marker + data[size_pos + width:]
    return out, (size_pos, size_pos + width)


def mutate_relative_overflow(data: bytes, *, set_rel: int = -1) -> tuple[bytes, tuple[int, int]]:
    """Set the first video SimpleBlock relative timecode to a signed-negative
    value (0xFFFF => -1), exercising R1."""
    from .model import import_bytes
    doc = import_bytes(data)
    target = next(b for b in doc["blocks"]
                  if b["kind"] == "SimpleBlock" and b["track_number"] == 1)
    # block payload starts after element header; rel timecode = track vint +1
    ps = target["payload_range"][0]
    tn, tn_len = ebml.read_vint(data, ps)
    tc_pos = ps + tn_len
    out = data[:tc_pos] + int(set_rel).to_bytes(2, "big", signed=True) + data[tc_pos + 2:]
    return out, (tc_pos, tc_pos + 2)


def mutate_lacing_corrupt(data: bytes) -> tuple[bytes, tuple[int, int]]:
    """Corrupt the Xiph lacing size declaration so declared sizes overflow.

    The block declares 3 frames; bump the first Xiph size to 255 and the
    terminator byte is also 255 (run-on), making sizes exceed payload => no
    frame boundaries may be guessed (R2).
    """
    from .model import import_bytes
    doc = import_bytes(data)
    target = next(b for b in doc["blocks"] if b["lacing"] == "xiph")
    ps = target["payload_range"][0]
    # skip track vint(1) + rel tc(2) + flags(1), then lacing-count byte
    pos = ps + 4 + 1
    out = bytearray(data)
    # size 12 -> write 0xff run + 0xff terminator => 510 bytes, exceeds payload
    out[pos] = 0xFF
    out[pos + 1] = 0xFF
    return bytes(out), (pos, pos + 2)


def mutate_discard_padding(data: bytes, *, new_ns: int = 60_000_000) -> tuple[bytes, tuple[int, int]]:
    """Replace DiscardPadding with a value larger than the block duration."""
    from .model import import_bytes
    doc = import_bytes(data)
    pos = data.index(ebml.encode_id(I.DISCARD_PADDING))
    _, id_len = ebml.read_id(data, pos)
    size_pos = pos + id_len
    _, width, _ = ebml.read_vint(data, size_pos)
    val_pos = size_pos + width
    raw = int(new_ns).to_bytes(width, "big", signed=True)
    out = data[:val_pos] + raw + data[val_pos + width:]
    return out, (val_pos, val_pos + width)


def mutate_cue_mispoint(data: bytes, *, delta: int = 2) -> tuple[bytes, tuple[int, int]]:
    """Shift the second CueClusterPosition by ``delta`` bytes (same width)."""
    from .model import import_bytes
    doc = import_bytes(data)
    cue = doc["cues"][1]
    pos = data.index(ebml.encode_id(I.CUE_CLUSTER_POS))
    # second occurrence
    pos = data.index(ebml.encode_id(I.CUE_CLUSTER_POS), pos + 1)
    _, id_len = ebml.read_id(data, pos)
    size_pos = pos + id_len
    _, width, _ = ebml.read_vint(data, size_pos)
    val_pos = size_pos + width
    old = int.from_bytes(data[val_pos:val_pos + width], "big")
    raw = int(old + delta).to_bytes(width, "big")
    out = data[:val_pos] + raw + data[val_pos + width:]
    return out, (val_pos, val_pos + width)


def mutate_track_timebase(data: bytes, *, new_scale: int = 2_000_000) -> tuple[bytes, tuple[int, int]]:
    """Rewrite TimecodeScale in place (4-byte fixed width baseline)."""
    pos = data.index(ebml.encode_id(I.TIMECODE_SCALE))
    _, id_len = ebml.read_id(data, pos)
    size_pos = pos + id_len
    _, width, _ = ebml.read_vint(data, size_pos)
    val_pos = size_pos + width
    raw = int(new_scale).to_bytes(width, "big")
    out = data[:val_pos] + raw + data[val_pos + width:]
    return out, (val_pos, val_pos + width)


def mutate_cluster_gap(data: bytes, *, shift: int = 600) -> tuple[bytes, tuple[int, int]]:
    """Shift the third cluster's ClusterTimecode forward to open a gap."""
    from .model import import_bytes
    doc = import_bytes(data)
    c = doc["clusters"][2]
    ps = c["payload_range"][0]  # ClusterTimecode is first child
    _, id_len = ebml.read_id(data, ps)
    size_pos = ps + id_len
    _, width, _ = ebml.read_vint(data, size_pos)
    val_pos = size_pos + width
    raw = int(c["timecode_raw"] + shift).to_bytes(width, "big")
    out = data[:val_pos] + raw + data[val_pos + width:]
    return out, (val_pos, val_pos + width)


def mutate_audio_block_local(data: bytes) -> tuple[bytes, tuple[int, int]]:
    """Change one audio payload byte: only that block's subtree may change."""
    from .model import import_bytes
    doc = import_bytes(data)
    target = next(b for b in doc["blocks"]
                  if b["kind"] == "SimpleBlock" and b["track_number"] == 2)
    pos = target["payload_range"][1] - 1
    out = bytearray(data)
    out[pos] ^= 0x01
    return bytes(out), (pos, pos + 1)
