"""Matroska document model built on top of the raw EBML tree.

Raw representations are kept verbatim (``raw`` hex strings); any
normalisation happens in the derived layer only.  Values that cannot
be converted stay in place and carry a diagnostic.
"""
from __future__ import annotations

from . import ebml
from .blocks import parse_block

DEFAULT_TIMECODE_SCALE = 1_000_000  # ns per timecode unit, Matroska default


def _uint(data: bytes, el: ebml.Element):
    raw = ebml.element_bytes(data, el)
    value = int.from_bytes(raw, "big") if raw else 0
    return {"value": value, "unit": "count", "raw": raw.hex(), "element": el.uid}


def _int(data: bytes, el: ebml.Element):
    raw = ebml.element_bytes(data, el)
    value = int.from_bytes(raw, "big", signed=True) if raw else 0
    return {"value": value, "unit": "count", "raw": raw.hex(), "element": el.uid}


def _text(data: bytes, el: ebml.Element):
    raw = ebml.element_bytes(data, el)
    try:
        value = raw.decode("utf-8", "replace")
    except Exception:  # pragma: no cover
        value = ""
    return {"value": value, "unit": "text", "raw": raw.hex(), "element": el.uid}


def _child(el, id_value):
    for c in el.children:
        if c.id == id_value:
            return c
    return None


def build_document(data: bytes, roots, parse_diagnostics) -> dict:
    """Extract Segment/Info/Tracks/Clusters/Cues with raw bytes kept."""
    doc = {
        "size_bytes": {"value": len(data), "unit": "bytes"},
        "diagnostics": list(parse_diagnostics),
        "timecode_scale": {"value": DEFAULT_TIMECODE_SCALE, "unit": "ns/timecode",
                           "raw": None, "element": None,
                           "note": "Matroska default, no TimecodeScale element"},
        "tracks": [],
        "clusters": [],
        "cues": [],
        "elements": [],
    }
    for el in ebml.iter_elements(roots):
        doc["elements"].append({
            "uid": el.uid,
            "id": f"0x{el.id:x}",
            "name": el.name,
            "offset": {"value": el.offset, "unit": "bytes"},
            "header_size": {"value": el.header_size, "unit": "bytes"},
            "data_size": ({"value": el.data_size, "unit": "bytes"}
                          if el.data_size is not None else
                          {"value": None, "unit": "bytes",
                           "diagnostic": "unknown_length"}),
            "depth": el.depth,
            "unknown_size": el.data_size is None,
            "truncated": el.truncated,
        })
    segment = None
    for el in roots:
        if el.id == 0x18538067:
            segment = el
    if segment is None:
        doc["diagnostics"].append({"element": None, "kind": "no_segment",
                                   "detail": "no Segment element found"})
        return doc
    seg_data_offset = segment.data_offset
    for top in segment.children:
        if top.id == 0x1549A966:  # Info
            ts = _child(top, 0x2AD7B1)
            if ts is not None:
                q = _uint(data, ts)
                q["unit"] = "ns/timecode"
                doc["timecode_scale"] = q
        elif top.id == 0x1654AE6B:  # Tracks
            for te in top.children:
                if te.id == 0xAE:
                    doc["tracks"].append(_track_entry(data, te))
        elif top.id == 0x1F43B675:  # Cluster
            doc["clusters"].append(_cluster(data, top, seg_data_offset))
        elif top.id == 0x1C53BB6B:  # Cues
            for cp in top.children:
                if cp.id == 0xBB:
                    doc["cues"].append(_cue_point(data, cp, seg_data_offset))
    return doc


def _track_entry(data: bytes, te) -> dict:
    track = {"element": te.uid, "raw_range": list(te.raw_range())}
    for c in te.children:
        if c.id == 0xD7:
            track["number"] = _uint(data, c)
        elif c.id == 0x73C5:
            track["uid"] = _uint(data, c)
        elif c.id == 0x83:
            track["type"] = _uint(data, c)
        elif c.id == 0x536E:
            track["name"] = _text(data, c)
        elif c.id == 0x86:
            track["codec"] = _text(data, c)
        elif c.id == 0x23E383:
            q = _uint(data, c)
            q["unit"] = "ns"
            track["default_duration"] = q
    return track


def _cluster(data: bytes, cl, seg_data_offset: int) -> dict:
    cluster = {
        "element": cl.uid,
        "position": {"value": cl.offset - seg_data_offset, "unit": "bytes",
                     "base": "segment_data"},
        "byte_range": {"start": {"value": cl.offset, "unit": "bytes"},
                       "end": {"value": cl.end, "unit": "bytes"}},
        "unknown_size": cl.data_size is None,
        "timecode": None,
        "blocks": [],
    }
    for c in cl.children:
        if c.id == 0xE7:
            q = _uint(data, c)
            q["unit"] = "timecode"
            cluster["timecode"] = q
        elif c.id in (0xA3, 0xA1):  # SimpleBlock / bare Block
            cluster["blocks"].append(_block(data, c, cluster, None))
        elif c.id == 0xA0:  # BlockGroup
            group = {"element": c.uid, "discard_padding": None,
                     "block_duration": None}
            block_el = None
            for g in c.children:
                if g.id == 0xA1:
                    block_el = g
                elif g.id == 0x75A2:
                    q = _int(data, g)
                    q["unit"] = "ns"
                    group["discard_padding"] = q
                elif g.id == 0x9B:
                    q = _uint(data, g)
                    q["unit"] = "timecode"
                    group["block_duration"] = q
            if block_el is not None:
                cluster["blocks"].append(_block(data, block_el, cluster, group))
    return cluster


def _block(data: bytes, el, cluster: dict, group) -> dict:
    payload = ebml.element_bytes(data, el)
    rec = {
        "element": el.uid,
        "kind": "SimpleBlock" if el.id == 0xA3 else "Block",
        "byte_range": {"start": {"value": el.offset, "unit": "bytes"},
                       "end": {"value": el.end, "unit": "bytes"}},
        "raw": payload.hex(),
        "group": group,
    }
    try:
        blk = parse_block(payload)
    except ebml.EbmlError as exc:
        rec["parse_error"] = str(exc)
        rec["diagnostics"] = [{
            "kind": "block_header_corrupt",
            "rule": "block_header_corrupt_no_frame_guess",
            "detail": f"block header unreadable: {exc}; payload kept opaque",
        }]
        return rec
    rec["track_number"] = {"value": blk.track_number, "unit": "count"}
    rel_start = blk.header_len - 3
    rec["rel_timecode"] = {
        "value": blk.rel_timecode, "unit": "timecode",
        "raw": payload[rel_start:rel_start + 2].hex() if rel_start >= 0 else None,
        "interpretation": "int16-be-signed",
        "bit_width": 16,
    }
    rec["flags"] = {"value": blk.flags, "unit": "bitfield",
                    "keyframe": blk.keyframe, "invisible": blk.invisible,
                    "discardable": blk.discardable}
    rec["lacing"] = blk.lacing_mode
    rec["frames"] = [{"offset": {"value": o, "unit": "bytes"},
                      "size": {"value": s, "unit": "bytes"}}
                     for o, s in blk.frames]
    rec["corrupted"] = blk.corrupted
    rec["diagnostics"] = blk.diagnostics
    return rec


def _cue_point(data: bytes, cp, seg_data_offset: int) -> dict:
    cue = {"element": cp.uid, "positions": []}
    for c in cp.children:
        if c.id == 0xB7:
            q = _uint(data, c)
            q["unit"] = "timecode"
            cue["time"] = q
        elif c.id == 0xB3:
            pos = {"element": c.uid}
            for p in c.children:
                if p.id == 0xF7:
                    pos["track"] = _uint(data, p)
                elif p.id == 0xF1:
                    q = _uint(data, p)
                    pos["cluster_position"] = {
                        "value": q["value"], "unit": "bytes",
                        "base": "segment_data", "raw": q["raw"],
                        "element": q["element"]}
            cue["positions"].append(pos)
    return cue
