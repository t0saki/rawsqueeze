"""The ``.rsq`` container (DESIGN.md section 3.1/3.2).

Byte layout (little endian)::

    0      8   magic  89 52 53 51 0D 0A 1A 0A
    8      2   u16 major (=1)
    10     2   u16 minor (=0)
    12     ... chunk*            (first = HEAD, last = INDX)
    EOF-12 8   u64 offset of the INDX chunk header
    EOF-4  4   b"RSQE"

    chunk:  fourcc(4) | u32 flags (bit0 ZSTD, bit1 CRITICAL) | u64 N | payload(N) |
            u32 crc32 = zlib.crc32(payload, zlib.crc32(header16))

INDX payload: JSON ``[[fourcc, offset_of_chunk_header, stored_payload_length], ...]`` for all
chunks *before* INDX (INDX does not list itself).

Reader policy: bad magic / major mismatch -> :class:`RsqFormatError`; any CRC failure ->
:class:`RsqCRCError` (except INDX, which is redundant: a damaged INDX triggers a sequential
rebuild with a warning); unknown CRITICAL chunk -> :class:`RsqUnknownChunkError`; unknown
non-critical chunk -> skipped + warning; missing footer -> :class:`RsqTruncatedError`;
higher minor -> accepted (warning).
"""

from __future__ import annotations

import io
import json
import os
import struct
import tempfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import numpy as np
import zstandard

from .tools import default_file_mode

MAGIC = b"\x89RSQ\r\n\x1a\n"
VERSION: tuple[int, int] = (1, 0)
END_MAGIC = b"RSQE"
FLAG_ZSTD = 0x1
FLAG_CRITICAL = 0x2
FLAG_RESERVED_MASK = ~(FLAG_ZSTD | FLAG_CRITICAL) & 0xFFFFFFFF
ZSTD_LEVEL = 19

_PREAMBLE = struct.Struct("<8sHH")  # magic, major, minor  (12 B)
_CHUNK_HDR = struct.Struct("<4sIQ")  # fourcc, flags, length (16 B)
_CRC = struct.Struct("<I")
_FOOTER = struct.Struct("<Q4s")  # offset of INDX, b"RSQE" (12 B)

KNOWN_CHUNKS: dict[str, tuple[bool, bool]] = {
    # fourcc: (critical, zstd) defaults per DESIGN.md 3.2
    "HEAD": (True, True),
    "LUTS": (True, True),
    "PLN0": (True, False),
    "PLN1": (True, False),
    "PLN2": (True, False),
    "PLN3": (True, False),
    "PLNS": (True, False),
    "H3RG": (True, False),
    "H3DG": (True, False),
    "SATM": (True, True),
    "G4P0": (True, False),
    "G4P1": (True, False),
    "G4P2": (True, False),
    "G4P3": (True, False),
    "META": (False, True),
    "PRV0": (False, False),
    "PRV1": (False, False),
    "TILE": (True, False),
    "RESD": (True, False),
    "INDX": (False, False),
}
"""Chunk types this reader understands, with default (critical, zstd) flags."""


class RsqError(ValueError):
    """Base class for .rsq parsing errors."""


class RsqFormatError(RsqError):
    """Bad magic, unsupported major version, or structurally invalid file."""


class RsqTruncatedError(RsqError):
    """Footer missing or a chunk extends past the end of the file."""


class RsqCRCError(RsqError):
    """A chunk's CRC32 does not match."""


class RsqUnknownChunkError(RsqError):
    """An unknown chunk flagged CRITICAL was found."""


def _fourcc_bytes(fourcc: str | bytes) -> bytes:
    b = fourcc.encode("ascii") if isinstance(fourcc, str) else bytes(fourcc)
    if len(b) != 4 or not all(0x20 < c < 0x7F for c in b):
        raise ValueError(f"fourcc must be 4 printable ASCII bytes, got {fourcc!r}")
    return b


def _fourcc_str(fourcc: bytes) -> str:
    return fourcc.decode("latin-1")


@dataclass
class Chunk:
    """A chunk to write.  ``payload`` is the UNCOMPRESSED payload; the writer applies
    zstd-19 when ``zstd`` is True.  ``fourcc`` may be given as str or bytes (stored as bytes).
    """

    fourcc: bytes
    payload: bytes
    critical: bool = True
    zstd: bool = False

    def __post_init__(self) -> None:
        self.fourcc = _fourcc_bytes(self.fourcc)
        if not isinstance(self.payload, bytes):
            self.payload = bytes(self.payload)

    @property
    def name(self) -> str:
        return _fourcc_str(self.fourcc)

    @property
    def flags(self) -> int:
        return (FLAG_ZSTD if self.zstd else 0) | (FLAG_CRITICAL if self.critical else 0)


def make_chunk(fourcc: str | bytes, payload: bytes, **overrides: bool) -> Chunk:
    """Chunk with the default flags from :data:`KNOWN_CHUNKS` (``critical``/``zstd`` overridable).

    Unknown fourccs default to non-critical, uncompressed.
    """
    name = _fourcc_str(_fourcc_bytes(fourcc))
    crit, zst = KNOWN_CHUNKS.get(name, (False, False))
    return Chunk(
        fourcc,
        payload,
        critical=overrides.get("critical", crit),
        zstd=overrides.get("zstd", zst),
    )


def _json_default(o: object) -> object:
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"HEAD value not JSON serialisable: {type(o).__name__}")


def head_to_json(head: Mapping[str, Any]) -> bytes:
    """Serialise HEAD dict to compact UTF-8 JSON (floats use repr: exact float64 round trip).

    numpy scalars/arrays are converted; NaN/inf are rejected (ValueError).
    """
    return json.dumps(
        head, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=_json_default
    ).encode("utf-8")


def make_head_chunk(head: Mapping[str, Any]) -> Chunk:
    """The HEAD chunk (critical, zstd) for a HEAD dict."""
    return Chunk(b"HEAD", head_to_json(head), critical=True, zstd=True)


# ---------------------------------------------------------------------------------------
# payload helpers for format-defined chunks


def pack_luts(luts: Sequence[np.ndarray]) -> bytes:
    """LUTS payload: 4 x (``u32 n`` + ``n x u16``) in position order (uncompressed form)."""
    if len(luts) != 4:
        raise ValueError(f"expected 4 LUTs, got {len(luts)}")
    out = bytearray()
    for lut in luts:
        a = np.asarray(lut)
        if a.ndim != 1:
            raise ValueError("LUT must be 1-D")
        if a.size and (a.min() < 0 or a.max() > 0xFFFF):
            raise ValueError("LUT values must fit in uint16")
        out += struct.pack("<I", a.size)
        out += a.astype("<u2").tobytes()
    return bytes(out)


def unpack_luts(b: bytes) -> list[np.ndarray]:
    """Inverse of :func:`pack_luts`; returns 4 native uint16 arrays."""
    luts: list[np.ndarray] = []
    p = 0
    for _ in range(4):
        if p + 4 > len(b):
            raise RsqFormatError("LUTS payload truncated")
        (n,) = struct.unpack_from("<I", b, p)
        p += 4
        if p + 2 * n > len(b):
            raise RsqFormatError("LUTS payload truncated")
        luts.append(np.frombuffer(b, dtype="<u2", count=n, offset=p).astype(np.uint16))
        p += 2 * n
    if p != len(b):
        raise RsqFormatError("LUTS payload has trailing bytes")
    return luts


def pack_mask(mask: np.ndarray) -> bytes:
    """SATM payload (uncompressed form): ``np.packbits(mask.ravel(), bitorder='big')``."""
    return np.packbits(np.asarray(mask, dtype=bool).ravel(), bitorder="big").tobytes()


def unpack_mask(b: bytes, shape: Sequence[int]) -> np.ndarray:
    """Inverse of :func:`pack_mask` for a mask of ``shape`` (row-major)."""
    n = int(np.prod(shape))
    if len(b) != (n + 7) // 8:
        raise RsqFormatError(f"SATM payload has {len(b)} bytes, expected {(n + 7) // 8} for shape {tuple(shape)}")
    bits = np.unpackbits(np.frombuffer(b, dtype=np.uint8), count=n, bitorder="big")
    return bits.reshape(tuple(int(s) for s in shape)).astype(bool)


# ---------------------------------------------------------------------------------------
# writer


def _write_chunk(f: IO[bytes], fourcc: bytes, flags: int, stored: bytes) -> int:
    hdr = _CHUNK_HDR.pack(fourcc, flags, len(stored))
    f.write(hdr)
    f.write(stored)
    f.write(_CRC.pack(zlib.crc32(stored, zlib.crc32(hdr)) & 0xFFFFFFFF))
    return len(hdr) + len(stored) + _CRC.size


def write_rsq(
    dst: str | os.PathLike[str] | IO[bytes],
    chunks: Iterable[Chunk],
    *,
    minor: int = VERSION[1],
    zstd_level: int = ZSTD_LEVEL,
) -> int:
    """Write an .rsq file; returns the number of bytes written.

    ``chunks`` must start with HEAD and must not contain INDX (appended automatically
    together with the footer).  ``zstd=True`` chunks are compressed with zstd level
    ``zstd_level``.  For a path the write is atomic (temp file + ``os.replace``).
    Duplicate fourccs are rejected.
    """
    chunk_list = list(chunks)
    if not chunk_list or chunk_list[0].fourcc != b"HEAD":
        raise ValueError("the first chunk must be HEAD")
    seen: set[bytes] = set()
    for c in chunk_list:
        if c.fourcc == b"INDX":
            raise ValueError("INDX is written automatically; do not pass it")
        if c.fourcc in seen:
            raise ValueError(f"duplicate chunk {c.name}")
        seen.add(c.fourcc)

    cctx = zstandard.ZstdCompressor(level=zstd_level)

    def emit(f: IO[bytes], base: int) -> int:
        pos = base
        f.write(_PREAMBLE.pack(MAGIC, VERSION[0], int(minor)))
        pos += _PREAMBLE.size
        index: list[list[Any]] = []
        for c in chunk_list:
            stored = cctx.compress(c.payload) if c.zstd else c.payload
            index.append([c.name, pos - base, len(stored)])
            pos += _write_chunk(f, c.fourcc, c.flags, stored)
        indx_off = pos - base
        indx = json.dumps(index, separators=(",", ":")).encode("ascii")
        pos += _write_chunk(f, b"INDX", 0, indx)
        f.write(_FOOTER.pack(indx_off, END_MAGIC))
        pos += _FOOTER.size
        return pos - base

    if hasattr(dst, "write"):
        f = dst  # type: ignore[assignment]
        try:
            base = f.tell()  # type: ignore[union-attr]
        except (OSError, AttributeError, io.UnsupportedOperation):
            base = 0
        return emit(f, base)  # type: ignore[arg-type]

    path = Path(os.fspath(dst))  # type: ignore[arg-type]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            n = emit(f, 0)
        os.chmod(tmp, default_file_mode())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return n


# ---------------------------------------------------------------------------------------
# reader


@dataclass
class ChunkEntry:
    """Location of one chunk in a file (``length`` = stored payload length)."""

    fourcc: str
    offset: int
    length: int
    flags: int = 0
    crc_ok: bool | None = None
    """True/False after CRC verification, None if not verified."""

    @property
    def critical(self) -> bool:
        return bool(self.flags & FLAG_CRITICAL)

    @property
    def zstd(self) -> bool:
        return bool(self.flags & FLAG_ZSTD)

    @property
    def total_size(self) -> int:
        """Header + payload + CRC bytes."""
        return _CHUNK_HDR.size + self.length + _CRC.size

    @property
    def known(self) -> bool:
        return self.fourcc in KNOWN_CHUNKS


@dataclass
class RsqFile:
    """Parsed .rsq file.

    ``chunks`` maps fourcc (str) -> decompressed payload for every known chunk except
    INDX (empty when read with :func:`read_rsq_info`).  ``entries`` lists all chunks in
    file order, including unknown ones and INDX.
    """

    head: dict[str, Any]
    chunks: dict[str, bytes] = field(default_factory=dict)
    entries: list[ChunkEntry] = field(default_factory=list)
    version: tuple[int, int] = VERSION
    warnings: list[str] = field(default_factory=list)
    file_size: int = 0
    index_rebuilt: bool = False
    path: str | None = None

    def __contains__(self, fourcc: object) -> bool:
        key = fourcc.decode("latin-1") if isinstance(fourcc, (bytes, bytearray)) else fourcc
        return key in self.chunks

    def get(self, fourcc: str | bytes, default: bytes | None = None) -> bytes | None:
        key = fourcc.decode("latin-1") if isinstance(fourcc, (bytes, bytearray)) else fourcc
        return self.chunks.get(key, default)

    def __getitem__(self, fourcc: str | bytes) -> bytes:
        key = fourcc.decode("latin-1") if isinstance(fourcc, (bytes, bytearray)) else fourcc
        return self.chunks[key]

    @property
    def index(self) -> list[ChunkEntry]:
        """Alias of :attr:`entries`."""
        return self.entries

    def entry(self, fourcc: str) -> ChunkEntry | None:
        for e in self.entries:
            if e.fourcc == fourcc:
                return e
        return None

    def chunk_sizes(self) -> dict[str, int]:
        """fourcc -> total on-disk bytes (header+payload+crc)."""
        return {e.fourcc: e.total_size for e in self.entries}


class _Source:
    """Random access over bytes or a seekable binary file."""

    def __init__(self, src: bytes | memoryview | IO[bytes]) -> None:
        if isinstance(src, (bytes, bytearray, memoryview)):
            self._buf: bytes | None = bytes(src)
            self._f: IO[bytes] | None = None
            self.size = len(self._buf)
        else:
            self._buf = None
            self._f = src
            src.seek(0, os.SEEK_END)
            self.size = src.tell()

    def read(self, off: int, n: int) -> bytes:
        if off < 0 or n < 0:
            return b""
        if self._buf is not None:
            return self._buf[off : off + n]
        assert self._f is not None
        self._f.seek(off)
        return self._f.read(n)


def _read_header(src: _Source, off: int, limit: int) -> tuple[bytes, int, int]:
    """Return (fourcc, flags, length) of the chunk at ``off``; validate it fits before ``limit``."""
    hdr = src.read(off, _CHUNK_HDR.size)
    if len(hdr) < _CHUNK_HDR.size:
        raise RsqTruncatedError(f"chunk header at offset {off} is truncated")
    fourcc, flags, n = _CHUNK_HDR.unpack(hdr)
    if off + _CHUNK_HDR.size + n + _CRC.size > limit:
        raise RsqTruncatedError(
            f"chunk {_fourcc_str(fourcc)!r} at offset {off} (length {n}) extends past end of data"
        )
    return fourcc, flags, n


def _check_crc(src: _Source, e: ChunkEntry) -> tuple[bool, bytes]:
    blob = src.read(e.offset, e.total_size)
    hdr, payload = blob[: _CHUNK_HDR.size], blob[_CHUNK_HDR.size : -_CRC.size]
    (crc,) = _CRC.unpack(blob[-_CRC.size :])
    return (zlib.crc32(payload, zlib.crc32(hdr)) & 0xFFFFFFFF) == crc, payload


def _scan(src: _Source, start: int, limit: int) -> list[ChunkEntry]:
    entries: list[ChunkEntry] = []
    p = start
    while p < limit:
        fourcc, flags, n = _read_header(src, p, limit)
        entries.append(ChunkEntry(_fourcc_str(fourcc), p, n, flags))
        p += _CHUNK_HDR.size + n + _CRC.size
    if p != limit:
        raise RsqFormatError("chunk stream does not end at the footer")
    return entries


def _entries_from_index(src: _Source, indx_off: int, limit: int) -> list[ChunkEntry] | None:
    """Parse and validate INDX.

    Returns None if INDX itself is missing/damaged (bad offset, CRC, JSON, layout) so the
    caller rebuilds by scanning; raises RsqCRCError if a valid INDX disagrees with a chunk
    header (header corruption).
    """
    try:
        fourcc, flags, n = _read_header(src, indx_off, limit)
    except RsqError:
        return None
    if fourcc != b"INDX" or indx_off + _CHUNK_HDR.size + n + _CRC.size != limit:
        return None
    indx = ChunkEntry("INDX", indx_off, n, flags)
    ok, payload = _check_crc(src, indx)
    if not ok:
        return None
    try:
        raw = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, list):
        return None
    entries: list[ChunkEntry] = []
    expect = _PREAMBLE.size
    for item in raw:
        if not (isinstance(item, list) and len(item) == 3):
            return None
        name, off, length = item
        if not (isinstance(name, str) and isinstance(off, int) and isinstance(length, int)):
            return None
        if off != expect:
            return None
        # INDX passed its CRC, so it is trusted: a disagreeing chunk header is corruption.
        try:
            f4, fl, nn = _read_header(src, off, indx_off)
        except RsqTruncatedError as exc:
            raise RsqCRCError(f"chunk header at offset {off} is corrupt ({exc})") from None
        if _fourcc_str(f4) != name or nn != length:
            raise RsqCRCError(
                f"chunk header at offset {off} is corrupt "
                f"(INDX says {name!r}/{length}, header says {_fourcc_str(f4)!r}/{nn})"
            )
        entries.append(ChunkEntry(name, off, length, fl))
        expect = off + _CHUNK_HDR.size + length + _CRC.size
    if expect != indx_off:
        return None
    indx.crc_ok = True
    entries.append(indx)
    return entries


def _locate(src: _Source, warnings: list[str]) -> tuple[tuple[int, int], list[ChunkEntry], bool]:
    """Validate preamble/footer and return (version, entries, index_rebuilt)."""
    pre = src.read(0, _PREAMBLE.size)
    if len(pre) < len(MAGIC) or pre[: len(MAGIC)] != MAGIC:
        if len(pre) < len(MAGIC) and MAGIC.startswith(pre) and pre:
            raise RsqTruncatedError("file truncated inside the magic number")
        raise RsqFormatError("bad magic: not an .rsq file")
    if len(pre) < _PREAMBLE.size:
        raise RsqTruncatedError("file truncated inside the version field")
    _, major, minor = _PREAMBLE.unpack(pre)
    if major != VERSION[0]:
        raise RsqFormatError(f"unsupported .rsq major version {major} (supported: {VERSION[0]})")
    if minor > VERSION[1]:
        warnings.append(f"file minor version {minor} is newer than reader ({VERSION[1]})")
    if src.size < _PREAMBLE.size + _FOOTER.size:
        raise RsqTruncatedError("truncated: file too short for footer")
    indx_off, end = _FOOTER.unpack(src.read(src.size - _FOOTER.size, _FOOTER.size))
    if end != END_MAGIC:
        raise RsqTruncatedError("truncated: end marker 'RSQE' missing")
    limit = src.size - _FOOTER.size
    entries = _entries_from_index(src, indx_off, limit)
    rebuilt = False
    if entries is None:
        warnings.append("INDX missing or damaged; index rebuilt by sequential scan")
        entries = _scan(src, _PREAMBLE.size, limit)
        rebuilt = True
    if not entries or entries[0].fourcc != "HEAD":
        raise RsqFormatError("first chunk is not HEAD")
    for i, e in enumerate(entries):
        if e.fourcc == "INDX" and i != len(entries) - 1:
            raise RsqFormatError("INDX is not the last chunk")
    names = [e.fourcc for e in entries]
    dups = {n for n in names if names.count(n) > 1}
    if dups:
        raise RsqFormatError(f"duplicate chunks: {sorted(dups)}")
    return (int(major), int(minor)), entries, rebuilt


def _decode_payload(e: ChunkEntry, payload: bytes) -> bytes:
    if not e.zstd:
        return payload
    try:
        return zstandard.ZstdDecompressor().decompress(payload, max_output_size=1 << 31)
    except zstandard.ZstdError as exc:
        raise RsqFormatError(f"chunk {e.fourcc}: zstd decompression failed: {exc}") from exc


def _parse_head(payload: bytes) -> dict[str, Any]:
    try:
        head = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RsqFormatError(f"HEAD is not valid JSON: {exc}") from exc
    if not isinstance(head, dict):
        raise RsqFormatError("HEAD JSON is not an object")
    return head


def _open_source(src: str | os.PathLike[str] | bytes | IO[bytes]) -> tuple[_Source, IO[bytes] | None, str | None]:
    if isinstance(src, (bytes, bytearray, memoryview)):
        return _Source(src), None, None
    if hasattr(src, "read"):
        return _Source(src), None, None  # type: ignore[arg-type]
    path = os.fspath(src)  # type: ignore[arg-type]
    f = open(path, "rb")
    return _Source(f), f, path


def read_rsq(
    src: str | os.PathLike[str] | bytes | IO[bytes],
    *,
    verify_crc: bool = True,
) -> RsqFile:
    """Read a full .rsq file (path, bytes, or seekable binary file object).

    ``verify_crc=False`` skips CRC checks of all chunks except HEAD (debug / ``--force``).
    Raises :class:`RsqError` subclasses as described in the module docstring.
    """
    source, fh, path = _open_source(src)
    try:
        warnings: list[str] = []
        version, entries, rebuilt = _locate(source, warnings)
        chunks: dict[str, bytes] = {}
        head: dict[str, Any] | None = None
        for e in entries:
            if e.fourcc == "INDX":
                if e.crc_ok is None and verify_crc:
                    ok, _ = _check_crc(source, e)
                    e.crc_ok = ok
                    if not ok:
                        warnings.append("INDX CRC mismatch (index was rebuilt by scan)")
                continue
            if e.flags & FLAG_RESERVED_MASK:
                warnings.append(f"chunk {e.fourcc}: reserved flag bits set (0x{e.flags:08x})")
            if not e.known:
                if e.critical:
                    raise RsqUnknownChunkError(f"unknown critical chunk {e.fourcc!r}")
                warnings.append(f"skipping unknown non-critical chunk {e.fourcc!r}")
                continue
            ok, payload = _check_crc(source, e)
            if verify_crc or e.fourcc == "HEAD":
                e.crc_ok = ok
                if not ok:
                    raise RsqCRCError(f"CRC mismatch in chunk {e.fourcc} at offset {e.offset}")
            data = _decode_payload(e, payload)
            if e.fourcc == "HEAD":
                head = _parse_head(data)
            else:
                chunks[e.fourcc] = data
        assert head is not None
        return RsqFile(
            head=head,
            chunks=chunks,
            entries=entries,
            version=version,
            warnings=warnings,
            file_size=source.size,
            index_rebuilt=rebuilt,
            path=path,
        )
    finally:
        if fh is not None:
            fh.close()


def read_rsq_info(
    src: str | os.PathLike[str] | bytes | IO[bytes],
    *,
    verify_crc: bool = False,
) -> RsqFile:
    """Read HEAD + chunk index without keeping payloads (for ``rawsqueeze info``).

    HEAD's CRC is always checked.  With ``verify_crc=True`` every chunk's CRC is checked
    too (payloads are read one at a time and discarded) and recorded in
    ``ChunkEntry.crc_ok``; failures are reported in ``warnings`` instead of raising.
    Unknown critical chunks are reported as warnings.  ``chunks`` is empty.
    """
    source, fh, path = _open_source(src)
    try:
        warnings: list[str] = []
        version, entries, rebuilt = _locate(source, warnings)
        head: dict[str, Any] | None = None
        for e in entries:
            if e.fourcc == "HEAD":
                ok, payload = _check_crc(source, e)
                e.crc_ok = ok
                if not ok:
                    raise RsqCRCError(f"CRC mismatch in chunk HEAD at offset {e.offset}")
                head = _parse_head(_decode_payload(e, payload))
                continue
            if not e.known:
                kind = "critical" if e.critical else "non-critical"
                warnings.append(f"unknown {kind} chunk {e.fourcc!r}")
            if verify_crc and e.crc_ok is None:
                ok, _ = _check_crc(source, e)
                e.crc_ok = ok
                if not ok:
                    warnings.append(f"CRC mismatch in chunk {e.fourcc} at offset {e.offset}")
        assert head is not None
        return RsqFile(
            head=head,
            chunks={},
            entries=entries,
            version=version,
            warnings=warnings,
            file_size=source.size,
            index_rebuilt=rebuilt,
            path=path,
        )
    finally:
        if fh is not None:
            fh.close()
