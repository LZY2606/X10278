"""Matroska/EBML parser producing the raw, fully-preserved object model.

Design rules enforced here (they are the case's standing semantics):

* every element keeps its absolute offset, header size and byte range;
* unknown-size elements are recorded as such, never silently "fixed";
* block relative timecodes are interpreted as *signed* 16-bit big-endian;
* corrupted lacing never leads to guessed frame boundaries -- frames become
  "indeterminate" and the raw lace bytes stay attached;
* unparsable regions become explicit gap objects with their byte range.
"""
from __future__ import annotations

import base64
import hashlib
import struct

from .ebml import (DEFAULT_TIMECODE_SCALE_NS, ELEMENTS, MASTER_IDS,
                   TOP_LEVEL_IDS, EBMLFormatError, read_vint)

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


def _find_next_known_id(data: bytes, pos: int, end: int,
                        candidates: tuple) -> int:
    """Re-sync point: next occurrence of a plausible element id.

    Only the caller-supplied candidate ids are considered -- single-byte ids
    like Void (0xEC) appear in random junk far too often to be trusted.
    """
    best = end
    for id_hex in candidates:
        idx = data.find(bytes.fromhex(id_hex), pos, end)
        if idx != -1:
            best = min(best, idx)
    return best


def _decode_leaf(typ: str, payload: bytes):
    if typ == "uint":
        return int.from_bytes(payload, "big")
    if typ == "int":
        return int.from_bytes(payload, "big", signed=True)
    if typ == "float":
        if len(payload) == 4:
            return struct.unpack(">f", payload)[0]
        if len(payload) == 8:
            return struct.unpack(">d", payload)[0]
        return None
    if typ == "string":
        return payload.decode("ascii", errors="replace")
    return None


class _Ctx:
    def __init__(self, data: bytes):
        self.data = data
        self.elements = []       # flat element records
        self.gaps = []           # (start, end)
        self.diagnostics = []    # parse-level diagnostics
        self._diag_seq = 0

    def diag(self, kind: str, **kw):
        self._diag_seq += 1
        self.diagnostics.append({"seq": self._diag_seq, "kind": kind, **kw})


def _parse_children(ctx: _Ctx, start: int, end: int, parent_el_id: str | None,
                    depth: int, stop_ids: tuple = (),
                    resync_ids: tuple = TOP_LEVEL_IDS) -> int:
    data = ctx.data
    pos = start
    while pos < end:
        try:
            id_val, id_len = read_vint(data, pos, keep_marker=True)
            id_hex = f"{id_val:0{id_len * 2}X}"
        except EBMLFormatError:
            resync = _find_next_known_id(data, pos + 1, end, resync_ids)
            ctx.gaps.append((pos, resync))
            ctx.diag("corrupted_region", byte_range=[pos, resync],
                     note="unparsable bytes; re-synced at next known element id")
            pos = resync
            continue
        if id_hex in stop_ids:
            break
        if id_hex not in ELEMENTS:
            resync = _find_next_known_id(data, pos + 1, end, resync_ids)
            ctx.gaps.append((pos, resync))
            ctx.diag("corrupted_region", byte_range=[pos, resync],
                     note=f"unknown element id {id_hex}; re-synced")
            pos = resync
            continue
        name, typ = ELEMENTS[id_hex]
        try:
            size, size_len = read_vint(data, pos + id_len)
        except EBMLFormatError:
            resync = _find_next_known_id(data, pos + 1, end, resync_ids)
            ctx.gaps.append((pos, resync))
            ctx.diag("corrupted_region", byte_range=[pos, resync],
                     note="truncated element header; re-synced")
            pos = resync
            continue
        header = id_len + size_len
        data_start = pos + header
        el_id = f"el:{name}@{pos}"
        rec = {
            "object_id": el_id, "name": name, "type": typ,
            "offset": pos, "header_size": header, "size": size,
            "unknown_size": size is None, "data_start": data_start,
            "depth": depth, "parent": parent_el_id,
        }
        if size is None:
            ctx.diag("unknown_length", element=el_id, offset=pos,
                     note="element written with unknown (streaming) size")
            if typ == "master":
                child_stops = TOP_LEVEL_IDS if id_hex in ("1F43B675",) else ()
                child_resync = (("E7", "A3", "A0") + TOP_LEVEL_IDS
                                if id_hex in ("1F43B675",) else resync_ids)
                data_end = _parse_children(ctx, data_start, end, el_id,
                                           depth + 1, stop_ids=child_stops,
                                           resync_ids=child_resync)
            else:
                data_end = end
        else:
            data_end = min(data_start + size, end)
            if typ == "master":
                child_resync = (("E7", "A3", "A0") + TOP_LEVEL_IDS
                                if id_hex in ("1F43B675",) else resync_ids)
                _parse_children(ctx, data_start, data_end, el_id, depth + 1,
                                resync_ids=child_resync)
        rec["data_end"] = data_end
        rec["byte_range"] = [pos, data_end]
        if typ != "master":
            payload = data[data_start:data_end]
            rec["value"] = _decode_leaf(typ, payload)
            if len(payload) <= 64:
                rec["raw_hex"] = payload.hex()
        ctx.elements.append(rec)
        pos = data_end
    return pos


def _parse_block_payload(payload: bytes, payload_offset: int, ctx: _Ctx,
                         block_el_id: str) -> dict:
    """Parse Block/SimpleBlock payload. Never guesses on corruption."""
    out = {"frames_status": "ok", "frames": [], "lace_sizes_raw": None}
    track_number, tlen = read_vint(payload, 0)
    out["track_number"] = track_number
    rel_raw = payload[tlen:tlen + 2]
    out["rel_raw_hex"] = rel_raw.hex()
    # Standing rule: relative timecode is a SIGNED 16-bit big-endian integer.
    out["rel_signed"] = int.from_bytes(rel_raw, "big", signed=True)
    flags = payload[tlen + 2]
    out["keyframe"] = bool(flags & 0x80)
    out["invisible"] = bool(flags & 0x08)
    lacing = (flags >> 1) & 0x03
    out["lacing"] = LACING_NAMES[lacing]
    pos = tlen + 3
    frames = []
    if lacing == 0:
        frames = [payload[pos:]]
    elif lacing == 2:  # fixed
        count = payload[pos] + 1
        rest = payload[pos + 1:]
        if len(rest) % count != 0 or count == 0:
            out["frames_status"] = "indeterminate"
            ctx.diag("lacing_corrupt", element=block_el_id,
                     byte_range=[payload_offset, payload_offset + len(payload)],
                     note="fixed lacing: payload not divisible by frame count; "
                          "frame boundaries NOT guessed")
            return out
        step = len(rest) // count
        frames = [rest[i * step:(i + 1) * step] for i in range(count)]
    elif lacing == 1:  # xiph
        count = payload[pos] + 1
        pos += 1
        sizes = []
        ok = True
        for _ in range(count - 1):
            size = 0
            while True:
                if pos >= len(payload):
                    ok = False
                    break
                byte = payload[pos]
                pos += 1
                size += byte
                if byte < 255:
                    break
            if not ok:
                break
            sizes.append(size)
        out["lace_sizes_raw"] = sizes
        remaining = len(payload) - pos
        if not ok or sum(sizes) > remaining:
            out["frames_status"] = "indeterminate"
            ctx.diag("lacing_corrupt", element=block_el_id,
                     byte_range=[payload_offset, payload_offset + len(payload)],
                     lace_sizes_raw=sizes,
                     note="xiph lace sizes exceed payload; frame boundaries "
                          "NOT guessed")
            return out
        cursor = pos
        for size in sizes:
            frames.append(payload[cursor:cursor + size])
            cursor += size
        frames.append(payload[cursor:])
    else:  # ebml lacing
        count = payload[pos] + 1
        pos += 1
        try:
            first, vlen = read_vint(payload, pos)
            pos += vlen
            sizes = [first]
            for _ in range(count - 2):
                diff, vlen = read_vint(payload, pos)
                bias = (1 << (7 * vlen - 1)) - 1
                sizes.append(sizes[-1] + (diff - bias))
                pos += vlen
            remaining = len(payload) - pos - sum(sizes)
            sizes.append(remaining)
            if any(s < 0 for s in sizes) or remaining < 0:
                raise EBMLFormatError("negative lace size")
            out["lace_sizes_raw"] = sizes
            cursor = pos
            for size in sizes:
                frames.append(payload[cursor:cursor + size])
                cursor += size
        except EBMLFormatError:
            out["frames_status"] = "indeterminate"
            ctx.diag("lacing_corrupt", element=block_el_id,
                     byte_range=[payload_offset, payload_offset + len(payload)],
                     note="ebml lacing sizes invalid; frame boundaries "
                          "NOT guessed")
            return out
    cursor = payload_offset + pos if lacing != 0 else payload_offset + pos
    # recompute absolute frame ranges
    abs_cursor = payload_offset + (len(payload) - sum(len(f) for f in frames))
    for index, frame in enumerate(frames):
        out["frames"].append({
            "index": index, "size": len(frame),
            "byte_range": [abs_cursor, abs_cursor + len(frame)],
        })
        abs_cursor += len(frame)
    return out


def parse_file(data: bytes, source_name: str) -> list[dict]:
    """Parse bytes into the ordered object list (raw layer, nothing dropped)."""
    ctx = _Ctx(data)
    _parse_children(ctx, 0, len(data), None, 0)

    objects: list[dict] = []

    def add(kind: str, object_id: str, payload: dict):
        objects.append({"object_id": object_id, "kind": kind, "data": payload})

    add("source", f"src:{source_name}", {
        "name": source_name, "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data_b64": base64.b64encode(data).decode("ascii"),
        "unit_notes": "all offsets/byte ranges are absolute file bytes",
    })

    for rec in ctx.elements:
        payload = dict(rec)
        add("ebml_element", payload.pop("object_id"), payload)

    for start, end in ctx.gaps:
        add("gap", f"gap@{start}", {"byte_range": [start, end],
                                    "size": end - start})

    # ---- domain model -----------------------------------------------------
    by_name = {}
    for rec in ctx.elements:
        by_name.setdefault(rec["name"], []).append(rec)

    def child_value(parent_id, name):
        for rec in ctx.elements:
            if rec["parent"] == parent_id and rec["name"] == name:
                return rec.get("value")
        return None

    timecode_scale = DEFAULT_TIMECODE_SCALE_NS
    scale_source = "matroska_default"
    for rec in ctx.elements:
        if rec["name"] == "TimecodeScale":
            timecode_scale = rec["value"]
            scale_source = rec["object_id"] if "object_id" in rec else "element"
    add("params", "params:file", {
        "timecode_scale_ns": timecode_scale,
        "timecode_scale_source": scale_source,
        "unit": "ns per tick",
    })

    tracks_by_number = {}
    for rec in ctx.elements:
        if rec["name"] != "TrackEntry":
            continue
        entry_id = rec["object_id"]
        number = child_value(entry_id, "TrackNumber")
        uid = child_value(entry_id, "TrackUID")
        track = {
            "track_number": number, "track_uid": uid,
            "track_type": child_value(entry_id, "TrackType"),
            "codec_id": child_value(entry_id, "CodecID"),
            "default_duration_ns": child_value(entry_id, "DefaultDuration"),
            "track_timecode_scale": child_value(entry_id, "TrackTimecodeScale") or 1.0,
            "element": entry_id,
        }
        tracks_by_number[number] = track
        add("track", f"track:{uid}", track)

    segment_data_start = None
    for rec in ctx.elements:
        if rec["name"] == "Segment":
            segment_data_start = rec["data_start"]

    block_seq = 0
    cluster_seq = 0
    for rec in ctx.elements:
        if rec["name"] != "Cluster":
            continue
        cluster_seq += 1
        cluster_id = f"cluster:{cluster_seq}"
        cluster_tc = child_value(rec["object_id"], "Timecode") or 0
        add("cluster", cluster_id, {
            "offset": rec["offset"], "byte_range": rec["byte_range"],
            "unknown_size": rec["unknown_size"],
            "timecode_ticks": cluster_tc,
            "element": rec["object_id"],
        })
        for child in ctx.elements:
            if child["parent"] != rec["object_id"]:
                continue
            if child["name"] == "SimpleBlock":
                block_seq += 1
                _emit_block(ctx, add, child, cluster_id, cluster_tc,
                            tracks_by_number, block_seq, None)
            elif child["name"] == "BlockGroup":
                discard = child_value(child["object_id"], "DiscardPadding")
                for grand in ctx.elements:
                    if grand["parent"] == child["object_id"] and grand["name"] == "Block":
                        block_seq += 1
                        _emit_block(ctx, add, grand, cluster_id, cluster_tc,
                                    tracks_by_number, block_seq, discard)

    cue_seq = 0
    for rec in ctx.elements:
        if rec["name"] != "CuePoint":
            continue
        cue_seq += 1
        cue_id = f"cue:{cue_seq}"
        cue = {
            "cue_time_ticks": child_value(rec["object_id"], "CueTime"),
            "element": rec["object_id"],
            "byte_range": rec["byte_range"],
        }
        for child in ctx.elements:
            if child["parent"] == rec["object_id"] and child["name"] == "CueTrackPositions":
                cue["cue_track"] = child_value(child["object_id"], "CueTrack")
                cue["cue_cluster_position"] = child_value(child["object_id"], "CueClusterPosition")
                cue["cue_block_number"] = child_value(child["object_id"], "CueBlockNumber")
        cue["segment_data_start"] = segment_data_start
        add("cue", cue_id, cue)

    for diag in ctx.diagnostics:
        add("diagnostic", f"diag:{diag['seq']}", diag)

    return objects


def _emit_block(ctx, add, element_rec, cluster_id, cluster_tc,
                tracks_by_number, seq, discard_padding):
    payload = ctx.data[element_rec["data_start"]:element_rec["data_end"]]
    parsed = _parse_block_payload(payload, element_rec["data_start"], ctx,
                                  element_rec["object_id"])
    track = tracks_by_number.get(parsed["track_number"])
    block_id = f"block:{cluster_id.replace(':', '_')}:{seq}"
    if track is None:
        ctx.diag("unknown_track", element=element_rec["object_id"],
                 track_number=parsed["track_number"],
                 note="block references a track number not present in Tracks")
    add("block", block_id, {
        "cluster": cluster_id,
        "element": element_rec["object_id"],
        "byte_range": element_rec["byte_range"],
        "track_number": parsed["track_number"],
        "track_uid": track["track_uid"] if track else None,
        "rel_raw_hex": parsed["rel_raw_hex"],
        "rel_timecode_ticks": parsed["rel_signed"],
        "cluster_timecode_ticks": cluster_tc,
        "keyframe": parsed["keyframe"],
        "lacing": parsed["lacing"],
        "lace_sizes_raw": parsed["lace_sizes_raw"],
        "frames_status": parsed["frames_status"],
        "frames": parsed["frames"],
        "discard_padding_ns": discard_padding,
        "unit": "relative timecode in TimecodeScale ticks (signed int16)",
    })
