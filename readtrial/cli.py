"""Small command line interface. Trial output directories must be new."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .process import friendly_error
from .targets import Limits
from .trial import Agent, checks, run


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Test README commands on disposable targets; never on the host.")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="mode", required=True)
    start = sub.add_parser("run", help="try a repository as a stranger")
    start.add_argument("repo", help="credential-free HTTPS repository URL or local Git path (committed HEAD)")
    start.add_argument("--ref", default="", help="full initial commit identity; rerun uses latest HEAD")
    start.add_argument("--target", action="append", required=True, help="lxc:IMAGE, ssh:HOST, or local-container")
    start.add_argument("--trusted-target", action="store_true", help="required for macOS/Windows SSH: user permissions, no OS confinement")
    start.add_argument("--i-understand-ssh-linux-is-not-confined", dest="ssh_linux_ack", action="store_true", help="required for Linux SSH: user permissions; fresh folder, limits, deny list; no network/filesystem confinement")
    start.add_argument("--instance-prefix", default="readtrial-", help="recognizable lowercase LXC instance prefix (max 31 characters; complete name max 63)")
    start.add_argument("--dry-run", action="store_true", help="print configuration and planned actions; execute nothing")
    start.add_argument("--agent-command", required=True, help="trusted, tool-disabled CLI argv template; prompt arrives on stdin")
    start.add_argument("--check", action="append", default=[], help="independent command that must exit zero")
    start.add_argument("--match", action="append", default=[], help="regex for each --check, in order")
    start.add_argument("--lxc-pool", default="", help="quota-capable existing storage pool; auto-select only if unique")
    start.add_argument("--container-image", default="readtrial-sandbox:latest", help="prebuilt Python/git/bwrap/prlimit OCI image")
    for flag, default in (("command-seconds", 60), ("total-seconds", 600), ("output-bytes", 65536),
                          ("memory-mb", 512), ("disk-mb", 1024), ("cpu-seconds", 300), ("max-steps", 20)):
        start.add_argument("--" + flag, type=positive, default=default)
    start.add_argument("--out", required=True, type=Path, help="new private output directory")
    repeat = sub.add_parser("rerun", help="repeat saved targets/checks against latest HEAD and diff advisory findings")
    repeat.add_argument("previous", type=Path)
    repeat.add_argument("--out", required=True, type=Path)
    repeat.add_argument("--dry-run", action="store_true", help="print saved plan without execution")
    demo = sub.add_parser("demo", help="offline fake agent/target FAIL then PASS loop (under 20 seconds)")
    demo.add_argument("--out", default=Path('trial-results/demo'), type=Path, help="new demo evidence folder (default: trial-results/demo)")
    return root


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    try:
        if args.mode == "demo":
            from .demo import run_demo
            return run_demo(args.out.expanduser().resolve())
        if args.mode == "rerun":
            prior = args.previous.expanduser().resolve()
            saved = json.loads((prior / "run.json").read_text())
            if saved.get("version") != 1 or saved.get("error"):
                raise ValueError("previous run is incompatible or did not fetch source successfully")
            config = saved["config"]
            print("Rerun will execute the saved trusted agent command and checks.")
        else:
            prior = None
            if args.match and len(args.match) != len(args.check):
                raise ValueError("supply one --match per --check, or omit --match")
            limits = Limits(**{key: getattr(args, key) for key in Limits.__dataclass_fields__})
            if limits.output_bytes > 131072 or limits.max_steps > 100:
                raise ValueError("output cap must be <=131072; max steps <=100")
            Agent(args.agent_command, limits)  # Validate CLI policy before target creation.
            repo = args.repo if "://" in args.repo else str(Path(args.repo).expanduser().resolve())
            config = {"repo": repo, "targets": args.target, "agent_command": args.agent_command,
                      "ref": args.ref,
                      "trusted_target": args.trusted_target, "ssh_linux_ack": args.ssh_linux_ack,
                      "instance_prefix": args.instance_prefix,
                      "checks": checks([{"command": command, "match": args.match[index] if args.match else None}
                                       for index, command in enumerate(args.check)]),
                      "limits": asdict(limits), "lxc_pool": args.lxc_pool, "container_image": args.container_image}
        if args.dry_run:
            print(json.dumps({'dry_run': True, 'config': config, 'out': str(args.out.expanduser().resolve()),
                              'actions': ['snapshot committed source', 'create fresh target(s)', 'read numbered README',
                                          'ask tool-disabled agent for commands', 'validate and execute command segments',
                                          'run independent declared checks', 'write reports/diff and verify owned-resource cleanup']}, indent=2))
            return 0
        return run(config, args.out.expanduser().resolve(), prior=prior)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        root.exit(2, "readtrial: " + (friendly_error(exc) if isinstance(exc, OSError) else str(exc)) + "\n")
