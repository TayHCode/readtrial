"""Bounded subprocesses; no shell interpolation and no inherited target secrets."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field


@dataclass
class Result:
    exit_code: int | None
    output: str
    seconds: float
    timed_out: bool = False
    truncated: bool = False
    denied: str = ""
    steps: list[dict] = field(default_factory=list)
    execution_error: str = ""
    execution_mode: str = "shell"

    def json(self) -> dict:
        return asdict(self)


def execute(argv: Sequence[str], *, timeout: float = 60, cap: int = 65536,
            data: bytes = b"", cwd: str | None = None,
            env: Mapping[str, str] | None = None, pass_fds: tuple[int, ...] = (),
            tempdir: str | None = None) -> Result:
    """Drain output while capping storage; terminate the entire process group."""
    started = time.monotonic()
    output = bytearray()
    timed_out = truncated = False
    # A file avoids deadlock if a child writes output before reading stdin.
    with tempfile.TemporaryFile(dir=tempdir) as source:
        source.write(data)
        source.seek(0)
        try:
            proc = subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, cwd=cwd, env=env,
                                    start_new_session=True, pass_fds=pass_fds)
        except OSError as exc:
            return Result(127, friendly_error(exc), time.monotonic() - started,
                          execution_error='process spawn failed')
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() - started >= timeout:
                        timed_out = True
                        break
                    for key, _ in selector.select(min(0.1, max(0, timeout - (time.monotonic() - started)))):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        remaining = cap - len(output)
                        output.extend(chunk[:max(0, remaining)])
                        if len(chunk) > remaining:
                            truncated = True
                            break
                    if truncated:
                        break
            # Allow a child that closed stdout to finish naturally within its
            # deadline. Then kill orphan descendants even if the child has exited.
            if not timed_out and not truncated:
                try:
                    proc.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    timed_out = True
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stdout.close()
    code = 124 if timed_out else 125 if truncated else proc.returncode
    return Result(code, decode_output(bytes(output)), round(time.monotonic() - started, 3),
                  timed_out, truncated)


def decode_output(data: bytes, oem: str = 'cp437') -> str:
    """Prefer UTF-8, recognize UTF-16 BOM, preserve OEM bytes without U+FFFD."""
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16', 'backslashreplace')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode(oem, 'backslashreplace')


def friendly_error(exc: OSError) -> str:
    if isinstance(exc, FileExistsError):
        return 'output folder already exists; choose a new --out folder'
    if isinstance(exc, FileNotFoundError):
        return 'required file or executable was not found: ' + str(exc.filename or 'unknown')
    if isinstance(exc, PermissionError):
        return 'permission denied for required file or executable'
    return 'file/process operation failed: ' + (exc.strerror or type(exc).__name__)
