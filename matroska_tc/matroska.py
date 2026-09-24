"""Domain extraction: EBML tree -> Matroska fact bank.

The extractor never interprets time semantics beyond copying raw evidence.
Signed relative timecodes, lacing framing, discard padding and cue positions
are decoded structurally; *truth* about times is decided in engine.py.
"""
from __future__ import annotations

import struct
from typing import Any, Optional

from . import ebml, tags
from .models import (FactBank, oid, q, brange, U_BYTE, U_SEG_BYTE, U_TICK,
                     U_S16_TICK, U_NS_PER_TICK, U_HZ, U_PX, U_NONE,
                     DEFAULT_TIMESCALE_NS, TRACK_TYPES)


def _uint(data: Optional[bytes]) -> Optional[int]:
    if data is None:
        return None
    return int.from_bytes(data, "big", signed=False)


def _float(data: Optional[bytes]) -> Optional[float]:
    if data is None or len(data) not in (4, 8):
        return None
    return struct.unpack(">f" if len(data) == 4 else ">d", data)[0]


def _signed(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def walk_elements(elements, out, source_id, depth=0, parent_oid=None,
                  file_len=0):
    """Flatten every EBML element (incl. unknown ids) into range facts."""
    for el in elements:
        is_unknown_name = el.name.startswith("Unknown_")
        eoid = oid("elem", source_id, el.header_start, el.id)
        rec = {
            "oid": eoid,
            "id": f"0x{el.id:X}",
            "name": el.name,
            "known": not is_unknown_name,
            "depth": depth,
            "header_start": q(el.header_start, U_BYTE),
            "data_start": q(el.data_start, U_BYTE),
            "data_end": q(el.data_end, U_BYTE),
            "raw_range": brange(el.header_start, el.data_end),
            "value_range": brange(el.data_start, el.data_end),
            "id_len": el.id_len,
            "size_len": el.size_len,
            "size": None if el.size is None else q(el.size, U_BYTE),
            "unknown_length": el.size is None,
            "parent_oid": parent_oid,
            "scalar": _scalar_preview(el),
        }
        out.elements.append(rec)
        if el.size is None:
            out.unknown_lengths.append({
                "oid": oid("ulen", source_id, el.header_start),
                "element_oid": eoid,
                "name": el.name,
                "header_start": q(el.header_start, U_BYTE),
                "resolved_end": q(el.data_end, U_BYTE),
                "raw_range": brange(el.header_start, el.data_end),
                "resolution": "eof" if el.data_end >= file_len else "boundary",
            })
        walk_elements(el.children, out, source_id, depth + 1, eoid,
                      file_len)


def _scalar_preview(el):
    if el.children or el.value is None:
        return None
    if el.id in (tags.TIMECODE_SCALE, tags.TRACK_NUMBER, tags.TRACK_UID,
                 tags.TRACK_TYPE, tags.FLAG_LACING, tags.DEFAULT_DURATION,
                 tags.CLUSTER_TIMECODE, tags.BLOCK_DURATION,
                 tags.REFERENCE_BLOCK, tags.CUE_TIME, tags.CUE_TRACK,
                 tags.CUE_CLUSTER_POSITION, tags.CUE_RELATIVE_POSITION,
                 tags.CUE_DURATION, tags.PIXEL_WIDTH, tags.PIXEL_HEIGHT,
                 tags.CHANNELS, tags.EBML_VERSION, tags.DOC_TYPE_VERSION):
        return _uint(el.value)
    if el.id in (tags.SAMPLING_FREQUENCY, tags.DURATION):
        return _float(el.value)
    if el.id in (tags.DOC_TYPE, tags.CODEC_ID, tags.NAME, tags.MUXING_APP,
                 tags.WRITING_APP, tags.LANGUAGE):
        try:
            return el.value.decode("utf-8")
        except UnicodeDecodeError:
            return el.value.hex()
    return None


_eof = -1


# ---- block binary parsing ---------------------------------------------------
def parse_block_binary(data: bytes):
    """Return (track_number, rel_signed, rel_unsigned, flags, lace_offset).

    rel_signed interprets the stored 16-bit relative timecode as signed
    (Matroska rule). The unsigned reading is retained too as raw evidence.
    Raises ValueError if the header cannot be framed.
    """
    if len(data) < 4:
        raise ValueError("block shorter than 4-byte header")
    tn_vint = ebml.read_vint(data, 0)
    if tn_vint.marker_unknown:
        raise ValueError("block track number is unknown-vint")
    p = tn_vint.raw_len
    unsigned = int.from_bytes(data[p:p + 2], "big")
    rel_signed = _signed(unsigned, 16)
    p += 2
    flags = data[p]
    p += 1
    return tn_vint.value, rel_signed, unsigned, flags, p


def frame_lacing(payload: bytes, flags: int, out_diag, block_oid,
                 byte_start):
    """Split a block payload into lace frame ranges (payload-relative).

    On corruption returns (None, reason) and never guesses boundaries.
    """
    lacing = (flags >> 1) & 0x03
    if lacing == tags.LACING_NONE:
        return {"mode": "none", "count": 1,
                "ranges": [brange(0, len(payload))], "sizes": [len(payload)]}, None
    if len(payload) < 1:
        return None, "lacing header missing"
    count = payload[0] + 1
    body = payload[1:]
    if lacing == tags.LACING_FIXED:
        if body.__len__() % count != 0:
            reason = (f"fixed-size lacing: {len(body)} payload bytes not "
                      f"divisible by {count} frames")
            out_diag("lacing_corrupt", "error", reason, refs=[block_oid],
                     byte_range=brange(byte_start + 1,
                                       byte_start + 1 + len(payload)))
            return None, reason
        size = len(body) // count
        ranges = [brange(i * size, (i + 1) * size) for i in range(count)]
        return {"mode": "fixed", "count": count, "ranges": ranges,
                "sizes": [size] * count}, None
    if lacing == tags.LACING_XIPH:
        try:
            sizes, header_end = _xiph_parse(body, count)
        except ValueError as exc:
            reason = f"xiph lacing: {exc}"
            out_diag("lacing_corrupt", "error", reason, refs=[block_oid],
                     byte_range=brange(byte_start + 1,
                                       byte_start + len(payload) + 1))
            return None, reason
        return _assemble_lace("xiph", sizes, header_end, len(body),
                              block_oid, byte_start, out_diag)
    if lacing == tags.LACING_EBML:
        try:
            sizes = _ebml_lace_sizes(body, count)
        except ValueError as exc:
            reason = f"EBML lacing: {exc}"
            out_diag("lacing_corrupt", "error", reason, refs=[block_oid],
                     byte_range=brange(byte_start + 1,
                                       byte_start + len(payload) + 1))
            return None, reason
        header_end = _ebml_header_len(body, count)
        return _assemble_lace("ebml", sizes, header_end, len(body),
                              block_oid, byte_start, out_diag)
    return None, f"reserved lacing mode {lacing}"


def _xiph_parse(body: bytes, count: int):
    sizes = []
    i = 0
    for _ in range(count - 1):
        total = 0
        seen = False
        while i < len(body):
            b = body[i]
            i += 1
            total += b
            if b != 255:
                seen = True
                break
        if not seen:
            raise ValueError("size run overruns lacing header")
        sizes.append(total)
    header_end = i
    used = sum(sizes)
    last = len(body) - header_end - used
    if last < 0:
        raise ValueError("declared frame sizes exceed payload")
    sizes.append(last)
    return sizes, header_end


def _ebml_header_len(body: bytes, count: int) -> int:
    off = 0
    for _ in range(count - 1):
        v = ebml.read_vint(body, off)
        off += v.raw_len
    return off


def _ebml_lace_sizes(body: bytes, count: int):
    # First size is an EBML vint; following are signed vints (differences).
    if count == 1:
        return [len(body)]
    sizes: list[int] = []
    off = 0
    first = ebml.read_vint(body, off)
    if first.marker_unknown:
        raise ValueError("first frame size is unknown-vint")
    sizes.append(first.value)
    off += first.raw_len
    for _ in range(count - 2):
        start = off
        v = ebml.read_vint(body, off)
        off += v.raw_len
        width = v.raw_len
        marker_pos = (width - 1) * 8 + (8 - width)
        diff = v.value - (1 << marker_pos)  # strip marker -> signed
        sizes.append(sizes[-1] + diff)
        if sizes[-1] < 0:
            raise ValueError(f"negative frame size near byte {start}")
    header_end = off
    used = sum(sizes)
    last = len(body) - header_end - used
    if last < 0:
        raise ValueError("accumulated sizes exceed payload")
    sizes.append(last)
    return sizes


def _assemble_lace(mode, sizes, header_end, body_len, block_oid,
                   byte_start, out_diag):
    if any(s < 0 for s in sizes):
        reason = f"{mode} lacing produced a negative frame size"
        out_diag("lacing_corrupt", "error", reason, refs=[block_oid])
        return None, reason
    ranges = []
    cursor = header_end
    for size in sizes:
        ranges.append(brange(cursor, cursor + size))
        cursor += size
    if cursor != body_len:
        reason = (f"{mode} lacing frames end at byte {cursor} but payload is "
                  f"{body_len} bytes")
        out_diag("lacing_corrupt", "error", reason, refs=[block_oid],
                 byte_range=brange(byte_start, byte_start + body_len + 1))
        return None, reason
    return {"mode": mode, "count": len(sizes), "ranges": ranges,
            "sizes": sizes}, None


def extract(buf: bytes, source_id: str) -> FactBank:
    tags.configure()
    out = FactBank(source_id=source_id)
    roots = ebml.parse_root(buf)
    walk_elements(roots, out, source_id, 0, None, file_len=len(buf))

    segment = next((e for e in roots if e.id == tags.SEGMENT), None)
    if segment is None:
        out.diag("no_segment", "error", "no Matroska Segment element found")
        return out
    out.segment_data_start = segment.data_start
    out.segment_unknown = segment.size is None

    info = segment.find(tags.INFO)
    if info is not None:
        ts_raw = info.child_value(tags.TIMECODE_SCALE)
        ts = _uint(ts_raw)
        if ts is None or ts <= 0:
            out.diag("timecode_scale", "warning",
                     "TimecodeScale missing/non-positive; raw position kept",
                     refs=[_eoid(info, tags.TIMECODE_SCALE, source_id)],
                     detail={"raw": None if ts_raw is None
                             else ts_raw.hex()})
            ts = DEFAULT_TIMESCALE_NS
        out.timescale = {
            "element_oid": _eoid(info, tags.TIMECODE_SCALE, source_id),
            "scale": q(ts, U_NS_PER_TICK,
                       None if ts_raw is None else ts_raw.hex()),
            "raw_range": (brange(info.find(tags.TIMECODE_SCALE).data_start,
                                 info.find(tags.TIMECODE_SCALE).data_end)
                          if info.find(tags.TIMECODE_SCALE) else None),
            "defaulted": ts_raw is None or _uint(ts_raw) in (None, 0),
        }
    else:
        out.diag("no_info", "warning", "no Info element; default TimecodeScale")
        out.timescale = {"element_oid": None,
                         "scale": q(DEFAULT_TIMESCALE_NS, U_NS_PER_TICK),
                         "raw_range": None, "defaulted": True}

    _extract_tracks(segment, out, source_id)
    track_by_number = {t["number"]["value"]: t for t in out.tracks}
    _extract_clusters(segment, out, source_id, track_by_number, buf)
    _extract_cues(segment, out, source_id, track_by_number)
    return out


def _eoid(parent_el, child_id, source_id):
    child = parent_el.find(child_id)
    return None if child is None else oid("elem", source_id,
                                          child.header_start, child.id)


def _extract_tracks(segment, out, source_id):
    tracks_el = segment.find(tags.TRACKS)
    if tracks_el is None:
        out.diag("no_tracks", "error", "no Tracks element")
        return
    for te in tracks_el.find_all(tags.TRACK_ENTRY):
        number = _uint(te.child_value(tags.TRACK_NUMBER))
        uid = _uint(te.child_value(tags.TRACK_UID))
        ttype = _uint(te.child_value(tags.TRACK_TYPE))
        default_duration = _uint(te.child_value(tags.DEFAULT_DURATION))
        rec = {
            "oid": oid("track", source_id, number),
            "element_oid": oid("elem", source_id, te.header_start, te.id),
            "number": q(number, U_NONE,
                        None if te.find(tags.TRACK_NUMBER) is None
                        else te.find(tags.TRACK_NUMBER).value.hex()),
            "uid": q(uid, U_NONE),
            "type": q(TRACK_TYPES.get(ttype, f"unknown({ttype})"), U_NONE),
            "codec": (te.child_value(tags.CODEC_ID) or b"").decode(
                "utf-8", "replace"),
            "name": (te.child_value(tags.NAME) or b"").decode(
                "utf-8", "replace"),
            "default_duration": (q(default_duration, "ns/frame",
                                   te.find(tags.DEFAULT_DURATION).value.hex())
                                 if default_duration is not None else None),
            "flag_lacing": _uint(te.child_value(tags.FLACING)
                                 if False else te.child_value(tags.FLAG_LACING)),
            "raw_range": brange(te.header_start, te.data_end),
        }
        audio = te.find(tags.AUDIO)
        if audio is not None:
            freq = _float(audio.child_value(tags.SAMPLING_FREQUENCY))
            chans = _uint(audio.child_value(tags.CHANNELS))
            rec["sampling_frequency"] = (
                q(freq, U_HZ,
                  audio.find(tags.SAMPLING_FREQUENCY).value.hex())
                if freq is not None else None)
            rec["channels"] = q(chans or 1, U_NONE)
        video = te.find(tags.VIDEO)
        if video is not None:
            pw = _uint(video.child_value(tags.PIXEL_WIDTH))
            ph = _uint(video.child_value(tags.PIXEL_HEIGHT))
            rec["pixel_width"] = q(pw, U_PX) if pw is not None else None
            rec["pixel_height"] = q(ph, U_PX) if ph is not None else None
        out.tracks.append(rec)


def _extract_clusters(segment, out, source_id, track_by_number, buf):
    cluster_index = 0
    for cluster in segment.find_all(tags.CLUSTER):
        tc_el = cluster.find(tags.CLUSTER_TIMECODE)
        tc_raw = tc_el.value if tc_el is not None else None
        tc_value = _uint(tc_raw)
        coid = oid("cluster", source_id, cluster.header_start)
        out.clusters.append({
            "oid": coid,
            "element_oid": oid("elem", source_id, cluster.header_start,
                               cluster.id),
            "seq": cluster_index,
            "timecode": q(tc_value, U_TICK,
                          None if tc_raw is None else tc_raw.hex()),
            "timecode_present": tc_el is not None,
            "header_start": q(cluster.header_start, U_BYTE),
            "data_start": q(cluster.data_start, U_BYTE),
            "data_end": q(cluster.data_end, U_BYTE),
            "segment_position": q(cluster.data_start
                                  - out.segment_data_start, U_SEG_BYTE),
            "unknown_length": cluster.size is None,
            "raw_range": brange(cluster.header_start, cluster.data_end),
        })
        if tc_el is None:
            out.diag("cluster_no_timecode", "error",
                     f"cluster #{cluster_index} has no Timecode",
                     refs=[coid],
                     byte_range=brange(cluster.header_start,
                                       cluster.data_start))
        block_seq = 0
        for child in cluster.children:
            if child.id == tags.SIMPLE_BLOCK:
                _record_block(out, source_id, child, coid, cluster_index,
                              block_seq, tc_value, track_by_number, buf,
                              simple=True)
                block_seq += 1
            elif child.id == tags.BLOCK_GROUP:
                block_el = child.find(tags.BLOCK)
                if block_el is not None:
                    _record_block(out, source_id, block_el, coid,
                                  cluster_index, block_seq, tc_value,
                                  track_by_number, buf, simple=False,
                                  group_el=child)
                    block_seq += 1
        cluster_index += 1


def _record_block(out, source_id, block_el, cluster_oid, cluster_index,
                  block_seq, cluster_tc, track_by_number, buf, simple,
                  group_el=None):
    boid = oid("block", source_id, block_el.header_start)
    data = block_el.value or b""
    group_oid = (oid("blockgroup", source_id, group_el.header_start)
                 if group_el is not None else None)
    try:
        track_num, rel_signed, rel_unsigned, flags, lace_off = \
            parse_block_binary(data)
    except ValueError as exc:
        out.diag("block_header_corrupt", "error",
                 f"block header unframeable: {exc}", refs=[boid],
                 byte_range=brange(block_el.data_start, block_el.data_end))
        out.blocks.append({
            "oid": boid, "cluster_oid": cluster_oid,
            "element_oid": oid("elem", source_id, block_el.header_start,
                               block_el.id),
            "seq": block_seq, "simple": simple, "group_oid": group_oid,
            "framed": False, "raw_range": brange(block_el.header_start,
                                                 block_el.data_end),
            "payload_range": brange(block_el.data_start,
                                    block_el.data_end),
            "segment_position": q(block_el.data_start
                                  - out.segment_data_start, U_SEG_BYTE),
        })
        return

    track = track_by_number.get(track_num)
    if track is None:
        out.diag("unknown_track", "error",
                 f"block references unknown track number {track_num}",
                 refs=[boid],
                 byte_range=brange(block_el.data_start,
                                   block_el.data_start + 6))
    payload = data[lace_off:]
    block_duration = None
    discard_padding = None
    reference_blocks = []
    if group_el is not None:
        bd = group_el.child_value(tags.BLOCK_DURATION)
        if bd is not None:
            block_duration = q(_uint(bd), U_TICK, bd.hex())
        dp = group_el.child_value(tags.DISCARD_PADDING)
        if dp is not None:
            raw_val = int.from_bytes(dp, "big", signed=True)
            discard_padding = q(raw_val, U_TICK, dp.hex())
        for rb_el in group_el.find_all(tags.REFERENCE_BLOCK):
            rbv = int.from_bytes(rb_el.value, "big", signed=True)
            reference_blocks.append(q(rbv, U_S16_TICK, rb_el.value.hex()))

    keyframe = bool(flags & 0x80) if simple else None
    rec = {
        "oid": boid, "cluster_oid": cluster_oid,
        "element_oid": oid("elem", source_id, block_el.header_start,
                           block_el.id),
        "group_oid": group_oid,
        "seq": block_seq, "simple": simple, "framed": True,
        "track_number": q(track_num, U_NONE),
        "track_oid": track["oid"] if track else None,
        "cluster_index": cluster_index,
        "cluster_timecode": q(cluster_tc, U_TICK)
        if cluster_tc is not None else None,
        "relative_timecode": {
            "signed": q(rel_signed, U_S16_TICK,
                        data[lace_off - 3:lace_off - 1].hex()),
            "unsigned": q(rel_unsigned, "u16_tick"),
            "bits": 16,
        },
        "flags": flags, "keyframe": keyframe,
        "invisible": bool(flags & 0x08),
        "block_duration": block_duration,
        "discard_padding": discard_padding,
        "reference_blocks": reference_blocks,
        "header_range": brange(block_el.data_start,
                               block_el.data_start + lace_off),
        "payload_range": brange(block_el.data_start + lace_off,
                                block_el.data_end),
        "raw_range": brange(block_el.header_start, block_el.data_end),
        "segment_position": q(block_el.data_start - out.segment_data_start,
                              U_SEG_BYTE),
    }
    lace, reason = frame_lacing(
        payload, flags,
        lambda *a, **k: out.diag(*a, **k), boid,
        block_el.data_start + lace_off)
    rec["lacing"] = lace
    rec["lacing_corrupt"] = reason
    out.blocks.append(rec)

    if lace is not None:
        for i, rng in enumerate(lace["ranges"]):
            abs_start = block_el.data_start + lace_off + rng["start"]
            abs_end = block_el.data_start + lace_off + rng["end"]
            out.frames.append({
                "oid": oid("frame", source_id, block_el.header_start, i),
                "block_oid": boid, "track_oid": rec["track_oid"],
                "index_in_block": i,
                "payload_range": brange(abs_start, abs_end),
                "size": q(abs_end - abs_start, U_BYTE),
            })


def _extract_cues(segment, out, source_id):
    cues_el = segment.find(tags.CUES)
    cluster_by_pos = {c["segment_position"]["value"]: c
                      for c in out.clusters}
    if cues_el is None:
        return
    for cp in cues_el.find_all(tags.CUE_POINT):
        cue_time = _uint(cp.child_value(tags.CUE_TIME))
        for ctp in cp.find_all(tags.CUE_TRACK_POSITIONS):
            track_num = _uint(ctp.child_value(tags.CUE_TRACK))
            cluster_pos = _uint(ctp.child_value(tags.CUE_CLUSTER_POSITION))
            rel_pos = _uint(ctp.child_value(tags.CUE_RELATIVE_POSITION))
            cue_duration = _uint(ctp.child_value(tags.CUE_DURATION))
            track = next((t for t in out.tracks
                          if t["number"]["value"] == track_num), None)
            cid = oid("cue", source_id, cp.header_start, track_num)
            target = cluster_by_pos.get(cluster_pos)
            absolute = (None if cluster_pos is None
                        else out.segment_data_start + cluster_pos
                        + (rel_pos or 0))
            rec = {
                "oid": cid,
                "element_oid": oid("elem", source_id, ctp.header_start,
                                   ctp.id),
                "cue_time": q(cue_time, U_TICK,
                              cp.find(tags.CUE_TIME).value.hex()
                              if cp.find(tags.CUE_TIME) else None),
                "track_number": q(track_num, U_NONE),
                "track_oid": track["oid"] if track else None,
                "cluster_position": q(cluster_pos, U_SEG_BYTE,
                                      ctp.find(tags.CUE_CLUSTER_POSITION)
                                      .value.hex()),
                "relative_position": (q(rel_pos, U_SEG_BYTE,
                                        ctp.find(tags.CUE_RELATIVE_POSITION)
                                        .value.hex())
                                      if rel_pos is not None else None),
                "absolute_position": q(absolute, U_BYTE)
                if absolute is not None else None,
                "cue_duration": q(cue_duration, U_TICK)
                if cue_duration is not None else None,
                "raw_range": brange(cp.header_start, cp.data_end),
                "points_to_cluster_oid": target["oid"] if target else None,
            }
            out.cues.append(rec)
            if target is None:
                out.diag("cue_mispoint", "error",
                         f"cue track {track_num} points at segment offset "
                         f"{cluster_pos} where no cluster begins",
                         refs=[cid],
                         byte_range=brange(
                             ctp.find(tags.CUE_CLUSTER_POSITION).data_start,
                             ctp.find(tags.CUE_CLUSTER_POSITION).data_end),
                         detail={"declared_cluster_position": cluster_pos})
            else:
                declared_tc = target["timecode"]["value"]
                if (cue_time is not None and declared_tc is not None
                        and cue_time < declared_tc):
                    out.diag("cue_time_before_cluster", "warning",
                             f"CueTime {cue_time} precedes target cluster "
                             f"timecode {declared_tc}; cue is index "
                             f"evidence, not time truth",
                             refs=[cid, target["oid"]])
