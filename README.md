# rawsqueeze

Adaptive dual-engine camera RAW compressor. Encodes LibRaw-readable 2x2 Bayer raw files into a
`.rsq` container (JPEG XL payloads) and decodes back to standard DNG (LJ92) with full metadata.

See `docs/DESIGN.md` for the specification.

```
uv run rawsqueeze --help
uv run pytest -q            # fast tests
uv run pytest -q -m slow    # tests that need samples/*.RW2
```
