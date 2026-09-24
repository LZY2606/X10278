"""EBML primitives: VINT coding, element parsing with raw byte ranges.

Every parsed element keeps its exact byte range in the source so the
review UI can always point back at the original bytes.  Unknown-length
(streaming) elements are first-class: their data size stays ``None``
and carries a diagnostic instead of being guessed.
"""
from __future__ import annotations


class EbmlError(Exception):
    """Raised when bytes cannot be interpreted as EBML."""


def read_vint(data: bytes, pos: int, strip_marker: bool = True):
    """Read a variable-length integer at ``pos``.

    Returns ``(value, length, unknown)``.  With ``strip_marker`` the
    length-marker bit is removed (size semantics); otherwise it is kept
    (element-ID semantics).  ``unknown`` is True when every data bit is
    set, which EBML reserves for "unknown size".
    """
    if pos >= len(data):
        raise EbmlError(f"vint: end of data at {pos}")
    first = data[pos]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise EbmlError(f"vint: no marker bit at {pos}")
    if pos + length > len(data):
        raise EbmlError(f"vint: truncated at {pos}")
    value = (first & (mask - 1)) if strip_marker else first
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    unknown = value == (1 << (7 * length)) - 1
    return value, length, unknown


def write_vint(value: int, length: int | None = None, marker: bool = True) -> bytes:
    """Encode ``value`` as a VINT.  ``length`` forces the encoded width."""
    if length is None:
        length = 1
        while value >= (1 << (7 * length)) - (1 if marker else 0):
            length += 1
    out = value | (1 << (7 * length)) if marker else value
    return out.to_bytes(length, "big")


def write_id(id_value: int) -> bytes:
    """Encode an element ID (marker bit already part of the ID)."""
    length = 1
    while id_value >= (1 << (8 * length)):
        length += 1
    return id_value.to_bytes(length, "big")


def write_size(value: int | None, length: int | None = None) -> bytes:
    """Encode an element data size; ``None`` means unknown length."""
    if value is None:
        length = length or 1
        if length == 1:
            return b"\xff"
        marker = 1 << (8 - length)
        return bytes([marker - 1]) + b"\xff" * (length - 1)
    return write_vint(value, length)


ELEMENT_NAMES = {
    0x1A45DFA3: "EBML",
    0x4286: "EBMLVersion",
    0x42F7: "EBMLReadVersion",
    0x42F2: "EBMLMaxIDLength",
    0x42F3: "EBMLMaxSizeLength",
    0x4282: "DocType",
    0x4287: "DocTypeVersion",
    0x4285: "DocTypeReadVersion",
    0x18538067: "Segment",
    0x1549A966: "Info",
    0x2AD7B1: "TimecodeScale",
    0x4489: "Duration",
    0x1654AE6B: "Tracks",
    0xAE: "TrackEntry",
    0xD7: "TrackNumber",
    0x73C5: "TrackUID",
    0x83: "TrackType",
    0x536E: "Name",
    0x86: "CodecID",
    0x23E383: "DefaultDuration",
    0x1C53BB6B: "Cues",
    0xBB: "CuePoint",
    0xB7: "CueTime",
    0xB3: "CueTrackPositions",
    0xF7: "CueTrack",
    0xF1: "CueClusterPosition",
    0x1F43B675: "Cluster",
    0xE7: "Timecode",
    0xA3: "SimpleBlock",
    0xA0: "BlockGroup",
    0xA1: "Block",
    0x9B: "BlockDuration",
    0x75A2: "DiscardPadding",
    0xEC: "Void",
    0xBF: "CRC-32",
}

MASTER_IDS = {0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE,
              0x1C53BB6B, 0xBB, 0xB3, 0x1F43B675, 0xA0}


def element_name(id_value: int) -> str:
    return ELEMENT_NAMES.get(id_value, f"Unknown(0x{id_value:x})")


class Element:
    """One EBML element with its exact source byte range."""

    def __init__(self, id_value, offset, header_size, data_size, depth, order):
        self.id = id_value
        self.offset = offset
        self.header_size = header_size
        self.data_size = data_size
        self.depth = depth
        self.order = order
        self.children: list[Element] = []
        self.truncated = False

    @property
    def name(self) -> str:
        return element_name(self.id)

    @property
    def data_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def end(self):
        if self.data_size is None:
            return None
        return self.data_offset + self.data_size

    @property
    def uid(self) -> str:
        return f"el{self.order}@{self.offset}x{self.id:x}"

    def raw_range(self):
        return (self.offset, self.end)


class Parser:
    def __init__(self, data: bytes):
        self.data = data
        self.counter = 0
        self.diagnostics: list[dict] = []

    def parse_element(self, pos: int, limit: int, depth: int) -> Element:
        id_value, id_len, _ = read_vint(self.data, pos, strip_marker=False)
        size, size_len, unknown = read_vint(self.data, pos + id_len)
        el = Element(
            id_value=id_value,
            offset=pos,
            header_size=id_len + size_len,
            data_size=None if unknown else size,
            depth=depth,
            order=self.counter,
        )
        self.counter += 1
        if unknown:
            self.diagnostics.append({
                "element": el.uid,
                "kind": "unknown_length",
                "detail": f"{el.name} at byte {pos} declares unknown length; "
                          f"extent inferred from parent, raw bytes retained",
            })
        if el.id in MASTER_IDS:
            child_limit = el.end if el.end is not None else limit
            child_limit = min(child_limit, len(self.data))
            self._parse_children(el, child_limit)
        return el

    def _parse_children(self, parent: Element, limit: int) -> None:
        pos = parent.data_offset
        while pos < limit:
            try:
                child = self.parse_element(pos, limit, parent.depth + 1)
            except EbmlError as exc:
                parent.truncated = True
                self.diagnostics.append({
                    "element": parent.uid,
                    "kind": "parse_stop",
                    "detail": f"stopped at byte {pos}: {exc}; remaining bytes kept raw",
                })
                break
            parent.children.append(child)
            end = child.end
            if end is None:
                if child.children:
                    last_end = child.children[-1].end
                    pos = last_end if last_end is not None else limit
                else:
                    pos = limit
            else:
                pos = end

    def parse(self) -> list[Element]:
        roots: list[Element] = []
        pos = 0
        limit = len(self.data)
        while pos < limit:
            try:
                el = self.parse_element(pos, limit, 0)
            except EbmlError as exc:
                self.diagnostics.append({
                    "element": None,
                    "kind": "parse_stop",
                    "detail": f"top-level stop at byte {pos}: {exc}",
                })
                break
            roots.append(el)
            end = el.end
            if end is None:
                break
            pos = end
        return roots


def parse(data: bytes):
    parser = Parser(data)
    roots = parser.parse()
    return roots, parser.diagnostics


def iter_elements(roots):
    stack = list(reversed(roots))
    while stack:
        el = stack.pop()
        yield el
        stack.extend(reversed(el.children))


def find_all(roots, id_value):
    return [el for el in iter_elements(roots) if el.id == id_value]


def element_bytes(data: bytes, el: Element) -> bytes:
    """Raw payload bytes of an element (never truncated silently)."""
    end = el.end
    if end is None:
        end = len(data)
    return data[el.data_offset:end]
