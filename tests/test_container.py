from __future__ import annotations

import io
import json
import struct
import zlib

import numpy as np
import pytest

from rawsqueeze import container as C


def _sample_chunks() -> list[C.Chunk]:
    rng = np.random.default_rng(0)
    head = {"format": "rsq", "version": [1, 0], "pi": 3.141592653589793, "tiny": 1e-300,
            "np": np.float64(0.1) * 3, "arr": np.arange(3), "name": "P1060444.RW2"}
    return [
        C.make_head_chunk(head),
        C.make_chunk("LUTS", C.pack_luts([np.arange(10, dtype=np.uint16) * k for k in range(1, 5)])),
        C.Chunk(b"PLN0", rng.integers(0, 256, 3000, dtype=np.uint8).tobytes()),
        C.Chunk("PLN1", b""),
        C.make_chunk("SATM", C.pack_mask(np.eye(8, 12, dtype=bool))),
        C.make_chunk("META", b"metadata " * 200),
    ]


def _write_bytes(chunks: list[C.Chunk]) -> bytes:
    buf = io.BytesIO()
    n = C.write_rsq(buf, chunks)
    data = buf.getvalue()
    assert n == len(data)
    return data


def test_roundtrip_path(tmp_path) -> None:
    chunks = _sample_chunks()
    p = tmp_path / "a.rsq"
    n = C.write_rsq(p, chunks)
    assert p.stat().st_size == n
    assert not list(tmp_path.glob(".*.tmp"))
    f = C.read_rsq(p)
    assert f.head["pi"] == 3.141592653589793 and f.head["tiny"] == 1e-300
    assert f.head["np"] == 0.1 * 3 and f.head["arr"] == [0, 1, 2]
    assert f.version == (1, 0) and not f.warnings and not f.index_rebuilt
    assert [e.fourcc for e in f.entries] == ["HEAD", "LUTS", "PLN0", "PLN1", "SATM", "META", "INDX"]
    for c in chunks[1:]:
        assert f.chunks[c.name] == c.payload
        assert f[c.fourcc] == c.payload and c.fourcc in f and c.name in f
    assert "INDX" not in f.chunks and "HEAD" not in f.chunks
    luts = C.unpack_luts(f["LUTS"])
    assert [l.tolist() for l in luts] == [(np.arange(10) * k).tolist() for k in range(1, 5)]
    assert np.array_equal(C.unpack_mask(f["SATM"], (8, 12)), np.eye(8, 12, dtype=bool))
    assert f.entry("HEAD").zstd and f.entry("HEAD").critical
    assert not f.entry("META").critical and f.entry("META").zstd
    assert f.entry("PLN0").length == 3000 and not f.entry("PLN0").zstd
    assert sum(f.chunk_sizes().values()) + 12 + 12 == f.file_size
    assert f.path == str(p)


def test_layout_bytes() -> None:
    data = _write_bytes(_sample_chunks())
    assert data[:8] == b"\x89RSQ\r\n\x1a\n"
    assert struct.unpack_from("<HH", data, 8) == (1, 0)
    assert data[12:16] == b"HEAD"
    assert data[-4:] == b"RSQE"
    (indx_off,) = struct.unpack_from("<Q", data, len(data) - 12)
    assert data[indx_off : indx_off + 4] == b"INDX"
    _, flags, n = struct.unpack_from("<4sIQ", data, indx_off)
    idx = json.loads(data[indx_off + 16 : indx_off + 16 + n])
    assert idx[0] == ["HEAD", 12, idx[0][2]]
    # crc definition
    hdr = data[12:28]
    (hn,) = struct.unpack_from("<Q", hdr, 8)
    pay = data[28 : 28 + hn]
    (crc,) = struct.unpack_from("<I", data, 28 + hn)
    assert crc == zlib.crc32(pay, zlib.crc32(hdr))
    assert struct.unpack_from("<I", hdr, 4)[0] == C.FLAG_ZSTD | C.FLAG_CRITICAL


def test_read_from_bytes_and_fileobj() -> None:
    data = _write_bytes(_sample_chunks())
    a = C.read_rsq(data)
    b = C.read_rsq(io.BytesIO(data))
    assert a.chunks == b.chunks and a.head == b.head


def test_writer_validation() -> None:
    ch = _sample_chunks()
    with pytest.raises(ValueError):
        C.write_rsq(io.BytesIO(), ch[1:])  # HEAD not first
    with pytest.raises(ValueError):
        C.write_rsq(io.BytesIO(), ch + [C.Chunk(b"INDX", b"[]")])
    with pytest.raises(ValueError):
        C.write_rsq(io.BytesIO(), ch + [C.Chunk(b"PLN0", b"x")])
    with pytest.raises(ValueError):
        C.Chunk(b"AB", b"")
    with pytest.raises(ValueError):
        C.make_head_chunk({"x": float("nan")})


def _chunk_spans(data: bytes) -> dict[str, tuple[int, int]]:
    f = C.read_rsq(data)
    return {e.fourcc: (e.offset, e.total_size) for e in f.entries}


def test_every_bit_flip_in_chunks_detected() -> None:
    data = _write_bytes(_sample_chunks())
    spans = _chunk_spans(data)
    rng = np.random.default_rng(42)
    for name, (off, size) in spans.items():
        positions = set(range(off, off + 16))  # every header byte
        positions |= set(int(x) for x in rng.integers(off, off + size, 24))
        positions.add(off + size - 1)  # crc byte
        for pos in sorted(positions):
            for bit in (0, 7) if pos < off + 16 else (int(rng.integers(0, 8)),):
                bad = bytearray(data)
                bad[pos] ^= 1 << bit
                if name == "INDX":
                    # INDX is redundant: damage triggers a rebuild, payloads stay intact.
                    try:
                        f = C.read_rsq(bytes(bad))
                    except C.RsqError:
                        continue  # e.g. header length flip makes the stream unparsable
                    assert f.index_rebuilt and f.warnings
                    assert f.chunks == C.read_rsq(data).chunks
                else:
                    with pytest.raises(C.RsqError):
                        C.read_rsq(bytes(bad))


def test_payload_flip_is_crc_error() -> None:
    data = _write_bytes(_sample_chunks())
    off, size = _chunk_spans(data)["PLN0"]
    bad = bytearray(data)
    bad[off + 100] ^= 0x10
    with pytest.raises(C.RsqCRCError):
        C.read_rsq(bytes(bad))
    # --force style: CRC skipped for payload chunks
    f = C.read_rsq(bytes(bad), verify_crc=False)
    assert f["PLN0"] != C.read_rsq(data)["PLN0"]
    # HEAD CRC is always checked
    hoff, _ = _chunk_spans(data)["HEAD"]
    bad = bytearray(data)
    bad[hoff + 20] ^= 1
    with pytest.raises(C.RsqError):
        C.read_rsq(bytes(bad), verify_crc=False)


def test_header_field_flip_is_crc_error_with_index() -> None:
    data = _write_bytes(_sample_chunks())
    off, _ = _chunk_spans(data)["PLN0"]
    for delta in (0, 4, 8):  # fourcc, flags, length
        bad = bytearray(data)
        bad[off + delta] ^= 0x01
        with pytest.raises(C.RsqCRCError):
            C.read_rsq(bytes(bad))


def test_truncation_anywhere_detected() -> None:
    data = _write_bytes(_sample_chunks())
    rng = np.random.default_rng(7)
    cuts = {0, 1, 7, 8, 11, 12, 20, len(data) - 1, len(data) - 4, len(data) - 12}
    cuts |= set(int(x) for x in rng.integers(1, len(data), 40))
    for cut in sorted(cuts):
        with pytest.raises(C.RsqError):
            C.read_rsq(data[:cut])
    with pytest.raises(C.RsqTruncatedError):
        C.read_rsq(data[:-1])
    with pytest.raises(C.RsqTruncatedError):
        C.read_rsq(data[:3000])


def test_bad_magic_and_major() -> None:
    data = _write_bytes(_sample_chunks())
    with pytest.raises(C.RsqFormatError, match="magic"):
        C.read_rsq(b"PK\x03\x04" + data[4:])
    with pytest.raises(C.RsqFormatError, match="major"):
        C.read_rsq(data[:8] + struct.pack("<H", 2) + data[10:])
    newer = data[:10] + struct.pack("<H", 5) + data[12:]
    f = C.read_rsq(newer)
    assert any("minor" in w for w in f.warnings)
    with pytest.raises(C.RsqError):
        C.read_rsq(b"")


def test_higher_minor_written() -> None:
    buf = io.BytesIO()
    C.write_rsq(buf, _sample_chunks(), minor=3)
    f = C.read_rsq(buf.getvalue())
    assert f.version == (1, 3)


def test_unknown_chunks() -> None:
    ch = _sample_chunks()
    ok = ch + [C.Chunk(b"XTRA", b"future stuff", critical=False, zstd=True)]
    f = C.read_rsq(_write_bytes(ok))
    assert "XTRA" not in f.chunks
    assert any("XTRA" in w for w in f.warnings)
    assert f.entry("XTRA") is not None and not f.entry("XTRA").known
    bad = ch + [C.Chunk(b"XCRT", b"must understand", critical=True)]
    with pytest.raises(C.RsqUnknownChunkError):
        C.read_rsq(_write_bytes(bad))
    info = C.read_rsq_info(_write_bytes(bad))
    assert any("XCRT" in w for w in info.warnings)


def _strip_index(data: bytes, *, keep_footer: bool) -> bytes:
    (indx_off,) = struct.unpack_from("<Q", data, len(data) - 12)
    body = data[:indx_off]
    if keep_footer:
        return body + struct.pack("<Q4s", indx_off, b"RSQE")  # INDX gone, footer offset dangling
    return body


def test_index_missing_rebuilt_by_scan() -> None:
    data = _write_bytes(_sample_chunks())
    ref = C.read_rsq(data)
    noidx = _strip_index(data, keep_footer=True)
    f = C.read_rsq(noidx)
    assert f.index_rebuilt and any("INDX" in w for w in f.warnings)
    assert f.chunks == ref.chunks and f.head == ref.head
    assert [e.fourcc for e in f.entries] == ["HEAD", "LUTS", "PLN0", "PLN1", "SATM", "META"]
    with pytest.raises(C.RsqTruncatedError):
        C.read_rsq(_strip_index(data, keep_footer=False))


def test_index_bad_offset_rebuilt() -> None:
    data = _write_bytes(_sample_chunks())
    bad = data[:-12] + struct.pack("<Q4s", 12, b"RSQE")
    f = C.read_rsq(bad)
    assert f.index_rebuilt and f.chunks == C.read_rsq(data).chunks


def test_read_info(tmp_path) -> None:
    p = tmp_path / "x.rsq"
    C.write_rsq(p, _sample_chunks())
    info = C.read_rsq_info(p)
    assert info.head["format"] == "rsq" and info.chunks == {}
    assert [e.fourcc for e in info.entries][-1] == "INDX"
    assert all(e.crc_ok is None for e in info.entries if e.fourcc not in ("HEAD", "INDX"))
    info = C.read_rsq_info(p, verify_crc=True)
    assert all(e.crc_ok for e in info.entries)
    data = bytearray(p.read_bytes())
    off = info.entry("PLN0").offset
    data[off + 50] ^= 4
    info = C.read_rsq_info(bytes(data), verify_crc=True)
    assert info.entry("PLN0").crc_ok is False
    assert any("PLN0" in w for w in info.warnings)


def test_luts_and_mask_helpers() -> None:
    with pytest.raises(ValueError):
        C.pack_luts([np.zeros(3)] * 3)
    with pytest.raises(ValueError):
        C.pack_luts([np.array([70000])] * 4)
    b = C.pack_luts([np.array([], np.uint16)] * 4)
    assert all(l.size == 0 for l in C.unpack_luts(b))
    with pytest.raises(C.RsqFormatError):
        C.unpack_luts(b + b"\x00")
    m = np.random.default_rng(0).random((7, 13)) > 0.9
    assert np.array_equal(C.unpack_mask(C.pack_mask(m), m.shape), m)
    with pytest.raises(C.RsqFormatError):
        C.unpack_mask(b"\x00", (10, 10))
