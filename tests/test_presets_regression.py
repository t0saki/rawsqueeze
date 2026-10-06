"""Preset regression on the full-size samples (DESIGN.md 7.13; slow).

Sizes must stay within +-10 % of the DESIGN.md reference figures and every lossy result
must meet its DESIGN.md 6.4 acceptance criteria (4 tiles, EV 0/+3, no FLOOR to save time).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from rawsqueeze.pipeline import decode_mosaic, encode_file
from rawsqueeze.tools import have
from rawsqueeze.verify import verify

pytestmark = pytest.mark.slow

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

# (sample, preset) -> (expected engine, reference MB from DESIGN.md 1.3 / 4.1)
REFERENCE: dict[tuple[str, str], tuple[str, float]] = {
    ("P1060444", "lossless"): ("nlq", 17.837),
    ("P1060444", "high"): ("half3", 7.49),
    ("P1060444", "vl"): ("half3", 4.89),
    ("P1060444", "compact"): ("half3", 3.70),
    ("P1037920", "lossless"): ("nlq", 20.200),
    ("P1037920", "high"): ("nlq", 11.45),
    ("P1037920", "vl"): ("nlq", 8.53),
    ("P1037920", "compact"): ("nlq", 6.01),
}


@pytest.fixture(scope="module")
def encoded(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], tuple[Path, object]]:
    out: dict[tuple[str, str], tuple[Path, object]] = {}
    d = tmp_path_factory.mktemp("regr")
    for (s, p) in REFERENCE:
        src = SAMPLES / f"{s}.RW2"
        if not src.exists():
            pytest.skip(f"sample {src.name} not available")
        dst = d / f"{s}_{p}.rsq"
        out[(s, p)] = (dst, encode_file(src, dst, preset=p))
    return out


@pytest.mark.parametrize("key", list(REFERENCE), ids=lambda k: f"{k[0]}-{k[1]}")
def test_size_and_engine(encoded: dict, key: tuple[str, str]) -> None:
    engine, ref_mb = REFERENCE[key]
    _, rep = encoded[key]
    assert rep.engine == engine
    mb = rep.out_size / 1e6
    assert abs(mb / ref_mb - 1) <= 0.10, f"{key}: {mb:.3f} MB vs reference {ref_mb} MB"


@pytest.mark.parametrize("sample", ["P1060444", "P1037920"])
def test_lossless_bit_exact(encoded: dict, sample: str) -> None:
    import rawpy

    path, _ = encoded[(sample, "lossless")]
    with rawpy.imread(str(SAMPLES / f"{sample}.RW2")) as r:
        ref = np.array(r.raw_image)
    rec = decode_mosaic(path)
    assert hashlib.sha256(rec.tobytes()).hexdigest() == hashlib.sha256(ref.tobytes()).hexdigest()


@pytest.mark.skipif(not (have("ssimulacra2") and have("butteraugli_main")), reason="metric tools missing")
@pytest.mark.parametrize("key", [k for k in REFERENCE if k[1] != "lossless"], ids=lambda k: f"{k[0]}-{k[1]}")
def test_acceptance(encoded: dict, key: tuple[str, str]) -> None:
    import rawpy

    from rawsqueeze.container import read_rsq

    path, _ = encoded[key]
    rsq = read_rsq(path)
    with rawpy.imread(str(SAMPLES / f"{key[0]}.RW2")) as r:
        orig = np.array(r.raw_image)
    rec = decode_mosaic(rsq)
    rep = verify(orig, rec, rsq.head, (0.0, 3.0), 4, floor=False)
    assert rep.clip["inconsistent"] == 0
    assert rep.acceptance["criteria"] is not None
    failures = list(rep.acceptance["failures"])
    assert not failures, f"{key}: {failures}"
