"""Matroska Block / SimpleBlock decoding and lacing rules.

Rule LACING-NO-GUESS (domain rule, see tribunal.rules):
when lacing metadata is inconsistent with the payload we never invent
frame boundaries; the block stays one opaque payload and the damaged
byte range is reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ebml
from .ebml import Element

LACING_NONE = 0
LACING_XIPH = 1
LACING_FIXED = 2
LACING_EBML = 3


class LacingError(Exception):
    pass


@dataclass
class FrameSlice:
    index: int
    offset: int          # relative to block payload
    size: int
    status: str = "ok"   # ok | candidate


@dataclass
class Lacing:
    mode: int
    mode_name: str
    frame_count: int          # total frames (additional + 1)
    frames: list[FrameSlice] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lacing_byte_range: tuple[int, int] | None = None  # offsets in payload
    damaged: bool = False

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "mode_name": self.mode_name,
            "frame_count": self.frame_count,
            "frames": [vars(f) for f in self.frames],
            "errors": list(self.errors),
            "lacing_byte_range": list(self.lacing_byte_range) if self.lacing_byte_range else None,
            "damaged": self.damaged,
        }


def _xiph_sizes(payload: bytes, extra: int) -> tuple[list[int], int]:
    sizes: list[int] = []
    pos = 0
    for _ in range(extra):
        total = 0
        start = pos
        while pos < len(payload):
            byte = payload[pos]
            pos += 1
            if byte != 255:
                total += byte
                break
            total += 255
        else:
            raise LacingError(
                f"xiph lacing size #{len(sizes)} has no terminator byte "
                f"(lacing bytes [{start}, {len(payload)}))")
        sizes.append(total)
    return sizes, pos


def _ebml_sizes(payload: bytes, extra: int) -> tuple[list[int], int]:
    sizes: list[int] = []
    pos = 0
    first, width = ebml.read_vint(payload, pos)
    pos += width
    sizes.append(first)
    prev = first
    for _ in range(extra - 1):
        delta_raw, width = ebml.read_vint(payload, pos, keep_marker=True)
        pos += width
        # keep-marker form: subtract the VINT_MARKER, sign-extend from the
        # top data bit of this width.
        marker = 1 << (8 * width - width)
        unsigned = delta_raw - marker
        if unsigned & (1 << (7 * width - 1)):
            unsigned -= 1 << (7 * width)
        prev += unsigned
        if prev < 0:
            raise LacingError(
                f"ebml lacing frame {len(sizes)} size went negative at byte {pos}")
        sizes.append(prev)
    return sizes, pos


def decode_lacing(payload: bytes, flags: int) -> Lacing:
    mode = (flags >> 1) & 0x3
    names = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}
    lacing = Lacing(mode=mode, mode_name=names[mode], frame_count=1)
    if mode == LACING_NONE or not payload:
        lacing.frames = [FrameSlice(0, 0, len(payload))]
        return lacing
    extra = payload[0]
    total_frames = extra + 1
    lacing.frame_count = total_frames
    header_start = 1
    try:
        if mode == LACING_XIPH:
            sizes, header_end = _xiph_sizes(payload[1:], extra)
            header_end += 1
        elif mode == LACING_FIXED:
            sizes, header_end = [], header_start
        else:
            sizes, header_end = _ebml_sizes(payload[1:], extra)
            header_end += 1

        body = payload[header_end:]
        lacing.lacing_byte_range = (0, header_end)

        if mode == LACING_FIXED:
            if total_frames == 0 or len(body) % total_frames != 0:
                raise LacingError(
                    f"fixed lacing payload {len(body)} bytes not divisible "
                    f"into {total_frames} frames")
            frame_size = len(body) // total_frames
            sizes = [frame_size] * extra
        consumed = sum(sizes)
        last_size = len(body) - consumed
        if last_size < 0:
            raise LacingError(
                f"declared lacing sizes consume {consumed} bytes but payload "
                f"after lacing header is only {len(body)}")
        all_sizes = sizes + [last_size]
        offset = 0
        for i, size in enumerate(all_sizes):
            lacing.frames.append(FrameSlice(i, header_end + offset, size))
            offset += size
        if offset != len(body):
            raise LacingError(
                f"lacing frame sizes total {offset} but body is {len(body)} "
                "(trailing bytes)")
    except (LacingError, ebml.EbmlError) as exc:
        lacing.damaged = True
        lacing.errors.append(str(exc))
        # LACING-NO-GUESS: no frame boundaries are emitted on damage.
        lacing.frames = []
        lacing.frame_count = 0 if mode == LACING_FIXED else 0
    return lacing


@dataclass
class Block:
    block_id: str
    element: Element
    kind: str            # "SimpleBlock" | "Block"
    track_number: int
    rel_ticks_raw: bytes
    rel_ticks_signed: int
    flags: int
    payload: bytes
    lacing: Lacing
    keyframe: bool
    invisible: bool
    byte_range: tuple[int, int]
    payload_range: tuple[int, int]
    discard_padding_ns: int | None = None
    duration_ticks: int | None = None
    reference_blocks: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "block_id": self.block_id,
            "kind": self.kind,
            "track_number": self.track_number,
            "rel_ticks_raw_hex": self.rel_ticks_raw.hex(),
            "rel_ticks_signed": self.rel_ticks_signed,
            "flags": self.flags,
            "payload_hex": self.payload.hex(),
            "lacing": self.lacing.as_dict(),
            "keyframe": self.keyframe,
            "invisible": self.invisible,
            "byte_range": list(self.byte_range),
            "payload_range": list(self.payload_range),
            "discard_padding_ns": self.discard_padding_ns,
            "duration_ticks": self.duration_ticks,
            "reference_blocks": list(self.reference_blocks),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Block":
        lacing = Lacing(
            mode=data["lacing"]["mode"],
            mode_name=data["lacing"]["mode_name"],
            frame_count=data["lacing"]["frame_count"],
            frames=[FrameSlice(**f) for f in data["lacing"]["frames"]],
            errors=list(data["lacing"]["errors"]),
            lacing_byte_range=tuple(data["lacing"]["lacing_byte_range"])
            if data["lacing"]["lacing_byte_range"] else None,
            damaged=data["lacing"]["damaged"],
        )
        return cls(
            block_id=data["block_id"], element=None, kind=data["kind"],
            track_number=data["track_number"],
            rel_ticks_raw=bytes.fromhex(data["rel_ticks_raw_hex"]),
            rel_ticks_signed=data["rel_ticks_signed"], flags=data["flags"],
            payload=bytes.fromhex(data["payload_hex"]), lacing=lacing,
            keyframe=data["keyframe"], invisible=data["invisible"],
            byte_range=tuple(data["byte_range"]),
            payload_range=tuple(data["payload_range"]),
            discard_padding_ns=data["discard_padding_ns"],
            duration_ticks=data["duration_ticks"],
            reference_blocks=list(data["reference_blocks"]),
        )


def decode_block(el: Element, kind: str, cluster_offset: int,
                 block_index: int) -> Block:
    body = el.raw
    track, tw = ebml.read_vint(body, 0)
    rel_raw = body[tw:tw + 2]
    rel_signed = ebml.signed_int_width(rel_raw)
    flags = body[tw + 2]
    payload = body[tw + 3:]
    lacing = decode_lacing(payload, flags)
    block_id = f"cluster@{cluster_offset}#{kind.lower()}:{block_index}"
    return Block(
        block_id=block_id, element=el, kind=kind, track_number=track,
        rel_ticks_raw=rel_raw, rel_ticks_signed=rel_signed, flags=flags,
        payload=payload, lacing=lacing,
        keyframe=bool(flags & 0x80), invisible=bool(flags & 0x08),
        byte_range=(el.id_offset, el.end_offset),
        payload_range=(el.data_offset + tw + 3, el.data_offset + len(body)),
    )


def attach_blockgroup_details(block: Block, group: Element) -> None:
    dp = group.get_child("DiscardPadding")
    if dp is not None:
        block.discard_padding_ns = ebml.decode_sint(dp)
    dur = group.get_child("BlockDuration")
    if dur is not None:
        block.duration_ticks = ebml.decode_uint(dur)
    for ref in group.find_all("ReferenceBlock"):
        block.reference_blocks.append(ebml.decode_sint(ref))
