"""External command-line tools: discovery, version detection, subprocess helpers, ExifTool.

Tools used by rawsqueeze: ``exiftool`` (metadata), ``cjxl``/``djxl`` (preview transcoding),
``ssimulacra2`` and ``butteraugli_main`` (verify/bench).  All are optional at import time;
callers check :func:`which` / :func:`have` and degrade gracefully.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Sequence
from typing import Any

KNOWN_TOOLS: tuple[str, ...] = ("exiftool", "cjxl", "djxl", "ssimulacra2", "butteraugli_main")

# Environment override: RAWSQUEEZE_<TOOL> (upper case) may point at a specific binary.


class ToolError(RuntimeError):
    """An external tool is missing or failed."""


@functools.cache
def default_file_mode() -> int:
    """0o666 minus the process umask (``tempfile.mkstemp`` creates 0o600 files)."""
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def which(name: str) -> str | None:
    """Absolute path of tool ``name`` or ``None``.

    ``$RAWSQUEEZE_<NAME>`` (e.g. ``RAWSQUEEZE_EXIFTOOL``) overrides the PATH lookup.
    Cached; call ``which.cache_clear()`` after changing the environment.
    """
    env = os.environ.get(f"RAWSQUEEZE_{name.upper()}")
    if env:
        return env if os.path.exists(env) else shutil.which(env)
    return shutil.which(name)


def have(name: str) -> bool:
    """True if tool ``name`` is available."""
    return which(name) is not None


def require(name: str) -> str:
    """Path of tool ``name``; raises :class:`ToolError` if missing."""
    path = which(name)
    if path is None:
        raise ToolError(f"required tool {name!r} not found on PATH")
    return path


_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


@functools.cache
def tool_version(name: str) -> str | None:
    """Version string of a tool, or ``None`` if missing/undetectable.

    exiftool: ``exiftool -ver`` (e.g. ``"13.55"``); cjxl/djxl: ``--version``
    (e.g. ``"0.12.0"``).  ssimulacra2 and butteraugli_main have no version flag;
    they ship with libjxl, so the cjxl version is reported for them when available.
    """
    path = which(name)
    if path is None:
        return None
    if name == "exiftool":
        args = [path, "-ver"]
    elif name in ("ssimulacra2", "butteraugli_main"):
        return tool_version("cjxl")
    else:
        args = [path, "--version"]
    try:
        cp = subprocess.run(args, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    mt = _VERSION_RE.search(text)
    return mt.group(1) if mt else None


def tool_versions() -> dict[str, str | None]:
    """``{tool: version-or-None}`` for all :data:`KNOWN_TOOLS`."""
    return {t: tool_version(t) for t in KNOWN_TOOLS}


def run(
    args: Sequence[str | os.PathLike[str]],
    *,
    input: bytes | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command capturing stdout/stderr as bytes.

    ``args[0]`` may be a bare tool name; it is resolved through :func:`which`.
    Raises :class:`ToolError` if the tool is missing or (``check=True``) exits non-zero.
    """
    argv = [os.fspath(a) for a in args]
    if not argv:
        raise ValueError("empty command")
    if os.sep not in argv[0]:
        argv[0] = require(argv[0])
    try:
        cp = subprocess.run(argv, input=input, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{argv[0]} timed out after {timeout}s") from exc
    if check and cp.returncode != 0:
        err = cp.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"{os.path.basename(argv[0])} exited with {cp.returncode}: {err[:2000]}")
    return cp


class _PipeReader:
    """Background thread that drains a pipe into a buffer (prevents pipe-full deadlocks)."""

    def __init__(self, pipe: Any) -> None:
        self._pipe = pipe
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._eof = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        fd = self._pipe.fileno()
        while True:
            try:
                data = os.read(fd, 1 << 16)
            except OSError:
                data = b""
            with self._cond:
                if not data:
                    self._eof = True
                    self._cond.notify_all()
                    return
                self._buf += data
                self._cond.notify_all()

    def read_until(self, marker: bytes, timeout: float | None) -> bytes:
        """Wait until the buffer contains ``marker``; return bytes before it and consume both."""
        with self._cond:
            start = 0
            while True:
                idx = self._buf.find(marker, start)
                if idx >= 0:
                    out = bytes(self._buf[:idx])
                    del self._buf[: idx + len(marker)]
                    return out
                if self._eof:
                    raise ToolError("exiftool process terminated unexpectedly")
                start = max(0, len(self._buf) - len(marker))
                if not self._cond.wait(timeout):
                    raise ToolError(f"exiftool did not respond within {timeout}s")

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)


class ExifTool:
    """Persistent ``exiftool -stay_open True -@ -`` process.

    Usage::

        with ExifTool() as et:
            text = et.run(["-n", "-ISO", "a.RW2"])          # str (stdout)
            blob = et.run_bytes(["-b", "-JpgFromRaw", "a.RW2"])  # bytes
            info = et.run_json(["-n", "-ISO", "-Make", "a.RW2"])  # list[dict]

    One instance serialises commands with a lock (thread-safe, but not parallel); use one
    instance per worker for parallelism.  Arguments are passed one per line, so they
    must not contain newlines.  exiftool's stderr for the last command is available as
    :attr:`last_stderr`.  Exit status is not available in stay_open mode; callers judge
    success from output (``run_json`` raises on unparsable output).
    """

    def __init__(
        self,
        executable: str | None = None,
        *,
        common_args: Sequence[str] = (),
        timeout: float | None = 120.0,
    ) -> None:
        self.executable = executable or which("exiftool")
        if self.executable is None:
            raise ToolError("exiftool not found on PATH")
        self.common_args = list(common_args)
        self.timeout = timeout
        self.last_stderr = ""
        self._lock = threading.Lock()
        self._counter = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._out: _PipeReader | None = None
        self._err: _PipeReader | None = None

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> ExifTool:
        if self._proc is not None:
            return self
        # common_args are prepended to every command (see run_bytes) rather than passed
        # via -common_args, so they can be changed between calls.
        argv = [self.executable, "-stay_open", "True", "-@", "-"]
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._out = _PipeReader(self._proc.stdout)
        self._err = _PipeReader(self._proc.stderr)
        return self

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        """Ask exiftool to exit and reap it (kills it after a timeout)."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write(b"-stay_open\nFalse\n")
                proc.stdin.flush()
                proc.stdin.close()
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
        finally:
            for r in (self._out, self._err):
                if r is not None:
                    r.join(1.0)
            for s in (proc.stdout, proc.stderr):
                if s is not None:
                    s.close()
            self._out = self._err = None

    def __enter__(self) -> ExifTool:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- commands ----------------------------------------------------------------------
    def run_bytes(self, args: Sequence[str | os.PathLike[str]]) -> bytes:
        """Execute one command; return raw stdout bytes."""
        argv = list(self.common_args) + [os.fspath(a) for a in args]
        for a in argv:
            if "\n" in a or "\r" in a:
                raise ValueError(f"exiftool argument contains a newline: {a!r}")
        with self._lock:
            if not self.running:
                self._proc = None
                self.start()
            assert self._proc is not None and self._proc.stdin is not None
            assert self._out is not None and self._err is not None
            self._counter += 1
            n = self._counter
            marker = f"{{ready{n}}}"
            payload = "\n".join([*argv, "-echo4", marker, f"-execute{n}"]) + "\n"
            try:
                self._proc.stdin.write(payload.encode("utf-8"))
                self._proc.stdin.flush()
            except OSError as exc:
                raise ToolError(f"cannot write to exiftool: {exc}") from exc
            out = self._out.read_until((marker + "\n").encode(), self.timeout)
            err = self._err.read_until((marker + "\n").encode(), self.timeout)
            self.last_stderr = err.decode("utf-8", errors="replace")
            return out

    def run(self, args: Sequence[str | os.PathLike[str]]) -> str:
        """Execute one command; return stdout decoded as UTF-8."""
        return self.run_bytes(args).decode("utf-8", errors="replace")

    def run_json(self, args: Sequence[str | os.PathLike[str]]) -> list[dict[str, Any]]:
        """Execute with ``-j`` prepended; return the parsed JSON list.

        Raises :class:`ToolError` if exiftool produced no JSON (e.g. file not found);
        the message includes exiftool's stderr.
        """
        out = self.run(["-j", *[os.fspath(a) for a in args]])
        if not out.strip():
            raise ToolError(f"exiftool returned no output: {self.last_stderr.strip()}")
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise ToolError(f"exiftool returned invalid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise ToolError("exiftool JSON output is not a list")
        return data

    def version(self) -> str:
        """exiftool version via the running process."""
        return self.run(["-ver"]).strip()


def exiftool_json(
    path: str | os.PathLike[str],
    tags: Sequence[str] = (),
    *,
    numeric: bool = True,
    et: ExifTool | None = None,
) -> dict[str, Any] | None:
    """One-shot ``exiftool -j [-n] -TAG... path``; returns the first record or ``None``.

    Uses ``et`` when given, otherwise a one-off subprocess.  Returns ``None`` if exiftool
    is missing or fails.
    """
    args = (["-n"] if numeric else []) + [f"-{t}" for t in tags] + [os.fspath(path)]
    try:
        if et is not None:
            data = et.run_json(args)
        else:
            if not have("exiftool"):
                return None
            cp = run(["exiftool", "-j", *args], check=False, timeout=60)
            if not cp.stdout.strip():
                return None
            data = json.loads(cp.stdout)
    except (ToolError, json.JSONDecodeError, OSError):
        return None
    if not data:
        return None
    rec = dict(data[0])
    rec.pop("SourceFile", None)
    return rec
