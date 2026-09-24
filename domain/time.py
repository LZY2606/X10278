"""Time normalisation, multi-track timelines, gaps and cue evidence.

Layering principle: every domain quantity carries its unit / coordinate basis.
Raw values are preserved verbatim; only the derived layer normalises to
nanoseconds. Values that cannot be converted stay in place with a diagnostic
and are never dropped just to draw a picture.

Timeline semantics:
* relative timecode uses the fixed signed width;
* each track keeps its own time base (TimecodeScale * TrackTimecodeScale plus
  TrackOffset); tracks are NOT force-aligned by file order;
* a Cue is indexing evidence, never time truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field

NS_PER_MS = 1_000_000
DEFAULT_TIMECODE_SCALE = 1_000_000  # ns per raw scale tick (=> 1 ms ticks)


@dataclass
class Quantity:
    raw: object
    unit: str
    basis: str
    ns: float | None = None
    diagnostic: str | None = None

    def to_dict(self) -> dict:
        return {
            "raw": self.raw, "unit": self.unit, "basis": self.basis,
            "ns": self.ns, "diagnostic": self.diagnostic,
        }


def scale_tick_ns(timecode_scale: int, track_tc_scale: float = 1.0) -> float:
    return float(timecode_scale) * float(track_tc_scale)


def raw_to_ns(raw_ticks: int, timecode_scale: int,
              track_tc_scale: float = 1.0, track_offset: int = 0) -> dict:
    """Return a Quantity bundle preserving raw representation."""
    tick_ns = scale_tick_ns(timecode_scale, track_tc_scale)
    value_ticks = raw_ticks + track_offset
    return Quantity(
        raw=raw_ticks,
        unit="raw-ticks",
        basis=f"TimecodeScale={timecode_scale}ns "
              f"TrackTimecodeScale={track_tc_scale} TrackOffset={track_offset}",
        ns=value_ticks * tick_ns,
    ).to_dict()


@dataclass
class Gap:
    track: int
    prev_end_ns: float | None
    next_start_ns: float | None
    gap_ns: float | None
    cluster_a_start: int
    cluster_b_start: int
    kind: str = "midstream-cluster-gap"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def detect_gaps(track_points: list[dict], tolerance_ns: float = 1.0) -> list[Gap]:
    """track_points: sorted list of {track,start_ns,end_ns,cluster_start}."""
    gaps: list[Gap] = []
    points = sorted(track_points, key=lambda p: p["start_ns"])
    prev = None
    for cur in points:
        if cur["start_ns"] is None:
            continue
        if prev is not None and prev["end_ns"] is not None:
            delta = cur["start_ns"] - prev["end_ns"]
            if delta > tolerance_ns:
                gaps.append(Gap(
                    track=cur["track"], prev_end_ns=prev["end_ns"],
                    next_start_ns=cur["start_ns"], gap_ns=delta,
                    cluster_a_start=prev["cluster_start"],
                    cluster_b_start=cur["cluster_start"],
                ))
        prev = cur
    return gaps


def cue_consistency(cue_time_raw: int, cue_cluster_pos: int,
                    cluster_pos_actual: int, cluster_time_raw: int,
                    timecode_scale: int) -> dict:
    """Compare cue indexing evidence against actual cluster coordinates."""
    pos_match = cue_cluster_pos == cluster_pos_actual
    time_match = cue_time_raw == cluster_time_raw
    return {
        "cue_time": cue_time_raw,
        "declared_cluster_position": cue_cluster_pos,
        "actual_cluster_position": cluster_pos_actual,
        "cluster_time_raw": cluster_time_raw,
        "position_match": pos_match,
        "time_match": time_match,
        "evidence_only": True,
        "diagnostic": None if (pos_match and time_match) else
        ("cue points to wrong byte position" if not pos_match
         else "cue time differs from cluster Timecode (index evidence only)"),
        "cue_time_ns": cue_time_raw * timecode_scale,
        "cluster_time_ns": cluster_time_raw * timecode_scale,
    }
