"""Matroska semantic layer on top of the raw EBML tree.

Rules enforced here:
- Block relative timecodes are signed 16-bit integers.
- Corrupt lacing never leads to guessed frame boundaries; the block extent is
  kept and frames are reported as unknown.
- DiscardPadding is preserved with its raw value and unit.
"""
from __future__ import annotations

from ebml import Element, read_vint

LACING_MODES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}

DEFAULT_TIMECODE_SCALE_NS = 1_000_000


def decode_block(data: bytes) -> dict:
    raw_track, track, tlen = read_vint(data, 0)
    rel = int.from_bytes(data[tlen:tlen + 2], "big", signed=True)
    flags = data[tlen + 2]
    lacing = (flags >> 1) & 0x03
    rest = data[tlen + 3:]
    mode = LACING_MODES[lacing]
    frames = None
    frame_sizes = None
    corrupt = False
    diagnostic = None

    if lacing == 0:
        frame_sizes = [len(rest)]
        frames = [{"offset": 0, "size": len(rest)}]
    elif lacing == 1:  # Xiph
        if len(rest) < 1:
            corrupt, diagnostic = True, "xiph lacing: missing frame-count byte"
        else:
            count = rest[0] + 1
            sizes = []
            i = 1
            ok = True
            for _ in range(count - 1):
                size = 0
                while True:
                    if i >= len(rest):
                        ok = False
                        break
                    b = rest[i]
                    i += 1
                    size += b
                    if b < 255:
                        break
                if not ok:
                    break
                sizes.append(size)
            if not ok:
                corrupt, diagnostic = True, "xiph lacing: truncated lace sizes"
            else:
                payload = len(rest) - i
                last = payload - sum(sizes)
                if last < 0:
                    corrupt = True
                    diagnostic = (
                        f"xiph lacing: lace sizes {sizes} exceed payload "
                        f"{payload} bytes"
                    )
                else:
                    sizes.append(last)
                    frame_sizes = sizes
                    frames = []
                    pos = i
                    for s in sizes:
                        frames.append({"offset": pos, "size": s})
                        pos += s
    elif lacing == 2:  # fixed-size
        if len(rest) < 1:
            corrupt, diagnostic = True, "fixed lacing: missing frame-count byte"
        else:
            count = rest[0] + 1
            payload = len(rest) - 1
            if count == 0 or payload % count != 0:
                corrupt = True
                diagnostic = (
                    f"fixed lacing: payload {payload} not divisible by {count} frames"
                )
            else:
                each = payload // count
                frame_sizes = [each] * count
                frames = [{"offset": 1 + k * each, "size": each} for k in range(count)]
    else:  # EBML lacing
        try:
            count = rest[0] + 1
            _, first_size, used = read_vint(rest, 1)
            sizes = [first_size]
            pos = 1 + used
            for _ in range(count - 2):
                _, diff_raw, dlen = read_vint(rest, pos)
                bias = (1 << (7 * dlen - 1)) - 1
                sizes.append(sizes[-1] + (diff_raw - bias))
                pos += dlen
            payload = len(rest) - pos
            last = payload - sum(sizes)
            if last < 0 or any(s < 0 for s in sizes):
                raise ValueError("negative frame size")
            sizes.append(last)
            frame_sizes = sizes
            frames = []
            for s in sizes:
                frames.append({"offset": pos, "size": s})
                pos += s
        except (IndexError, ValueError) as exc:
            corrupt, diagnostic = True, f"ebml lacing: {exc}"

    if corrupt:
        frames = None
        frame_sizes = None

    return {
        "track": track,
        "rel_timecode": rel,
        "flags": flags,
        "lacing_mode": mode,
        "lacing_corrupt": corrupt,
        "lacing_diagnostic": diagnostic,
        "frames": frames,
        "frame_sizes": frame_sizes,
        "header_len": tlen + 3,
        "payload_size": len(rest),
    }


def _find(elements, name):
    for e in elements:
        if e.name == name:
            return e
    return None


def extract_semantics(elements: list[Element]) -> dict:
    seg = _find(elements, "Segment")
    if seg is None:
        raise ValueError("no Segment element")
    out = {
        "segment_data_offset": seg.data_offset,
        "segment_end": seg.end,
        "timecode_scale_ns": DEFAULT_TIMECODE_SCALE_NS,
        "tracks": [],
        "clusters": [],
        "cues": [],
        "top_level": [
            {"path": c.path, "name": c.name, "offset": c.offset, "end": c.end}
            for c in seg.children
        ],
    }
    for child in seg.children:
        if child.name == "Info":
            scale = _find(child.children, "TimecodeScale")
            if scale is not None:
                out["timecode_scale_ns"] = scale.value
        elif child.name == "Tracks":
            for te in child.children:
                if te.name != "TrackEntry":
                    continue
                track = {"number": None, "type": None, "name": None,
                         "default_duration_ns": None}
                for e in te.children:
                    if e.name == "TrackNumber":
                        track["number"] = e.value
                    elif e.name == "TrackType":
                        track["type"] = e.value
                    elif e.name == "Name":
                        track["name"] = e.value
                    elif e.name == "DefaultDuration":
                        track["default_duration_ns"] = e.value
                out["tracks"].append(track)
        elif child.name == "Cluster":
            ci = len(out["clusters"])
            cluster = {
                "path": child.path,
                "offset": child.offset,
                "end": child.end,
                "unknown_size": child.unknown_size,
                "timecode": 0,
                "blocks": [],
            }
            bi = 0
            for e in child.children:
                if e.name == "Timecode":
                    cluster["timecode"] = e.value
                elif e.name == "BlockGroup":
                    padding = None
                    block_el = None
                    for ge in e.children:
                        if ge.name == "DiscardPadding":
                            padding = ge.value
                        elif ge.name == "Block":
                            block_el = ge
                    if block_el is not None:
                        cluster["blocks"].append(_block_record(
                            block_el, ci, bi, padding))
                        bi += 1
                elif e.name == "SimpleBlock":
                    cluster["blocks"].append(_block_record(e, ci, bi, None))
                    bi += 1
            out["clusters"].append(cluster)
        elif child.name == "Cues":
            for cp in child.children:
                if cp.name != "CuePoint":
                    continue
                cue = {"path": cp.path, "cue_time": None, "positions": []}
                for e in cp.children:
                    if e.name == "CueTime":
                        cue["cue_time"] = e.value
                    elif e.name == "CueTrackPositions":
                        pos = {"track": None, "cluster_position": None}
                        for pe in e.children:
                            if pe.name == "CueTrack":
                                pos["track"] = pe.value
                            elif pe.name == "CueClusterPosition":
                                pos["cluster_position"] = pe.value
                        cue["positions"].append(pos)
                out["cues"].append(cue)
    return out


def _block_record(block_el: Element, cluster_idx: int, block_idx: int,
                  discard_padding) -> dict:
    decoded = decode_block(block_el.data)
    record = {
        "id": f"blk:{cluster_idx}:{block_idx}",
        "element_path": block_el.path,
        "raw_range": [block_el.offset, block_el.end],
        "data_offset": block_el.data_offset,
        "raw_hex": block_el.data.hex(),
        "coordinate": "cluster-relative, TimecodeScale units",
    }
    record.update(decoded)
    if discard_padding is not None:
        record["discard_padding"] = {
            "value": discard_padding,
            "unit": "TimecodeScale units",
            "element_path": f"{block_el.path}/../DiscardPadding",
        }
    else:
        record["discard_padding"] = None
    return record
