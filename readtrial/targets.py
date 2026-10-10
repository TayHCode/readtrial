"""Fresh target lifecycles. Existing instances/profiles/services are never changed."""
from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .installers import prepare
from .process import Result, execute
from .proxy import Proxy
from .trusted import TrustedSSH


class TargetError(RuntimeError):
    pass


@dataclass
class Limits:
    command_seconds: int = 60
    total_seconds: int = 600
    output_bytes: int = 65536
    memory_mb: int = 512
    disk_mb: int = 1024
    cpu_seconds: int = 300
    max_steps: int = 20


class Target:
    def __init__(self, spec: str, limits: Limits, log, *, pool: str = "",
                 domains: set[str] | None = None, container_image: str = "readtrial-sandbox:latest",
                 trusted_target: bool = False, ssh_linux_ack: bool = False, instance_prefix: str = "readtrial-"):
        self.spec, self.limits, self.log = spec, limits, log
        self.pool, self.domains, self.container_image = pool, domains or set(), container_image
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", instance_prefix):
            raise TargetError("instance prefix must start with a lowercase letter and contain only letters, digits or hyphens (max 31)")
        self.id = instance_prefix + uuid.uuid4().hex
        self.base = "/tmp/" + self.id
        self.created = False
        self.platform = "Linux"
        self.trusted_target = trusted_target
        self.ssh_linux_ack = ssh_linux_ack
        self.trusted: TrustedSSH | None = None
        self.isolation = "not prepared"
        self.session: subprocess.Popen | None = None
        self.proxy: Proxy | None = None
        self.kind, _, self.value = spec.partition(":")
        self.network = "denied"
        self.repository = ""
        self.pending = bytearray()
        self.deadline = time.monotonic() + limits.total_seconds
        if self.kind == "ssh":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,200}", self.value):
                raise TargetError("SSH target must be a hostname or user@hostname")
            self.prefix = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                           "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
                           "-o", "ConnectTimeout=10", self.value]
        elif self.kind == "lxc" and self.value and not self.value.startswith("-"):
            self.prefix = ["lxc", "exec", self.id, "--"]
        elif spec == "local-container":
            engine = shutil.which("podman") or shutil.which("docker")
            if not engine:
                raise TargetError("local-container needs Podman or Docker")
            self.engine = engine
            self.prefix = [engine, "exec", "-i", self.id]
        else:
            raise TargetError("target must be ssh:HOST, lxc:IMAGE, or local-container")

    def outer(self, argv: list[str]) -> list[str]:
        # SSH concatenates arguments into a remote shell command. Quote every
        # token once; never interpolate agent/repository text into that shell.
        if self.kind == "ssh":
            return self.prefix + [shlex.join(argv)]
        return self.prefix + argv

    def management(self, argv: list[str], *, data: bytes = b"", timeout: int = 60) -> Result:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TargetError("total time limit exceeded during setup")
        result = execute(argv, data=data, timeout=min(timeout, remaining), cap=self.limits.output_bytes)
        self.log("setup", shlex.join(argv), result)
        if result.exit_code or result.truncated or result.timed_out:
            raise TargetError(result.output or "target setup failed")
        return result

    def prepare(self, bundle: Path) -> None:
        if self.kind == "ssh":
            self.platform = self.probe_platform()
            if self.platform in {"Darwin", "Windows"}:
                if not self.trusted_target:
                    raise TargetError(self.platform + " SSH requires --trusted-target; no OS confinement or address-space ceiling")
                self.trusted = TrustedSSH(self)
                self.isolation = "TRUSTED-TARGET: user permissions; command inspection only, no OS confinement"
                self.network = "explicit URL/host inspection only; repository subprocess networking is unrestricted"
                self.trusted.prepare(bundle)
                return
            if self.platform != "Linux":
                raise TargetError("unsupported SSH platform")
            if not self.ssh_linux_ack:
                raise TargetError("Linux SSH requires --i-understand-ssh-linux-is-not-confined; fresh folder, time/output limits and deny list only; no network/filesystem confinement")
            self.trusted = TrustedSSH(self)
            self.isolation = "SSH-LINUX-UNCONFINED: user permissions; fresh folder, time/output limits and deny list; no network/filesystem confinement"
            self.network = "explicit URL/host inspection only; repository subprocess networking is unrestricted"
            self.trusted.prepare(bundle)
            return
        if self.kind == "lxc":
            if not self.pool:
                pools = json.loads(self.management(["lxc", "storage", "list", "--format", "json"]).output)
                if len(pools) != 1:
                    raise TargetError("choose a quota-capable LXC storage pool with --lxc-pool")
                self.pool = pools[0]["name"]
            # Never inherit the default profile: it may mount homes or sockets.
            # Mark owned name before mutation so partial init is cleaned up too.
            self.created = True
            self.management(["lxc", "init", self.value, self.id, "--no-profiles", "-s", self.pool,
                             "-d", f"root,size={self.limits.disk_mb + 2048}MiB",
                             "-c", "security.privileged=false", "-c", "security.nesting=true",
                             "-c", "limits.cpu=1", "-c", f"limits.memory={self.limits.memory_mb}MiB",
                             "-c", "limits.processes=128"], timeout=180)
            self.proxy = Proxy(self.domains)
            self.proxy.start()
            self.management(["lxc", "config", "device", "add", self.id, "registry-proxy", "proxy",
                             "bind=container", "listen=tcp:127.0.0.1:18080",
                             f"connect=tcp:127.0.0.1:{self.proxy.port}"])
            self.management(["lxc", "start", self.id])
            self.management(self.outer(["sh", "-c",
                ("export http_proxy=http://127.0.0.1:18080 https_proxy=http://127.0.0.1:18080; "
                "export DEBIAN_FRONTEND=noninteractive; apt-get update && "
                "apt-get install -y --no-install-recommends python3 git curl bubblewrap ca-certificates util-linux")]),
                timeout=180)
            self.network = "registry/repository allowlist through loopback proxy; no NIC"
        elif self.kind == "local-container":
            self.created = True
            self.management([self.engine, "run", "-d", "--name", self.id, "--network", "none",
                             "--read-only", "--cap-drop", "ALL", "--pids-limit", "128",
                             "--cpus", "1", "--memory", f"{self.limits.memory_mb}m",
                             "--tmpfs", f"/tmp:rw,size={self.limits.disk_mb}m",
                             self.container_image, "sleep", str(self.limits.total_seconds + 60)])
        # This directory holds only the control scripts and immutable source
        # bundle. The stranger never sees the remote temp directory or home.
        self.management(self.outer(["mkdir", "-m", "700", self.base]))
        self.created = True
        for name in ("worker.py", "process.py", "installers.py", "shell.py"):
            self.put(self.base + "/" + name, Path(__file__).with_name(name).read_bytes())
        self.put(self.base + "/source.bundle", bundle.read_bytes())
        preflight = self.management(self.outer(["python3", "-c",
            ("import shutil,sys; sys.exit(2) if sys.version_info < (3,11) else None; "
            "print('\\n'.join(shutil.which(x) or '' for x in ('python3','bwrap','prlimit')))")]))
        if len([line for line in preflight.output.splitlines() if line.strip()]) < 3:
            raise TargetError("target requires Python 3.11+, bubblewrap with --size, and prlimit")
        bootstrap = (
            "import sys; sys.path.insert(0,sys.argv[1]); "
            "from worker import sandbox; import os; "
            "argv=sandbox(sys.argv[1],sys.argv[1]+'/source.bundle',int(sys.argv[2]),sys.argv[3]); "
            "os.execvp(argv[0],argv)"
        )
        proxy = "http://127.0.0.1:18080" if self.proxy else ""
        argv = ["timeout", "--signal=KILL", str(max(1, int(self.deadline - time.monotonic()) + 5)),
                "prlimit", f"--as={self.limits.memory_mb * 1024 * 1024}",
                f"--fsize={self.limits.disk_mb * 1024 * 1024}",
                f"--cpu={self.limits.cpu_seconds}", "--nofile=256", "--",
                "python3", "-c", bootstrap, self.base, str(self.limits.disk_mb), proxy]
        self.session = subprocess.Popen(self.outer(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        assert self.session.stdout
        os.set_blocking(self.session.stdout.fileno(), False)
        result = self.command("git -c core.hooksPath=/dev/null clone /source.bundle .", setup=True)
        if result.exit_code:
            raise TargetError(result.output or "sandbox session could not start")
        self.isolation = "Linux namespace/tmpfs and per-process limits"

    def probe_platform(self) -> str:
        """Fixed read-only probe; no work folder or repository command."""
        probe = execute(self.outer(['uname', '-s']), timeout=15, cap=4096)
        self.log('platform-probe', 'POSIX platform probe (failure expected on Windows; PowerShell fallback follows)', probe)
        if probe.exit_code == 0:
            return probe.output.strip()
        windows = execute(self.prefix + ['powershell -NoProfile -Command "[Environment]::OSVersion.Platform.ToString()"'], timeout=15, cap=4096)
        self.log('platform-probe', 'PowerShell platform probe', windows)
        if windows.exit_code == 0 and windows.output.strip() == 'Win32NT':
            return 'Windows'
        raise TargetError('SSH platform probe failed: ' + probe.output)

    def put(self, destination: str, data: bytes) -> None:
        script = "import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        # Before Python bootstrap (LXC) is complete, no writes occur.
        self.management(self.outer(["python3", "-c", script, destination]), data=data)

    def command(self, command: str, *, setup: bool = False, timeout: float | None = None) -> Result:
        if self.trusted:
            return self.trusted.command(command, setup=setup, timeout=timeout)
        if not self.session or not self.session.stdin or not self.session.stdout:
            raise TargetError("sandbox session is not running")
        runnable, _, denial = prepare(command, "/workspace", repository=self.repository)
        budget = timeout if timeout is not None else self.limits.command_seconds
        request = {"command": command, "timeout": budget,
                   "output_limit": self.limits.output_bytes, "repository": self.repository, "disposable": self.kind != "ssh", "setup": setup}
        try:
            self.session.stdin.write(json.dumps(request).encode() + b"\n")
            self.session.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise TargetError("sandbox session closed") from exc
        deadline = time.monotonic() + budget + 5
        cap = self.limits.output_bytes * 6 + 4096
        with selectors.DefaultSelector() as selector:
            selector.register(self.session.stdout, selectors.EVENT_READ)
            while b"\n" not in self.pending:
                if time.monotonic() >= deadline:
                    raise TargetError("sandbox transport timed out")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        raise TargetError(self.pending.decode("utf-8", "backslashreplace") or "sandbox closed")
                    self.pending.extend(chunk)
                    if len(self.pending) > cap:
                        raise TargetError("sandbox protocol exceeded output cap")
        line, _, remainder = self.pending.partition(b"\n")
        self.pending = bytearray(remainder)
        try:
            result = Result(**json.loads(line))
        except (ValueError, TypeError) as exc:
            raise TargetError(line.decode("utf-8", "backslashreplace")) from exc
        if not setup and not denial and runnable != command:
            self.log('installation-rewrite', runnable, result)
        self.log("setup" if setup else "command", command, result)
        return result

    def close(self) -> None:
        if self.trusted:
            self.trusted.close()
            return
        errors = []
        if self.session:
            if self.session.stdin:
                self.session.stdin.close()
            try:
                self.session.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.session.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.session.wait(timeout=3)
            if self.session.stdout:
                self.session.stdout.close()
        if self.created:
            if self.kind == "lxc":
                argv = ["lxc", "delete", "--force", self.id]
            elif self.kind == "local-container":
                argv = [self.engine, "rm", "-f", self.id]
            else:
                argv = self.outer(["rm", "-rf", "--", self.base])
            result = execute(argv, timeout=30, cap=self.limits.output_bytes)
            self.log("cleanup", shlex.join(argv), result)
            # A failed create may leave no instance; confirm absence rather than
            # treating a "not found" cleanup error as proof of a leak.
            if result.exit_code and self.kind == "lxc":
                check = execute(["lxc", "list", self.id, "--format", "json"], timeout=10)
                if check.exit_code == 0 and json.loads(check.output) == []:
                    result = Result(0, "instance absent", 0)
                    self.log("cleanup-confirmation", "confirm instance absent", result)
            if result.exit_code:
                errors.append("could not remove owned target: " + result.output)
            elif self.kind == "ssh":
                check = execute(self.outer(["test", "!", "-e", self.base]), timeout=10)
                self.log("cleanup-confirmation", "confirm temporary directory absent", check)
                if check.exit_code:
                    errors.append("temporary directory cleanup not confirmed")
            elif self.kind == "lxc":
                check = execute(["lxc", "list", self.id, "--format", "json"], timeout=10)
                self.log("cleanup-confirmation", "confirm owned instance absent", check)
                if check.exit_code or json.loads(check.output) != []:
                    errors.append("container cleanup not confirmed")
        if self.proxy:
            self.proxy.close()
        if errors:
            raise TargetError("; ".join(errors))
