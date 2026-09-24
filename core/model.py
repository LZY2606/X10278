"""Domain objects extracted from an EBML tree.

Objects keep raw quantities with their coordinate bases and stable identities
of the form ``kind:source@byte-offset``. Byte-offset identities survive
export/re-import because the imported bytes are hashed into ``source_uid`` and
single-source mutants used by the case package are written in place (same
length), preserving offsets.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, Any

from . import ebml
from .lacing import decode_block
from .units import q, BYTE_OFFSET, RAW_TICKS, REL_TICKS_SIGNED, NS, \
    SCALE_DIMENSIONLESS


@dataclass
class Anomaly:
    code: str
    message: str
    byte_range: Optional[list[int]]
    object_ids: list[str]
    candidates: list[Any] = field(default_factory=list)
    severity: str = "error"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Track:
    uid: str
    number: int
    track_uid: int
    track_type: Optional[int]
    flag_default: Optional[int]
    default_duration_ns: Optional[int]
    track_timestamp_scale: float
    name: str
    codec_id: str
    pos: int
    data_end: int

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "number": self.number,
            "track_uid": self.track_uid,
            "track_type": self.track_type,
            "flag_default": self.flag_default,
            "default_duration": q(self.default_duration_ns, NS,
                                  self.default_duration_ns).to_dict()
            if self.default_duration_ns is not None else None,
            "track_timestamp_scale": q(self.track_timestamp_scale,
                                       SCALE_DIMENSIONLESS,
                                       self.track_timestamp_scale).to_dict(),
            "name": self.name,
            "codec_id": self.codec_id,
            "byte_range": q([self.pos, self.data_end], BYTE_OFFSET).to_dict(),
        }


@dataclass
class BlockEntity:
    uid: str
    cluster_uid: str
    element: str                      # "Block" or "SimpleBlock"
    track_number: int
    relative_ticks_raw: int
    relative_ticks_signed: int
    keyframe: bool
    block_duration_ticks: Optional[int]
    discard_padding_ns: Optional[int]
    discard_padding_range: Optional[list[int]]
    reference_blocks: list[int]
    pos: int
    data_end: int
    payload_pos: int
    payload_end: int
    lace: dict
    corrupt: bool
    diagnostic: Optional[str]
    corrupt_range: Optional[list[int]]

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "cluster_uid": self.cluster_uid,
            "element": self.element,
            "track_number": self.track_number,
            "relative_timecode": q(self.relative_ticks_signed,
                                   REL_TICKS_SIGNED,
                                   self.relative_ticks_raw).to_dict(),
            "keyframe": self.keyframe,
            "block_duration": q(self.block_duration_ticks, RAW_TICKS,
                                self.block_duration_ticks).to_dict()
            if self.block_duration_ticks is not None else None,
            "discard_padding": q(self.discard_padding_ns, NS,
                                 self.discard_padding_ns).to_dict()
            if self.discard_padding_ns is not None else None,
            "discard_padding_range":
                q(self.discard_padding_range, BYTE_OFFSET).to_dict()
                if self.discard_padding_range else None,
            "reference_blocks": self.reference_blocks,
            "byte_range": q([self.pos, self.data_end], BYTE_OFFSET).to_dict(),
            "payload_range": q([self.payload_pos, self.payload_end],
                               BYTE_OFFSET).to_dict(),
            "lace": self.lace,
            "corrupt": self.corrupt,
            "diagnostic": self.diagnostic,
            "corrupt_range": q(self.corrupt_range, BYTE_OFFSET).to_dict()
            if self.corrupt_range else None,
        }


@dataclass
class Cluster:
    uid: str
    timecode_ticks: int
    pos: int
    data_end: int
    unknown_length: bool
    effective_end: int
    blocks: list[BlockEntity] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "timecode": q(self.timecode_ticks, RAW_TICKS,
                          self.timecode_ticks).to_dict(),
            "byte_range": q([self.pos, self.data_end], BYTE_OFFSET).to_dict(),
            "unknown_length": self.unknown_length,
            "effective_end": self.effective_end,
            "effective_range": q([self.pos, self.effective_end],
                                 BYTE_OFFSET).to_dict(),
            "blocks": [b.to_dict() for b in self.blocks],
        }


@dataclass
class Cue:
    uid: str
    cue_time_ticks: int
    track_number: int
    cluster_position_segrel: Optional[int]
    relative_position_segrel: Optional[int]
    pos: int
    data_end: int

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "cue_time": q(self.cue_time_ticks, RAW_TICKS,
                          self.cue_time_ticks).to_dict(),
            "track_number": self.track_number,
            "cue_cluster_position": q(self.cluster_position_segrel,
                                      "byte-segment-relative",
                                      self.cluster_position_segrel).to_dict()
            if self.cluster_position_segrel is not None else None,
            "cue_relative_position": q(self.relative_position_segrel,
                                       "byte-segment-relative",
                                       self.relative_position_segrel).to_dict()
            if self.relative_position_segrel is not None else None,
            "byte_range": q([self.pos, self.data_end], BYTE_OFFSET).to_dict(),
        }


@dataclass
class MatroskaDoc:
    source_uid: str
    segment_pos: int
    segment_data_pos: int
    segment_data_end: Optional[int]
    segment_unknown_length: bool
    segment_effective_end: int
    timecode_scale_ns: int
    duration_ticks: Optional[float]
    muxing_app: Optional[str]
    writing_app: Optional[str]
    tree: list[dict]
    tracks: list[Track]
    clusters: list[Cluster]
    cues: list[Cue]
    anomalies: list[Anomaly]

    def to_dict(self) -> dict:
        return {
            "source_uid": self.source_uid,
            "segment": {
                "byte_range": q([self.segment_pos, self.segment_data_end],
                                BYTE_OFFSET).to_dict(),
                "data_pos": self.segment_data_pos,
                "unknown_length": self.segment_unknown_length,
                "effective_end": self.segment_effective_end,
            },
            "info": {
                "timecode_scale": q(self.timecode_scale_ns, NS,
                                    self.timecode_scale_ns).to_dict(),
                "duration": q(self.duration_ticks, RAW_TICKS,
                              self.duration_ticks).to_dict()
                if self.duration_ticks is not None else None,
                "muxing_app": self.muxing_app,
                "writing_app": self.writing_app,
            },
            "ebml_tree": self.tree,
            "tracks": [t.to_dict() for t in self.tracks],
            "clusters": [c.to_dict() for c in self.clusters],
            "cues": [c.to_dict() for c in cues_sorted(self.cues)],
            "anomalies": [a.to_dict() for a in self.anomalies],
        }


def cues_sorted(cues: list[Cue]) -> list[Cue]:
    return sorted(cues, key=lambda c: (c.cue_time_ticks, c.track_number))


def extract(data: bytes, roots: list[ebml.Element],
            source_uid: str) -> MatroskaDoc:
    anomalies: list[Anomaly] = []
    segment = next((r for r in roots if r.id == ebml.SEGMENT), None)
    if segment is None:
        raise ebml.EbmlError("no Segment element")
    info_el = ebml.find_first(segment, ebml.INFO)
    tracks_el = ebml.find_first(segment, ebml.TRACKS)
    cues_el = ebml.find_first(segment, ebml.CUES)

    tcs = 1_000_000
    duration = None
    muxing_app = writing_app = None
    if info_el is not None:
        t = ebml.find_first(info_el, ebml.TIMESTAMP_SCALE)
        if t is not None and isinstance(t.value, int):
            tcs = t.value
        d = ebml.find_first(info_el, ebml.DURATION_EL)
        if d is not None:
            duration = d.value
            if d.diagnostic:
                anomalies.append(Anomaly(
                    "info.duration.unconvertible",
                    f"Duration kept raw: {d.diagnostic}",
                    [d.data_pos, d.data_end or len(data)], [],
                    severity="warning"))
        ma = ebml.find_first(info_el, ebml.MUXING_APP)
        if ma is not None:
            muxing_app = ma.value
        wa = ebml.find_first(info_el, ebml.WRITING_APP)
        if wa is not None:
            writing_app = wa.value

    tracks: list[Track] = []
    if tracks_el is not None:
        for te in tracks_el.children:
            if te.id != ebml.TRACK_ENTRY:
                continue
            num_el = ebml.find_first(te, ebml.TRACK_NUMBER)
            uid_el = ebml.find_first(te, ebml.TRACK_UID_EL)
            type_el = ebml.find_first(te, ebml.TRACK_TYPE)
            fd_el = ebml.find_first(te, ebml.FLAG_DEFAULT)
            dd_el = ebml.find_first(te, ebml.DEFAULT_DURATION)
            ts_el = ebml.find_first(te, ebml.TRACK_TIMESTAMP_SCALE)
            name_el = ebml.find_first(te, ebml.NAME)
            codec_el = ebml.find_first(te, ebml.CODEC_ID)
            tracks.append(Track(
                uid=f"trk:{source_uid}@{te.pos}",
                number=(num_el.value if num_el else 0),
                track_uid=(uid_el.value if uid_el else 0),
                track_type=type_el.value if type_el else None,
                flag_default=fd_el.value if fd_el else None,
                default_duration_ns=dd_el.value if dd_el else None,
                track_timestamp_scale=(ts_el.value if ts_el and ts_el.value
                                       is not None else 1.0),
                name=name_el.value if name_el else "",
                codec_id=codec_el.value if codec_el else "",
                pos=te.pos,
                data_end=te.effective_end or te.data_end or te.pos,
            ))

    clusters: list[Cluster] = []
    for ce in segment.children:
        if ce.id != ebml.CLUSTER:
            continue
        tc_el = ebml.find_first(ce, ebml.TIMESTAMP_CLUSTER)
        cluster_tc = tc_el.value if tc_el and tc_el.value is not None else 0
        if tc_el is None:
            anomalies.append(Anomaly(
                "cluster.missing_timecode",
                f"Cluster @{ce.pos} has no Timecode; assuming 0 raw ticks",
                [ce.pos, ce.effective_end],
                [f"clu:{source_uid}@{ce.pos}"], severity="warning"))
        cluster = Cluster(
            uid=f"clu:{source_uid}@{ce.pos}",
            timecode_ticks=cluster_tc,
            pos=ce.pos,
            data_end=(ce.data_end if ce.data_end is not None
                      else ce.effective_end),
            unknown_length=ce.size is None,
            effective_end=ce.effective_end,
        )
        for child in ce.children:
            if child.id == ebml.SIMPLE_BLOCK:
                _add_block(data, child, "SimpleBlock", cluster, source_uid,
                           anomalies)
            elif child.id == ebml.BLOCK_GROUP:
                block_el = ebml.find_first(child, ebml.BLOCK)
                if block_el is not None:
                    _add_block(data, block_el, "Block", cluster, source_uid,
                               anomalies, group=child)
        clusters.append(cluster)

    cues: list[Cue] = []
    if cues_el is not None:
        for cp in cues_el.children:
            if cp.id != ebml.CUE_POINT:
                continue
            ct = ebml.find_first(cp, ebml.CUE_TIME)
            ctp = ebml.find_first(cp, ebml.CUE_TRACK_POSITIONS)
            ctr = ccp = crp = None
            if ctp is not None:
                e = ebml.find_first(ctp, ebml.CUE_TRACK)
                ctr = e.value if e else None
                e = ebml.find_first(ctp, ebml.CUE_CLUSTER_POSITION)
                ccp = e.value if e else None
                e = ebml.find_first(ctp, ebml.CUE_RELATIVE_POSITION)
                crp = e.value if e else None
            cues.append(Cue(
                uid=f"cue:{source_uid}@{cp.pos}",
                cue_time_ticks=ct.value if ct and ct.value is not None else 0,
                track_number=ctr or 0,
                cluster_position_segrel=ccp,
                relative_position_segrel=crp,
                pos=cp.pos,
                data_end=cp.effective_end or cp.data_end or cp.pos,
            ))

    return MatroskaDoc(
        source_uid=source_uid,
        segment_pos=segment.pos,
        segment_data_pos=segment.data_pos,
        segment_data_end=segment.data_end,
        segment_unknown_length=segment.size is None,
        segment_effective_end=segment.effective_end or len(data),
        timecode_scale_ns=tcs,
        duration_ticks=duration,
        muxing_app=muxing_app,
        writing_app=writing_app,
        tree=[r.to_dict() for r in roots],
        tracks=tracks,
        clusters=clusters,
        cues=cues,
        anomalies=anomalies,
    )


def _add_block(data, block_el, kind, cluster: Cluster, source_uid,
               anomalies, group=None):
    dp, de = block_el.data_pos, block_el.data_end
    lace = decode_block(data, dp, de)
    dur_ticks = None
    discard_ns = None
    discard_range = None
    refs: list[int] = []
    if group is not None:
        bd = ebml.find_first(group, ebml.BLOCK_DURATION)
        if bd is not None:
            dur_ticks = bd.value
        dpad = ebml.find_first(group, ebml.DISCARD_PADDING)
        if dpad is not None:
            discard_ns = dpad.value
            discard_range = [dpad.data_pos, dpad.data_end]
        for rb in ebml.find_all(group, ebml.REFERENCE_BLOCK):
            refs.append(rb.value)
    be = BlockEntity(
        uid=f"blk:{source_uid}@{block_el.pos}",
        cluster_uid=cluster.uid,
        element=kind,
        track_number=lace.track_number,
        relative_ticks_raw=lace.relative_ticks_raw,
        relative_ticks_signed=lace.relative_ticks_signed,
        keyframe=lace.keyframe,
        block_duration_ticks=dur_ticks,
        discard_padding_ns=discard_ns,
        discard_padding_range=discard_range,
        reference_blocks=refs,
        pos=block_el.pos,
        data_end=de,
        payload_pos=lace.payload_pos,
        payload_end=lace.payload_end,
        lace=lace.to_dict(),
        corrupt=lace.corrupt,
        diagnostic=lace.diagnostic,
        corrupt_range=lace.corrupt_range,
    )
    cluster.blocks.append(be)
    if lace.corrupt:
        anomalies.append(Anomaly(
            "lacing.corrupt",
            f"{kind} @{block_el.pos}: {lace.diagnostic}; frame boundaries "
            "not guessed",
            lace.corrupt_range, [be.uid, cluster.uid], severity="error"))
