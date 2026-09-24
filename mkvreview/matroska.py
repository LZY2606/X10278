"""Matroska/WebM semantic extraction over the EBML tree.

The extractor is an *import* layer: it preserves raw representations, byte
coordinates and ambiguities.  Normalization (ms timelines, gap detection,
cue verification) happens later in the derivation engine.
"""

from __future__ import annotations

import hashlib

from . import ebml
from .lacing import decode_block_payload
from .units import (
    BYTE_OFFSET,
    MS,
    NS,
    RAW_BYTES,
    RAW_INT,
    SEGMENT_OFFSET,
    TICKS,
    Measure,
)
from .vint import VIntError


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _raw_uint(element: ebml.Element | None, unit: str = RAW_INT) -> Measure | None:
    if element is None:
        return None
    value = ebml.uint_value(element)
    return Measure(
        value=value,
        unit=unit,
        raw=value,
        basis=f"{BYTE_OFFSET}:{element.header_start}",
    )


def extract(data: bytes, file_id: str | None = None) -> dict:
    """Build the raw fact layer from Matroska bytes.

    The returned object is JSON-serializable and deterministic for identical
    bytes (stable identities used by storage and export/import).
    """
    parsed = ebml.parse_file(data)
    tree = parsed["elements"]
    fid = file_id or f"file-{_fingerprint(data)}"

    facts: dict = {
        "file_id": fid,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "elements": [element.to_dict() for element in tree],
        "segment": None,
        "info": {},
        "tracks": [],
        "clusters": [],
        "blocks": [],
        "cues": [],
        "diagnostics": list(parsed["diagnostics"]),
    }

    segment = next((element for element in tree if element.id == ebml.SEGMENT), None)
    if segment is None:
        facts["diagnostics"].append(
            {"code": "missing-segment", "message": "no Segment element found"}
        )
        return facts

    facts["segment"] = {
        "header_start": segment.header_start,
        "data_start": segment.data_start,
        "size": segment.size,
        "unknown_length": segment.unknown_length,
        "end": segment.end,
        "raw_range": [segment.header_start, segment.end],
    }
    data_start = segment.data_start

    info = segment.find(ebml.INFO)
    if info is not None:
        scale_element = info.find(ebml.TIMECODE_SCALE)
        scale = ebml.uint_value(scale_element, 1_000_000)
        facts["info"] = {
            "timecode_scale": Measure(scale, NS, raw=scale, basis=f"{RAW_INT}:TimecodeScale").to_dict()
            if scale is not None
            else Measure(None, NS, raw=None, diagnostic="missing TimecodeScale").to_dict(),
            "duration_ticks": _maybe_float(info.find(ebml.DURATION), TICKS),
            "muxing_app": ebml.string_value(info.find(ebml.MUXING_APP)),
            "writing_app": ebml.string_value(info.find(ebml.WRITING_APP)),
            "raw_range": [info.header_start, info.end],
            "unknown_length": info.unknown_length,
        }

    tracks_element = segment.find(ebml.TRACKS)
    if tracks_element is not None:
        for entry in tracks_element.find_all(ebml.TRACK_ENTRY):
            facts["tracks"].append(_extract_track(entry))

    for cluster_index, cluster in enumerate(segment.find_all(ebml.CLUSTER)):
        cluster_record, block_records = _extract_cluster(cluster, data, data_start)
        cluster_record["cluster_index"] = cluster_index
        facts["clusters"].append(cluster_record)
        facts["blocks"].extend(block_records)

    cues_element = segment.find(ebml.CUES)
    if cues_element is not None:
        facts["cues"] = _extract_cues(cues_element, data_start)

    return facts


def _maybe_float(element: ebml.Element | None, unit: str) -> dict | None:
    if element is None:
        return None
    value = ebml.float_value(element)
    raw = element.data.hex() if element.data is not None else None
    if value is None:
        return Measure(None, unit, raw=raw, diagnostic="unparsed float").to_dict()
    return Measure(value, unit, raw=raw).to_dict()


def _extract_track(entry: ebml.Element) -> dict:
    number = ebml.uint_value(entry.find(ebml.TRACK_NUMBER))
    uid = ebml.uint_value(entry.find(ebml.TRACK_UID))
    track_type = ebml.uint_value(entry.find(ebml.TRACK_TYPE))
    flag_lacing = ebml.uint_value(entry.find(ebml.FLAG_LACING), 1)
    default_duration = ebml.uint_value(entry.find(ebml.DEFAULT_DURATION))
    audio = entry.find(ebml.AUDIO)
    video = entry.find(ebml.VIDEO)
    sampling = ebml.float_value(audio.find(ebml.SAMPLING_FREQUENCY), 8000.0) if audio is not None else None
    width = ebml.uint_value(video.find(ebml.PIXEL_WIDTH)) if video is not None else None
    height = ebml.uint_value(video.find(ebml.PIXEL_HEIGHT)) if video is not None else None
    frame_rate = ebml.float_value(video.find(ebml.FRAME_RATE)) if video is not None else None
    timebase_kind = "audio-samplerate" if audio is not None else "video-default-duration" if default_duration else "unconstrained"
    return {
        "track_number": number,
        "track_uid": uid,
        "track_type": track_type,
        "flag_lacing": flag_lacing,
        "default_duration": Measure(default_duration, NS, raw=default_duration).to_dict()
        if default_duration is not None
        else Measure(None, NS, raw=None, diagnostic="no DefaultDuration").to_dict(),
        "name": ebml.string_value(entry.find(ebml.NAME)),
        "codec_id": ebml.string_value(entry.find(ebml.CODEC_ID)),
        "sampling_frequency": Measure(sampling, "Hz", raw=sampling).to_dict()
        if sampling is not None
        else None,
        "pixel_width": width,
        "pixel_height": height,
        "frame_rate": Measure(frame_rate, "Hz", raw=frame_rate).to_dict() if frame_rate is not None else None,
        "timebase_kind": timebase_kind,
        "raw_range": [entry.header_start, entry.end],
        "stable_id": f"track-{number}",
    }


def _extract_cluster(cluster: ebml.Element, data: bytes, segment_data_start: int) -> tuple[dict, list[dict]]:
    timecode_element = cluster.find(ebml.CLUSTER_TIMECODE)
    timecode = ebml.uint_value(timecode_element)
    cluster_record = {
        "cluster_index": None,  # assigned later in file order (fact, not timeline truth)
        "timecode_ticks": Measure(timecode, TICKS, raw=timecode,
                                  basis=f"{RAW_INT}:ClusterTimecode").to_dict()
        if timecode is not None
        else Measure(None, TICKS, raw=None, diagnostic="missing ClusterTimecode").to_dict(),
        "header_start": cluster.header_start,
        "data_start": cluster.data_start,
        "size": cluster.size,
        "unknown_length": cluster.unknown_length,
        "end": cluster.end,
        "raw_range": [cluster.header_start, cluster.end],
        "segment_relative_position": cluster.header_start - segment_data_start,
        "truncated": cluster.truncated,
        "block_children": [],
    }
    blocks: list[dict] = []
    order = 0
    for child in cluster.children:
        if child.id == ebml.SIMPLE_BLOCK:
            block_record = _extract_block(child, data, cluster, cluster_record, order, "SimpleBlock", None)
            blocks.append(block_record)
            cluster_record["block_children"].append(block_record["stable_id"])
            order += 1
        elif child.id == ebml.BLOCK_GROUP:
            block_element = child.find(ebml.BLOCK)
            discard_element = child.find(ebml.DISCARD_PADDING)
            duration_element = child.find(ebml.BLOCK_DURATION)
            reference = child.find(ebml.REFERENCE_BLOCK)
            if block_element is not None and block_element.data is not None:
                block_record = _extract_block(
                    block_element,
                    data,
                    cluster,
                    cluster_record,
                    order,
                    "Block",
                    {
                        "discard_padding_ticks": ebml.sint_value(discard_element)
                        if discard_element is not None
                        else None,
                        "block_duration_ticks": ebml.uint_value(duration_element),
                        "reference_block_ticks": ebml.sint_value(reference) if reference is not None else None,
                        "group_raw_range": [child.header_start, child.end],
                        "discard_raw_range": [discard_element.header_start, discard_element.end]
                        if discard_element is not None
                        else None,
                    },
                )
                blocks.append(block_record)
                cluster_record["block_children"].append(block_record["stable_id"])
                order += 1
    return cluster_record, blocks


def _extract_block(
    block_element: ebml.Element,
    data: bytes,
    cluster: ebml.Element,
    cluster_record: dict,
    order: int,
    kind: str,
    group: dict | None,
) -> dict:
    block_data = block_element.data if block_element.data is not None else b""
    parse_error = None
    track_number = relative = flags = None
    lace = None
    try:
        track_number, relative, flags, _frame_payload, lace_decoded = decode_block_payload(block_data)
        lace = lace_decoded.to_dict()
    except VIntError as exc:
        parse_error = str(exc)

    timecode_measure = Measure(
        value=relative,
        unit=TICKS,
        raw=relative,
        basis="signed16:Block-timecode",
        diagnostic=None
        if relative is not None
        else "block timecode unreadable",
    ).to_dict()

    record = {
        "stable_id": f"block@{block_element.header_start}",
        "kind": kind,
        "order_in_cluster": order,
        "track_number": track_number,
        "relative_timecode": timecode_measure,
        "flags": flags,
        "keyframe": bool(flags & 0x80) if flags is not None else None,
        "lacing": lace,
        "header_start": block_element.header_start,
        "data_start": block_element.data_start,
        "raw_range": [block_element.header_start, block_element.end],
        "cluster_header_start": cluster.header_start,
        "cluster_timecode_ticks": cluster_record["timecode_ticks"]["value"],
        "block_group": group,
        "parse_error": parse_error,
    }
    return record


def _extract_cues(cues: ebml.Element, segment_data_start: int) -> list[dict]:
    cue_records: list[dict] = []
    for point in cues.find_all(ebml.CUE_POINT):
        time_element = point.find(ebml.CUE_TIME)
        cue_time = ebml.uint_value(time_element)
        for positions in point.find_all(ebml.CUE_TRACK_POSITIONS):
            track_number = ebml.uint_value(positions.find(ebml.CUE_TRACK))
            cluster_position = ebml.uint_value(positions.find(ebml.CUE_CLUSTER_POSITION))
            relative_position = ebml.uint_value(positions.find(ebml.CUE_RELATIVE_POSITION))
            duration = ebml.uint_value(positions.find(ebml.CUE_DURATION))
            absolute_cluster = (
                segment_data_start + cluster_position
                if cluster_position is not None
                else None
            )
            cue_records.append(
                {
                    "stable_id": f"cue@{point.header_start}:t{track_number}",
                    "cue_time_ticks": Measure(cue_time, TICKS, raw=cue_time,
                                              basis=f"{RAW_INT}:CueTime").to_dict()
                    if cue_time is not None
                    else Measure(None, TICKS, raw=None, diagnostic="missing CueTime").to_dict(),
                    "track_number": track_number,
                    "cluster_position": Measure(
                        cluster_position, SEGMENT_OFFSET, raw=cluster_position,
                        basis=f"{RAW_INT}:CueClusterPosition").to_dict()
                    if cluster_position is not None
                    else None,
                    "absolute_cluster_byte": Measure(absolute_cluster, BYTE_OFFSET,
                                                     raw=absolute_cluster).to_dict()
                    if absolute_cluster is not None
                    else None,
                    "relative_position": relative_position,
                    "duration_ticks": duration,
                    "raw_range": [point.header_start, positions.end],
                }
            )
    return cue_records
