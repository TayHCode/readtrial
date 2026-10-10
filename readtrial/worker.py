"""Linux bubblewrap runner; only its text policy is imported on the controller."""
from __future__ import annotations

import ctypes
import importlib
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

runner = importlib.import_module(".process", __package__) if __package__ else importlib.import_module("process")
Result, execute = runner.Result, runner.execute
installers = importlib.import_module(".installers", __package__) if __package__ else importlib.import_module("installers")
shell = installers.shell

SYSTEM = {"sudo", "su", "doas", "apt", "apt-get", "dpkg", "dnf", "yum", "apk",
          "pacman", "zypper", "systemctl", "service", "crontab", "mount", "umount",
          "reboot", "shutdown", "lxc", "docker", "podman", "ssh", "scp"}
SENSITIVE = (".ssh", ".aws", ".git-credentials", ".netrc", "id_rsa", "id_ed25519",
             "credentials", "docker.sock")


def segment_policy(command: str, disposable: bool = False) -> str:
    """Conservative hints only. OS confinement is the authoritative boundary."""
    if not command.strip() or len(command) > 8192 or "\x00" in command:
        return "empty, invalid, or oversized command"
    if any(word in command.lower() for word in SENSITIVE):
        return "credential path"
    if re.search(r"\b922[2-6]\b", command):
        return "reserved browser port"
    if re.search(r"(^|[\s'\"=;])(?:~|/(?:home|root|etc|proc|sys|dev|run|var|tmp|Users|private|Volumes)(?:/|\b))", command):
        return "path outside workspace"
    visible = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>;)]+", "", command)
    visible = re.sub(r"/workspace(?=[/\s'\";]|$)", ".", visible)
    if re.search(r"(?:^|[\s'\"=><;])/(?!dev/null(?:[\s'\";]|$))", visible):
        return "absolute path outside workspace"
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return "malformed shell quoting"
    if any(Path(word).name in {"sudo", "su", "doas"} for word in words):
        return "sudo/admin elevation"
    if any(Path(word).name in SYSTEM for word in words):
        return "system or remote administration command"
    if any(Path(word).name in {"sudo", "su", "doas", "systemctl", "service", "crontab", "mount", "lxc",
                              "docker", "podman", "ssh", "scp", "reboot"} for word in words):
        return "service, mount, or remote administration command"
    return ""


def policy(command: str, disposable: bool = False, *, root: Path | None = None, windows: bool = False) -> str:
    try:
        segments, _ = shell.split(command, windows)
    except ValueError as exc:
        return str(exc)
    reason = shell.path_policy(command, root, windows)
    if not windows and re.search(r'\.\.\\', command):
        reason = 'path outside workspace (non-POSIX separator)'
    if reason:
        return reason
    for index, segment in enumerate(segments, 1):
        reason = segment_policy(segment, disposable)
        if reason:
            return shell.denied(index, segment, reason)
    return ''


def sandbox(control: str, bundle: str, disk_mb: int, proxy: str = "") -> list[str]:
    argv = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all",
            "--unshare-user", "--disable-userns", "--cap-drop", "ALL",
            "--ro-bind", "/usr", "/usr"]
    for path in ("/bin", "/sbin", "/lib", "/lib64"):
        if Path(path).is_symlink():
            argv += ["--symlink", os.readlink(path), path]
        elif Path(path).exists():
            argv += ["--ro-bind", path, path]
    for path in ("/etc/ssl/certs", "/etc/alternatives", "/etc/ld.so.cache"):
        if Path(path).exists():
            argv += ["--ro-bind", path, path]
    argv += ["--proc", "/proc", "--dev", "/dev", "--dir", "/tmp",
             "--ro-bind", control, "/control", "--ro-bind", bundle, "/source.bundle",
             "--size", str(disk_mb * 1024 * 1024), "--tmpfs", "/workspace",
             "--chdir", "/workspace", "--remount-ro", "/", "--remount-ro", "/dev", "--clearenv",
             "--setenv", "PATH", "/workspace/bin:/workspace/repo/.venv/bin:/usr/local/bin:/usr/bin:/bin",
             "--setenv", "HOME", "/workspace/home", "--setenv", "TMPDIR", "/workspace/tmp",
             "--setenv", "LANG", "C.UTF-8", "--setenv", "GIT_CONFIG_NOSYSTEM", "1",
             "--setenv", "GIT_CONFIG_GLOBAL", "/dev/null",
             "--setenv", "PIP_DISABLE_PIP_VERSION_CHECK", "1"]
    for name, value in installers.environment("/workspace").items():
        argv += ["--setenv", name, value]
    if proxy:
        argv += ["--share-net"]
        for variable in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            argv += ["--setenv", variable, proxy]
    argv += ["--", "python3", "/control/worker.py"]
    return argv


def protect_control() -> None:
    # Commands share a PID namespace with this protocol worker. Non-dumpable
    # control prevents them opening its /proc file descriptors or memory.
    if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
        raise OSError("cannot protect runner control process")


def main() -> int:
    protect_control()
    # A persistent private tmpfs retains the stranger's working files between
    # commands. Nothing from that filesystem is mounted on the host.
    for name in ("home", "tmp", "repo", "bin"):
        Path(name).mkdir()
    for line in sys.stdin.buffer:
        if len(line) > 131072:
            return 2
        request = json.loads(line)
        command = request["command"]
        runnable, inspected, denial = installers.prepare(command, "/workspace", repository=request.get("repository", ""))
        if request.get("setup"):
            runnable, denial = command, ""
        elif not denial:
            denial = policy(inspected, request["disposable"], root=Path('/workspace/repo'))
        if denial:
            result = Result(126, "", 0, denied=denial)
        else:
            # Limits are applied by the controller to this whole session, with
            # wall time/output bounds per command and a hard tmpfs disk quota.
            with tempfile.TemporaryFile() as status:
                fd = status.fileno()
                script = runnable if request.get('setup') else shell.posix_script(runnable, f'/dev/fd/{fd}')
                result = execute(["sh", "-c", script], timeout=request["timeout"],
                                 cap=request["output_limit"], cwd="/workspace/repo", pass_fds=(fd,))
                if not request.get('setup'):
                    status.seek(0)
                    result.steps = shell.statuses(status.read().decode('ascii'), runnable)
                    failed = next((step['exit_code'] for step in result.steps if step['exit_code']), 0)
                    if failed and not result.exit_code:
                        result.exit_code = failed
        print(json.dumps(result.json()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
