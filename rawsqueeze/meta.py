"""Metadata skeleton (META chunk), embedded previews and EXIF transfer (DESIGN.md 3.4, 3.5.4).

Skeleton strategy
-----------------
The META payload is the part of the original raw file that precedes the raw pixel data
(``data[0:raw_start]``).  Embedded JPEG previews inside it (RW2: JpgFromRaw, JpgFromRaw2)
are *scan-stripped*: every JPEG marker segment (APPn with EXIF/MakerNotes/GPS, DQT, DHT,
SOF, SOS headers, RSTn, EOI) is kept and only the entropy-coded scan bytes are zeroed.
On RW2 the complete EXIF, Panasonic MakerNotes and GPS live in the APP1 of the embedded
JpgFromRaw, so zeroing the whole JPEG would lose them.

Raw data location: RW2 uses ``RawDataOffset`` (tag 0x0118 in IFD0, or the value from
exiftool); other TIFF-like raws (NEF/CR2/ARW/ORF/PEF/DNG) use the raw IFD's
StripOffsets/StripByteCounts or TileOffsets/TileByteCounts.  Those intervals are zeroed
(or, when the raw data is the tail of the file, the file is truncated there).

The payload returned by :func:`make_skeleton` is *uncompressed*; the container stores
META with zstd-19 (``container.make_chunk("META", payload)``).  Use
:func:`compressed_size` to measure the stored size.

Fallback (``strategy="exif-only"``): if the raw data cannot be located, the payload is an
``exiftool -j -b`` JSON dump plus an ``exiftool -o x.exif`` blob, framed as::

    b"RSQMEXIF" | u32 json_len | json | u32 exif_len | exif

(:func:`parse_meta` splits either form).
"""

from __future__ import annotations

import os
import struct
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import zstandard

from .tools import ExifTool, ToolError, have, run

EXIF_ONLY_MAGIC = b"RSQMEXIF"
ZSTD_LEVEL = 19

# TIFF field types -> byte size
_TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}

# Tags of interest
_T_NEWSUBFILETYPE = 254
_T_WIDTH = 256
_T_COMPRESSION = 259
_T_PHOTOMETRIC = 262
_T_STRIP_OFFSETS = 273
_T_STRIP_BYTES = 279
_T_TILE_OFFSETS = 324
_T_TILE_BYTES = 325
_T_SUBIFDS = 330
_T_JPEG_IF = 513
_T_JPEG_IF_LEN = 514
_T_EXIF_IFD = 34665
_T_RW2_RAW_DATA_OFFSET = 0x0118
_T_RW2_JPG_FROM_RAW = 0x002E
_T_RW2_JPG_FROM_RAW2 = 0x0127

_RW2_PREVIEW_TAGS = {_T_RW2_JPG_FROM_RAW: "JpgFromRaw", _T_RW2_JPG_FROM_RAW2: "JpgFromRaw2"}
_PREVIEW_FOURCC = {"JpgFromRaw": "PRV0", "JpgFromRaw2": "PRV1"}


class MetaError(ValueError):
    """Raw layout could not be determined."""


# ---------------------------------------------------------------------------------------
# minimal TIFF parser


@dataclass
class TiffEntry:
    tag: int
    type: int
    count: int
    data_offset: int
    """Absolute file offset of the value bytes (inline values point into the IFD entry)."""
    size: int
    """Value size in bytes (count * type size)."""


@dataclass
class TiffIFD:
    offset: int
    kind: str
    """'IFD0', 'IFD1', ..., 'SubIFD', 'ExifIFD'."""
    entries: dict[int, TiffEntry] = field(default_factory=dict)


@dataclass
class TiffInfo:
    byteorder: str
    """'<' or '>'."""
    magic: int
    """42 (TIFF), 0x55 (RW2), 0x4F52/0x5352 (ORF) ..."""
    ifds: list[TiffIFD]

    def values(self, data: bytes, entry: TiffEntry) -> list[int]:
        """Integer values of a BYTE/SHORT/LONG/LONG8/IFD entry."""
        fmt = {1: "B", 3: "H", 4: "I", 7: "B", 13: "I", 16: "Q", 8: "h", 9: "i", 6: "b"}.get(entry.type)
        if fmt is None:
            raise MetaError(f"tag {entry.tag} has non-integer type {entry.type}")
        if entry.data_offset + entry.size > len(data):
            raise MetaError(f"tag {entry.tag} value runs past end of file")
        return list(struct.unpack_from(f"{self.byteorder}{entry.count}{fmt}", data, entry.data_offset))


def parse_tiff(data: bytes, *, max_ifds: int = 64) -> TiffInfo:
    """Parse the IFD structure of a TIFF-like file (TIFF, DNG, RW2, ORF, NEF, CR2, ARW, PEF).

    Follows the IFD0 chain, SubIFDs and the ExifIFD.  Raises :class:`MetaError` if the
    header is not TIFF-like.  BigTIFF is not supported.
    """
    if len(data) < 8:
        raise MetaError("file too small for a TIFF header")
    if data[:2] == b"II":
        bo = "<"
    elif data[:2] == b"MM":
        bo = ">"
    else:
        raise MetaError("not a TIFF-like file (byte order mark missing)")
    magic, first = struct.unpack_from(f"{bo}HI", data, 2)
    if magic == 43:
        raise MetaError("BigTIFF is not supported")
    if magic not in (42, 0x55, 0x4F52, 0x5352):
        raise MetaError(f"unknown TIFF magic 0x{magic:04x}")
    ifds: list[TiffIFD] = []
    seen: set[int] = set()
    queue: list[tuple[int, str, bool]] = [(first, "IFD", True)]
    chain_index = 0
    while queue and len(ifds) < max_ifds:
        off, kind, is_chain = queue.pop(0)
        while off and off not in seen and len(ifds) < max_ifds:
            if off + 2 > len(data):
                raise MetaError(f"IFD offset {off} past end of file")
            seen.add(off)
            (n,) = struct.unpack_from(f"{bo}H", data, off)
            if n == 0 or off + 2 + 12 * n + 4 > len(data):
                raise MetaError(f"corrupt IFD at {off} ({n} entries)")
            name = f"IFD{chain_index}" if is_chain else kind
            if is_chain:
                chain_index += 1
            ifd = TiffIFD(offset=off, kind=name)
            for k in range(n):
                eo = off + 2 + 12 * k
                tag, typ, cnt = struct.unpack_from(f"{bo}HHI", data, eo)
                tsz = _TIFF_TYPE_SIZE.get(typ, 1)
                size = tsz * cnt
                if size <= 4:
                    doff = eo + 8
                else:
                    (doff,) = struct.unpack_from(f"{bo}I", data, eo + 8)
                ifd.entries[tag] = TiffEntry(tag, typ, cnt, doff, size)
            ifds.append(ifd)
            info = TiffInfo(bo, magic, [])
            for sub_tag, sub_kind in ((_T_SUBIFDS, "SubIFD"), (_T_EXIF_IFD, "ExifIFD")):
                e = ifd.entries.get(sub_tag)
                if e is not None and e.type in (4, 13) and e.data_offset + e.size <= len(data):
                    for so in info.values(data, e):
                        if 8 <= so < len(data):
                            queue.append((so, sub_kind, False))
            (nxt,) = struct.unpack_from(f"{bo}I", data, off + 2 + 12 * n)
            off = nxt if is_chain and 8 <= nxt < len(data) else 0
    if not ifds:
        raise MetaError("no IFD found")
    return TiffInfo(bo, magic, ifds)


# ---------------------------------------------------------------------------------------
# raw layout


@dataclass
class PreviewRef:
    tag: str
    """'JpgFromRaw', 'JpgFromRaw2', 'PreviewImage', 'ThumbnailImage', 'PreviewStrip' ..."""
    offset: int
    length: int


@dataclass
class RawLayout:
    fmt: str
    """'rw2' | 'tiff'."""
    raw_regions: list[tuple[int, int]]
    """(offset, length) of raw pixel data intervals."""
    previews: list[PreviewRef]
    method: str
    """How the raw data was located: 'RawDataOffset' | 'StripOffsets' | 'TileOffsets'."""


def _is_jpeg(data: bytes, off: int) -> bool:
    return data[off : off + 3] == b"\xff\xd8\xff"


def _jpeg_is_lossless(data: bytes, off: int, length: int) -> bool:
    """True if the JPEG at ``off`` uses a lossless SOF (SOF3/7/11/15), i.e. it is raw data."""
    i = off + 2
    end = min(off + length, len(data), off + 65536 * 4)
    while i + 4 <= end:
        if data[i] != 0xFF:
            return False
        mk = data[i + 1]
        if mk == 0xFF:
            i += 1
            continue
        if mk in (0xC3, 0xC7, 0xCB, 0xCF):
            return True
        if mk in (0xC0, 0xC1, 0xC2, 0xC5, 0xC6, 0xC9, 0xCA, 0xCD, 0xCE, 0xDA):
            return False
        if mk == 0xD8 or 0xD0 <= mk <= 0xD7 or mk == 0x01:
            i += 2
            continue
        (L,) = struct.unpack_from(">H", data, i + 2)
        i += 2 + L
    return False


def locate_raw_layout(data: bytes, *, raw_data_offset: int | None = None) -> RawLayout:
    """Find the raw pixel data intervals and embedded JPEG previews of a TIFF-like raw.

    RW2 (TIFF magic 0x55): raw data = ``[RawDataOffset, EOF)`` (``raw_data_offset``
    overrides tag 0x0118); previews = tags 0x002E (JpgFromRaw) and 0x0127 (JpgFromRaw2).
    Other TIFF-like files: the raw IFD is the one with NewSubfileType 0 and CFA/LinearRaw
    photometric, else the IFD with the largest strip/tile payload that is not a baseline
    JPEG preview.  Raises :class:`MetaError` when nothing can be located.
    """
    info = parse_tiff(data)
    n = len(data)
    previews: list[PreviewRef] = []

    # Embedded JPEG blobs stored as UNDEFINED/BYTE tag values (RW2 JpgFromRaw*, etc.).
    for ifd in info.ifds:
        for tag, e in ifd.entries.items():
            if e.type in (1, 7) and e.size >= 1024 and e.data_offset + e.size <= n and _is_jpeg(data, e.data_offset):
                name = _RW2_PREVIEW_TAGS.get(tag, f"Tag0x{tag:04x}") if info.magic == 0x55 else f"Tag0x{tag:04x}"
                previews.append(PreviewRef(name, e.data_offset, e.size))
        jo, jl = ifd.entries.get(_T_JPEG_IF), ifd.entries.get(_T_JPEG_IF_LEN)
        if jo is not None and jl is not None:
            o, ln = info.values(data, jo)[0], info.values(data, jl)[0]
            if ln > 0 and o + ln <= n and _is_jpeg(data, o):
                previews.append(PreviewRef("ThumbnailImage" if ifd.kind == "IFD1" else "PreviewImage", o, ln))

    if info.magic == 0x55:
        rdo = raw_data_offset
        if rdo is None:
            e = info.ifds[0].entries.get(_T_RW2_RAW_DATA_OFFSET)
            if e is None:
                e = info.ifds[0].entries.get(_T_STRIP_OFFSETS)
            if e is None:
                raise MetaError("RW2 without RawDataOffset/StripOffsets")
            rdo = info.values(data, e)[0]
        if not 8 <= rdo <= n:
            raise MetaError(f"RawDataOffset {rdo} outside file (size {n})")
        prev = [p for p in previews if p.offset + p.length <= rdo]
        return RawLayout("rw2", [(int(rdo), n - int(rdo))], _dedupe(prev), "RawDataOffset")

    # Generic TIFF: collect strip/tile data per IFD.
    cands: list[tuple[int, int, TiffIFD, list[tuple[int, int]], str]] = []
    for ifd in info.ifds:
        for to, tb, meth in ((_T_STRIP_OFFSETS, _T_STRIP_BYTES, "StripOffsets"), (_T_TILE_OFFSETS, _T_TILE_BYTES, "TileOffsets")):
            eo, eb = ifd.entries.get(to), ifd.entries.get(tb)
            if eo is None or eb is None:
                continue
            offs, lens = info.values(data, eo), info.values(data, eb)
            regs = [(int(o), int(ln)) for o, ln in zip(offs, lens) if ln > 0 and o + ln <= n]
            if not regs:
                continue
            total = sum(ln for _, ln in regs)
            comp = info.values(data, ifd.entries[_T_COMPRESSION])[0] if _T_COMPRESSION in ifd.entries else 1
            phot = info.values(data, ifd.entries[_T_PHOTOMETRIC])[0] if _T_PHOTOMETRIC in ifd.entries else -1
            nsf = info.values(data, ifd.entries[_T_NEWSUBFILETYPE])[0] if _T_NEWSUBFILETYPE in ifd.entries else -1
            first = regs[0][0]
            baseline_jpeg = comp in (6, 7) and _is_jpeg(data, first) and not _jpeg_is_lossless(data, first, regs[0][1])
            if baseline_jpeg and phot != 32803:
                if len(regs) == 1:
                    previews.append(PreviewRef(f"PreviewStrip{ifd.kind}", first, regs[0][1]))
                continue
            score = 2 if (nsf == 0 and phot in (32803, 34892)) else (1 if phot in (32803, 34892) else 0)
            cands.append((score, total, ifd, regs, meth))
    if not cands:
        raise MetaError("no raw strip/tile data found")
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _, _, _, regs, meth = cands[0]
    regs_sorted = sorted(regs)
    raw_set = [(o, o + ln) for o, ln in regs_sorted]
    prev = [p for p in previews if not any(a < p.offset + p.length and p.offset < b for a, b in raw_set)]
    return RawLayout("tiff", regs_sorted, _dedupe(prev), meth)


def _dedupe(prev: list[PreviewRef]) -> list[PreviewRef]:
    out: list[PreviewRef] = []
    seen: set[tuple[int, int]] = set()
    for p in sorted(prev, key=lambda p: p.offset):
        if (p.offset, p.length) not in seen:
            seen.add((p.offset, p.length))
            out.append(p)
    return out


# ---------------------------------------------------------------------------------------
# JPEG scan stripping


@dataclass
class JpegStripInfo:
    header_bytes: int
    """Bytes before the first entropy-coded byte (end of the first SOS header)."""
    n_scans: int
    zeroed: int
    """Number of entropy-coded bytes set to zero."""
    n_rst: int
    trailing: int
    """Bytes after the final EOI that were kept unchanged (0 if none)."""
    n_images: int = 1


def _marker_positions(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(positions of real markers, positions of RSTn markers) inside entropy data.

    A real marker is 0xFF followed by a byte that is not 0x00 (stuffing), not 0xFF (fill)
    and not 0xD0..0xD7 (RSTn).
    """
    if arr.size < 2:
        e = np.zeros(0, dtype=np.int64)
        return e, e
    ff = np.flatnonzero(arr[:-1] == 0xFF)
    nxt = arr[ff + 1]
    rst = (nxt >= 0xD0) & (nxt <= 0xD7)
    real = (nxt != 0x00) & (nxt != 0xFF) & ~rst
    return ff[real], ff[rst]


def jpeg_strip_scan(b: bytes | bytearray) -> tuple[bytearray, JpegStripInfo]:
    """Zero the entropy-coded scan data of a JPEG, keeping every marker segment.

    Handles fill bytes (0xFF runs before a marker), parameterless markers (SOI, TEM, RSTn),
    multiple SOS segments (progressive / multi-scan), byte stuffing (FF 00), and data
    after EOI (a following JPEG, e.g. MPF, is stripped recursively; other trailing bytes
    are kept).  RSTn markers inside scans are kept.  Raises ValueError if the stream is
    not a parseable JPEG.  Returns ``(stripped, info)``; the length is unchanged.
    """
    out = bytearray(b)
    n = len(out)
    if n < 4 or out[0] != 0xFF or out[1] != 0xD8:
        raise ValueError("not a JPEG (missing SOI)")
    arr = np.frombuffer(bytes(out), dtype=np.uint8)
    markers, rsts = _marker_positions(arr)
    i = 0
    header_bytes = -1
    n_scans = zeroed = n_rst = 0
    n_images = 1
    trailing = 0
    while i < n:
        if out[i] != 0xFF:
            raise ValueError(f"expected marker at offset {i}, got 0x{out[i]:02x}")
        # skip fill bytes
        while i + 1 < n and out[i + 1] == 0xFF:
            i += 1
        if i + 1 >= n:
            raise ValueError("truncated JPEG (marker at end)")
        mk = out[i + 1]
        if mk == 0xD8 or mk == 0x01 or 0xD0 <= mk <= 0xD7:
            i += 2
            continue
        if mk == 0xD9:  # EOI
            i += 2
            if i < n:
                j = i
                while j < n and out[j] == 0x00:
                    j += 1
                if j + 3 <= n and out[j : j + 3] == b"\xff\xd8\xff":
                    sub, sinfo = jpeg_strip_scan(out[j:])
                    out[j:] = sub
                    zeroed += sinfo.zeroed
                    n_scans += sinfo.n_scans
                    n_rst += sinfo.n_rst
                    n_images += sinfo.n_images
                    trailing = sinfo.trailing
                else:
                    trailing = n - i
            break
        if i + 4 > n:
            raise ValueError("truncated JPEG (segment length)")
        L = (out[i + 2] << 8) | out[i + 3]
        if L < 2:
            raise ValueError(f"invalid segment length {L} at {i}")
        seg_end = i + 2 + L
        if seg_end > n:
            raise ValueError("truncated JPEG (segment past end)")
        if mk != 0xDA:
            i = seg_end
            continue
        # SOS: entropy-coded data until the next real marker
        n_scans += 1
        if header_bytes < 0:
            header_bytes = seg_end
        k = int(np.searchsorted(markers, seg_end))
        nxt = int(markers[k]) if k < markers.size else n
        # step back over fill bytes preceding the marker
        while nxt > seg_end and out[nxt - 1] == 0xFF:
            nxt -= 1
        lo, hi = int(np.searchsorted(rsts, seg_end)), int(np.searchsorted(rsts, nxt))
        keep = rsts[lo:hi]
        out[seg_end:nxt] = bytes(nxt - seg_end)
        for r in keep.tolist():
            if r + 1 < nxt:
                out[r] = 0xFF
                out[r + 1] = arr[r + 1]
        n_rst += len(keep)
        zeroed += (nxt - seg_end) - 2 * len(keep)
        if nxt >= n:
            break
        i = nxt
    if n_scans == 0:
        raise ValueError("JPEG has no SOS segment")
    return out, JpegStripInfo(max(header_bytes, 0), n_scans, zeroed, n_rst, trailing, n_images)


def jpeg_markers(b: bytes | bytearray) -> list[tuple[int, int, bytes]]:
    """List ``(offset, marker, segment_bytes)`` of all marker segments outside entropy data.

    Parameterless markers have empty segment bytes; scan data is skipped.  Used to check
    that :func:`jpeg_strip_scan` preserved all markers.
    """
    arr = np.frombuffer(bytes(b), dtype=np.uint8)
    markers, _ = _marker_positions(arr)
    n = len(b)
    out: list[tuple[int, int, bytes]] = []
    i = 0
    while i < n:
        if b[i] != 0xFF:
            break
        while i + 1 < n and b[i + 1] == 0xFF:
            i += 1
        if i + 1 >= n:
            break
        mk = b[i + 1]
        if mk == 0xD8 or mk == 0x01 or 0xD0 <= mk <= 0xD7:
            out.append((i, mk, b""))
            i += 2
            continue
        if mk == 0xD9:
            out.append((i, mk, b""))
            i += 2
            while i < n and b[i] == 0x00:
                i += 1
            continue
        L = (b[i + 2] << 8) | b[i + 3]
        seg_end = i + 2 + L
        out.append((i, mk, bytes(b[i:seg_end])))
        if mk == 0xDA:
            k = int(np.searchsorted(markers, seg_end))
            nxt = int(markers[k]) if k < markers.size else n
            while nxt > seg_end and b[nxt - 1] == 0xFF:
                nxt -= 1
            i = nxt
        else:
            i = seg_end
    return out


# ---------------------------------------------------------------------------------------
# skeleton


@dataclass
class MetaBlob:
    strategy: str
    """'skeleton' | 'exif-only' | 'none'."""
    skeleton: bytes = b""
    exif_json: bytes = b""
    exif_blob: bytes = b""


def compressed_size(payload: bytes, level: int = ZSTD_LEVEL) -> int:
    """Size of ``payload`` after zstd at ``level`` (what the container stores for META)."""
    return len(zstandard.ZstdCompressor(level=level).compress(payload))


def pack_exif_only(exif_json: bytes, exif_blob: bytes) -> bytes:
    return EXIF_ONLY_MAGIC + struct.pack("<I", len(exif_json)) + exif_json + struct.pack("<I", len(exif_blob)) + exif_blob


def parse_meta(payload: bytes) -> MetaBlob:
    """Split a META payload into a :class:`MetaBlob` (strategy detected from the bytes)."""
    if not payload:
        return MetaBlob("none")
    if payload.startswith(EXIF_ONLY_MAGIC):
        o = len(EXIF_ONLY_MAGIC)
        (lj,) = struct.unpack_from("<I", payload, o)
        o += 4
        js = payload[o : o + lj]
        o += lj
        (le,) = struct.unpack_from("<I", payload, o)
        o += 4
        ex = payload[o : o + le]
        if len(ex) != le or len(js) != lj:
            raise ValueError("truncated exif-only META payload")
        return MetaBlob("exif-only", exif_json=bytes(js), exif_blob=bytes(ex))
    return MetaBlob("skeleton", skeleton=bytes(payload))


def _source_ext(name: str | None) -> str:
    ext = Path(name).suffix.lower() if name else ""
    return ext if ext else ".rw2"


def make_skeleton(
    src: str | os.PathLike[str] | None,
    raw_bytes: bytes | None = None,
    *,
    raw_data_offset: int | None = None,
    et: ExifTool | None = None,
    allow_exif_only: bool = True,
) -> tuple[bytes, dict[str, Any]]:
    """Build the META payload for a raw file.

    ``raw_bytes`` is the full file content (read from ``src`` if None).  ``raw_data_offset``
    (e.g. ``RawFrame.raw_data_offset`` from exiftool) overrides the RW2 tag.  Returns
    ``(payload, info)`` where ``payload`` is UNCOMPRESSED (the container applies zstd-19)
    and ``info`` is JSON-ready for ``head["meta"]``::

        {"strategy": "skeleton", "format": "rw2", "method": "RawDataOffset",
         "source_size": ..., "skeleton_len": ..., "raw_regions": [[off, len], ...],
         "truncated": true, "previews": [{"tag", "offset", "length", "header_bytes",
         "n_scans", "zeroed"}], "warnings": [...], "time_s": ...}

    On layout failure falls back to ``exif-only`` (needs exiftool and ``src``) or ``none``.
    """
    t0 = time.perf_counter()
    if raw_bytes is None:
        if src is None:
            raise ValueError("need src or raw_bytes")
        raw_bytes = Path(src).read_bytes()
    data = raw_bytes
    warnings: list[str] = []
    try:
        layout = locate_raw_layout(data, raw_data_offset=raw_data_offset)
    except MetaError as exc:
        warnings.append(f"skeleton: raw data not located ({exc})")
        layout = None
    if layout is None:
        if allow_exif_only and src is not None and (et is not None or have("exiftool")):
            try:
                payload, info = make_exif_only(src, et=et)
                info["warnings"] = warnings + info.get("warnings", [])
                info["time_s"] = round(time.perf_counter() - t0, 4)
                return payload, info
            except (ToolError, OSError) as exc:
                warnings.append(f"exif-only fallback failed: {exc}")
        return b"", {"strategy": "none", "warnings": warnings, "source_size": len(data)}

    regions = layout.raw_regions
    raw_start = min(o for o, _ in regions)
    raw_end = max(o + ln for o, ln in regions)
    # truncate when the raw data is the tail of the file (allowing <= 64 KiB trailer padding)
    truncate = raw_end >= len(data) - 65536 and all(
        o >= raw_start for o, _ in regions
    )
    if truncate:
        hdr = bytearray(data[:raw_start])
    else:
        hdr = bytearray(data)
        for o, ln in regions:
            hdr[o : o + ln] = bytes(ln)
    prev_info: list[dict[str, Any]] = []
    for p in layout.previews:
        if p.offset + p.length > len(hdr):
            warnings.append(f"preview {p.tag} lies outside the skeleton; skipped")
            continue
        try:
            stripped, si = jpeg_strip_scan(hdr[p.offset : p.offset + p.length])
        except ValueError as exc:
            warnings.append(f"preview {p.tag}: cannot parse JPEG ({exc}); kept as-is")
            prev_info.append({"tag": p.tag, "offset": p.offset, "length": p.length, "stripped": False})
            continue
        hdr[p.offset : p.offset + p.length] = stripped
        prev_info.append(
            {
                "tag": p.tag,
                "offset": p.offset,
                "length": p.length,
                "stripped": True,
                "header_bytes": si.header_bytes,
                "n_scans": si.n_scans,
                "zeroed": si.zeroed,
            }
        )
    info: dict[str, Any] = {
        "strategy": "skeleton",
        "format": layout.fmt,
        "method": layout.method,
        "source_size": len(data),
        "skeleton_len": len(hdr),
        "truncated": bool(truncate),
        "raw_regions": [[int(o), int(ln)] for o, ln in regions[:64]],
        "n_raw_regions": len(regions),
        "previews": prev_info,
        "warnings": warnings,
        "time_s": round(time.perf_counter() - t0, 4),
    }
    return bytes(hdr), info


def make_exif_only(src: str | os.PathLike[str], *, et: ExifTool | None = None) -> tuple[bytes, dict[str, Any]]:
    """Fallback META: ``exiftool -j -b`` JSON + ``exiftool -o x.exif`` (partial metadata)."""
    src = os.fspath(src)
    with tempfile.TemporaryDirectory(prefix="rsq_meta_") as td:
        exv = os.path.join(td, "x.exif")
        args_json = ["-j", "-b", "-a", "-G1", "-x", "JpgFromRaw", "-x", "JpgFromRaw2", "-x", "PreviewImage", src]
        args_exif = ["-q", "-q", "-o", exv, src]
        if et is not None:
            js = et.run_bytes(args_json)
            et.run_bytes(args_exif)
        else:
            js = run(["exiftool", *args_json], check=False, timeout=120).stdout
            run(["exiftool", *args_exif], check=False, timeout=120)
        exif = Path(exv).read_bytes() if os.path.exists(exv) else b""
    if not js.strip():
        raise ToolError("exiftool produced no JSON")
    payload = pack_exif_only(js, exif)
    return payload, {
        "strategy": "exif-only",
        "json_len": len(js),
        "exif_len": len(exif),
        "previews": [],
        "warnings": ["metadata stored as exif-only fallback (partial metadata)"],
    }


# ---------------------------------------------------------------------------------------
# previews


def extract_previews(
    src: str | os.PathLike[str] | None,
    raw_bytes: bytes | None = None,
    *,
    et: ExifTool | None = None,
    tags: Sequence[str] = ("JpgFromRaw", "JpgFromRaw2"),
) -> dict[str, bytes]:
    """Return the embedded camera JPEGs ``{tag: jpeg_bytes}`` (bit-exact copies).

    Uses the TIFF layout parser; falls back to ``exiftool -b -TAG`` per tag.
    """
    if raw_bytes is None:
        if src is None:
            raise ValueError("need src or raw_bytes")
        raw_bytes = Path(src).read_bytes()
    out: dict[str, bytes] = {}
    try:
        layout = locate_raw_layout(raw_bytes)
        for p in layout.previews:
            if p.tag in tags:
                out[p.tag] = bytes(raw_bytes[p.offset : p.offset + p.length])
    except MetaError:
        pass
    missing = [t for t in tags if t not in out]
    if missing and src is not None and (et is not None or have("exiftool")):
        for t in missing:
            args = ["-b", f"-{t}", os.fspath(src)]
            try:
                b = et.run_bytes(args) if et is not None else run(["exiftool", *args], check=False).stdout
            except ToolError:
                b = b""
            if b.startswith(b"\xff\xd8"):
                out[t] = b
    return out


def transcode_preview(jpeg: bytes, *, effort: int = 7, verify: bool = False) -> bytes:
    """Lossless JPEG -> JXL transcode via ``cjxl --lossless_jpeg=1 -e <effort>``.

    With ``verify=True`` the result is decoded with djxl and compared bit-exactly.
    Raises :class:`ToolError` if cjxl is missing or fails.
    """
    with tempfile.TemporaryDirectory(prefix="rsq_prv_") as td:
        a, b = os.path.join(td, "in.jpg"), os.path.join(td, "out.jxl")
        Path(a).write_bytes(jpeg)
        run(["cjxl", a, b, "--lossless_jpeg=1", "-e", str(effort), "--quiet"], timeout=300)
        out = Path(b).read_bytes()
    if verify and restore_preview(out) != jpeg:
        raise ToolError("JPEG -> JXL transcode is not reversible")
    return out


def restore_preview(jxl: bytes) -> bytes:
    """Reconstruct the original JPEG from a ``transcode_preview`` result (``djxl``)."""
    with tempfile.TemporaryDirectory(prefix="rsq_prv_") as td:
        a, b = os.path.join(td, "in.jxl"), os.path.join(td, "out.jpg")
        Path(a).write_bytes(jxl)
        run(["djxl", a, b, "--quiet"], timeout=300)
        return Path(b).read_bytes()


def preview_payloads(
    src: str | os.PathLike[str] | None,
    keep: str,
    raw_bytes: bytes | None = None,
    *,
    et: ExifTool | None = None,
    effort: int = 7,
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Payloads for PRV0/PRV1 according to ``--keep-preview none|small|full``.

    Returns ``({fourcc: jxl_bytes}, head_meta_previews)``; the second item is a list of
    ``{"fourcc", "tag", "jpeg_size", "jxl_size", "sha256"}`` for ``head["meta"]["previews"]``.
    Build chunks with ``container.make_chunk(fourcc, payload)``.
    """
    import hashlib

    if keep not in ("none", "small", "full"):
        raise ValueError(f"keep must be none|small|full, got {keep!r}")
    if keep == "none":
        return {}, []
    tags = ("JpgFromRaw",) if keep == "small" else ("JpgFromRaw", "JpgFromRaw2")
    jpgs = extract_previews(src, raw_bytes, et=et, tags=tags)
    out: dict[str, bytes] = {}
    info: list[dict[str, Any]] = []
    for t in tags:
        if t not in jpgs:
            continue
        jxl = transcode_preview(jpgs[t], effort=effort)
        fcc = _PREVIEW_FOURCC[t]
        out[fcc] = jxl
        info.append(
            {
                "fourcc": fcc,
                "tag": t,
                "jpeg_size": len(jpgs[t]),
                "jxl_size": len(jxl),
                "sha256": hashlib.sha256(jpgs[t]).hexdigest(),
            }
        )
    return out, info


# ---------------------------------------------------------------------------------------
# EXIF transfer into a DNG


@dataclass
class TransferReport:
    strategy: str
    used_jpg_from_raw: bool
    time_s: float
    stderr: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def transfer_exif(
    meta_payload: bytes,
    dng_path: str | os.PathLike[str],
    et: ExifTool | None = None,
    *,
    source_name: str | None = None,
) -> TransferReport:
    """Copy EXIF / MakerNotes / GPS / XMP from a META payload into ``dng_path`` in place.

    Skeleton strategy (DESIGN.md 3.5.4)::

        exiftool -b -JpgFromRaw skel.rw2 > jfr.jpg
        exiftool -q -overwrite_original \\
          -tagsFromFile jfr.jpg -exif:all -makernotes -gps:all --IFD0:all --IFD1:all --ThumbnailImage \\
          -tagsFromFile skel.rw2 -IFD0:Make -IFD0:Model -IFD0:Orientation \\
              -IFD0:ModifyDate -IFD0:Artist -IFD0:Copyright -xmp:all \\
          -OriginalRawFileName=<source_name>  out.dng

    When the skeleton has no JpgFromRaw (non-RW2 raws), EXIF/MakerNotes/GPS are copied from
    the skeleton itself.  exif-only strategy: copied from the stored ``.exif`` blob.
    ``source_name`` gives the skeleton's temp file extension (default ``.rw2``) and the DNG
    ``OriginalRawFileName``.  A single ``-q`` keeps exiftool warnings (reported), and a
    post-check reads ``ExifIFD:ExifVersion`` back: if no EXIF arrived in the DNG (unparsable
    skeleton, ...) a warning says so instead of reporting a silent success.
    Uses ``et`` (a running :class:`ExifTool`) if given, else one-shot subprocesses.
    """
    t0 = time.perf_counter()
    blob = parse_meta(meta_payload)
    dng = os.fspath(dng_path)
    warnings: list[str] = []
    if blob.strategy == "none":
        return TransferReport("none", False, 0.0, warnings=["no metadata to transfer"])

    def call(args: list[str]) -> tuple[bytes, str]:
        if et is not None:
            out = et.run_bytes(args)
            return out, et.last_stderr
        cp = run(["exiftool", *args], check=False, timeout=300)
        return cp.stdout, cp.stderr.decode("utf-8", errors="replace")

    used_jfr = False
    with tempfile.TemporaryDirectory(prefix="rsq_exif_") as td:
        if blob.strategy == "skeleton":
            skel = os.path.join(td, "skel" + _source_ext(source_name))
            Path(skel).write_bytes(blob.skeleton)
            jfr_bytes, _ = call(["-b", "-JpgFromRaw", skel])
            args = ["-q", "-overwrite_original"]
            if jfr_bytes.startswith(b"\xff\xd8"):
                jfr = os.path.join(td, "jfr.jpg")
                Path(jfr).write_bytes(jfr_bytes)
                used_jfr = True
                args += ["-tagsFromFile", jfr, "-exif:all", "-makernotes", "-gps:all", "--IFD0:all", "--IFD1:all", "--ThumbnailImage"]
            else:
                args += ["-tagsFromFile", skel, "-exif:all", "-makernotes", "-gps:all", "--IFD0:all", "--IFD1:all", "--ThumbnailImage"]
            args += ["-tagsFromFile", skel, *SKELETON_IFD0_TAGS, "-xmp:all"]
        else:
            exv = os.path.join(td, "x.exif")
            Path(exv).write_bytes(blob.exif_blob)
            args = ["-q", "-overwrite_original", "-tagsFromFile", exv, "-exif:all", "-makernotes", "-gps:all", "--IFD0:all", "--IFD1:all"]
            warnings.append("exif-only metadata: partial transfer")
        if source_name and _safe_tag_value(source_name):
            args.append(f"-OriginalRawFileName={source_name}")
        _, err = call([*args, dng])
        check, _ = call(["-s3", "-ExifIFD:ExifVersion", dng])
    if "Error" in err:
        raise ToolError(f"exiftool EXIF transfer failed: {err.strip()[:1000]}")
    if err.strip():
        warnings.append(err.strip()[:500])
    if not check.strip():
        warnings.append("EXIF transfer copied no EXIF tags into the DNG (exiftool could not use the stored metadata"
                        + (f": {err.strip()[:200]}" if err.strip() else "") + ")")
    return TransferReport(blob.strategy, used_jfr, round(time.perf_counter() - t0, 4), err.strip(), warnings)


SKELETON_IFD0_TAGS: tuple[str, ...] = (
    "-IFD0:Make", "-IFD0:Model", "-IFD0:Orientation", "-IFD0:ModifyDate", "-IFD0:Artist", "-IFD0:Copyright",
)
"""IFD0 tags copied from the skeleton (the camera Software string is replaced by rawsqueeze's, Q10)."""


def _safe_tag_value(s: str) -> bool:
    return "\n" not in s and "\r" not in s and len(s) < 512


def panasonic_distortion_info(meta_payload: bytes) -> bytes | None:
    """Raw bytes of the Panasonic ``DistortionInfo`` tag (0x0119, RW2 IFD0) from a skeleton META.

    The in-camera lens distortion correction parameters (exiftool: DistortionParam02..11,
    DistortionScale, DistortionCorrection) live in the RW2 raw IFD, which has no DNG
    counterpart; rawsqueeze keeps them in the DNG XMP (``rawsqueeze:PanasonicDistortionInfo``)
    and converts them to a DNG WarpRectilinear opcode (:func:`parse_panasonic_distortion`,
    ``dng.panasonic_warp_rectilinear``).  ``None`` if absent.
    """
    try:
        blob = parse_meta(meta_payload)
        if blob.strategy != "skeleton":
            return None
        data = blob.skeleton
        info = parse_tiff(data, max_ifds=2)
    except (MetaError, ValueError):
        return None
    if info.magic != 0x55 or not info.ifds:
        return None
    e = info.ifds[0].entries.get(0x0119)
    if e is None or e.size <= 0 or e.data_offset + e.size > len(data):
        return None
    return bytes(data[e.data_offset : e.data_offset + e.size])


@dataclass(frozen=True)
class PanasonicDistortion:
    """Decoded Panasonic ``DistortionInfo`` (0x0119): 16 little-endian int16 words.

    Model (verified against the camera JPEG on 13 DC-S9 / LUMIX S 24-60 + 70-300 frames,
    see docs/STATUS.md): with radii normalised by ``n`` pixels (word 12, the half-diagonal of
    the camera's output crop) around the centre of that crop,
    ``r_out = scale * (r_src + a*r_src**3 + b*r_src**5 + c*r_src**7)`` maps the raw (source)
    radius to the corrected (camera JPEG) radius, where ``scale = 1/(1 + w5/32768)``
    (exiftool DistortionScale), ``a = w8/32768`` (DistortionParam08), ``b = w4/32768``
    (DistortionParam04), ``c = w11/32768`` (DistortionParam11).  Words 2, 3, 6, 9, 10, 13
    are unused by this model (exiftool DistortionParam02/09 have no measurable effect).
    """

    words: tuple[int, ...]
    checksum_ok: bool

    @property
    def enabled(self) -> bool:
        """DistortionCorrection flag (low nibble of word 7) == 1."""
        return (self.words[7] & 0x0F) == 1

    @property
    def scale(self) -> float:
        return 1.0 / (1.0 + self.words[5] / 32768.0)

    @property
    def coeffs(self) -> tuple[float, float, float]:
        """(a, b, c) for r**3, r**5, r**7."""
        w = self.words
        return (w[8] / 32768.0, w[4] / 32768.0, w[11] / 32768.0)

    @property
    def norm_radius(self) -> int:
        return int(self.words[12])

    def forward(self, r_src: np.ndarray | float) -> np.ndarray | float:
        """Corrected (output) radius for a source radius, both in units of ``norm_radius``."""
        a, b, c = self.coeffs
        r = r_src
        r2 = r * r
        return self.scale * r * (1.0 + r2 * (a + r2 * (b + r2 * c)))


def _pana_checksum(data: bytes, start: int, num: int, inc: int) -> int:
    csum = 0
    for i in range(num):
        csum = (73 * csum + data[start + i * inc]) % 0xFFEF
    return csum


def parse_panasonic_distortion(raw: bytes | None) -> PanasonicDistortion | None:
    """Decode the 32-byte DistortionInfo blob (exiftool PanasonicRaw.pm, ref. syscall.eu).

    ``None`` if the blob is missing or not 32 bytes.  ``checksum_ok`` reports the four
    embedded checksums (words 0, 1, 14, 15).
    """
    if raw is None or len(raw) != 32:
        return None
    words = struct.unpack("<16h", raw)
    u16 = struct.unpack("<16H", raw)
    ok = (
        _pana_checksum(raw, 4, 12, 1) == u16[1]
        and _pana_checksum(raw, 16, 12, 1) == u16[14]
        and _pana_checksum(raw, 2, 14, 2) == u16[0]
        and _pana_checksum(raw, 3, 14, 2) == u16[15]
    )
    return PanasonicDistortion(words=tuple(int(v) for v in words), checksum_ok=ok)


def write_skeleton_file(meta_payload: bytes, path: str | os.PathLike[str]) -> Path:
    """Write the skeleton (strategy skeleton) to ``path`` for inspection with exiftool."""
    blob = parse_meta(meta_payload)
    if blob.strategy != "skeleton":
        raise ValueError(f"META strategy is {blob.strategy}, not skeleton")
    p = Path(path)
    p.write_bytes(blob.skeleton)
    return p


def count_tags(path: str | os.PathLike[str], et: ExifTool | None = None, *, unknown: bool = False) -> dict[str, int]:
    """Tag counts per family-1 group from ``exiftool -a -G1 -s [-u]``.

    ``"all"`` counts every output line (the DESIGN.md "329/329" figure for DC-S9 RW2);
    ``"total"`` excludes the ``System``, ``File``, ``Composite`` and ``ExifTool`` groups.
    """
    args = ["-a", "-G1", "-s"] + (["-u"] if unknown else []) + [os.fspath(path)]
    if et is not None:
        text = et.run(args)
    else:
        text = run(["exiftool", *args], check=False).stdout.decode("utf-8", errors="replace")
    counts: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("[") or "]" not in line:
            continue
        g = line[1 : line.index("]")]
        counts[g] = counts.get(g, 0) + 1
    skip = {"System", "File", "Composite", "ExifTool"}
    groups = dict(counts)
    counts["all"] = sum(groups.values())
    counts["total"] = sum(v for k, v in groups.items() if k not in skip)
    return counts


__all__ = [
    "EXIF_ONLY_MAGIC",
    "JpegStripInfo",
    "MetaBlob",
    "MetaError",
    "PanasonicDistortion",
    "PreviewRef",
    "RawLayout",
    "TiffInfo",
    "TransferReport",
    "compressed_size",
    "count_tags",
    "extract_previews",
    "jpeg_markers",
    "jpeg_strip_scan",
    "locate_raw_layout",
    "make_exif_only",
    "make_skeleton",
    "panasonic_distortion_info",
    "parse_panasonic_distortion",
    "pack_exif_only",
    "parse_meta",
    "parse_tiff",
    "preview_payloads",
    "restore_preview",
    "transcode_preview",
    "transfer_exif",
    "write_skeleton_file",
]
