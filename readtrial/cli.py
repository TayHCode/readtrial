"""Small command line interface. Trial output directories must be new."""
from __future__ import annotations

import argparse
import json
import sys
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
    start.add_argument('--dedicated-account', action='store_true', help='macOS SSH only: reject privileged/shared-group accounts; journal owned HOME and cleanup')
    start.add_argument("--i-understand-ssh-linux-is-not-confined", dest="ssh_linux_ack", action="store_true", help="required for Linux SSH: user permissions; fresh folder, limits, deny list; no network/filesystem confinement")
    start.add_argument("--instance-prefix", default="readtrial-", help="recognizable lowercase LXC instance prefix (max 31 characters; complete name max 63)")
    start.add_argument("--dry-run", action="store_true", help="print configuration and planned actions; execute nothing")
    start.add_argument("--agent-command", required=True, help="trusted, tool-disabled CLI argv template; prompt arrives on stdin")
    start.add_argument("--check", action="append", default=[], help="independent command that must exit zero")
    start.add_argument("--match", action="append", default=[], help="regex for each --check, in order")
    start.add_argument("--lxc-pool", default="", help="quota-capable existing storage pool; auto-select only if unique")
    start.add_argument("--container-image", default="readtrial-sandbox:latest", help="prebuilt Python/git/bwrap/prlimit OCI image")
    start.add_argument('--protected-path', action='append', default=[], type=Path, help='explicit benign controller-local file or directory to fingerprint')
    start.add_argument('--readonly-input', action='append', default=[], type=Path, help='controller-local immutable input to fingerprint; never mounted writable')
    start.add_argument('--denied-endpoint', action='append', default=[], help='additional denied HOST:PORT; stored only in local run configuration')
    for flag, default in (("command-seconds", 60), ("total-seconds", 600), ("output-bytes", 65536),
                          ("memory-mb", 512), ("disk-mb", 1024), ("cpu-seconds", 300), ("max-steps", 20)):
        start.add_argument("--" + flag, type=positive, default=default)
    start.add_argument("--out", required=True, type=Path, help="new restricted output directory")
    verification = sub.add_parser('verify', help='independently exercise a trusted local product and save Factory proof')
    verification.add_argument('project', type=Path, help='local Git project; uses committed HEAD in a fresh copy')
    verification.add_argument('--requirements', required=True, type=Path, help='JSON array of {id, text, optional expect_exit, optional not_verifiable_in_sandbox reason}')
    verification.add_argument('--job', type=Path, help='optional job: stage verify, route.model, source_hash, required_check_ids, evidence_dir, optional recipe')
    verification.add_argument('--model', help='exact verifier model id (or job.route.model)')
    verification.add_argument('--builder-model', required=True, help='recorded builder model; must differ from verifier')
    verification.add_argument('--source-hash', help='SHA256 source fingerprint supplied by caller (or job.source_hash)')
    backend = verification.add_mutually_exclusive_group(required=True)
    backend.add_argument('--agent-command', help='same trusted, tool-disabled agent CLI policy as run')
    backend.add_argument('--scripted-plan', type=Path, help='offline rehearsal actions, requires model scripted-verifier; no model calls')
    verification.add_argument('--trusted-local', action='store_true', help='acknowledge user permissions, no filesystem/network confinement')
    verification.add_argument('--independent-agent', action='store_true', help='attest this verifier did not build the product')
    verification.add_argument('--recipe', type=Path, help='saved recipe.json, including Factory recipe/source_hash wrapper')
    verification.add_argument('--keep-running', action='store_true', help='require the owned launch process to stay alive between steps')
    verification.add_argument('--out', required=True, type=Path, help='new private evidence folder; Factory: beneath job.evidence_dir')
    for flag, default in (('command-seconds', 60), ('total-seconds', 600), ('output-bytes', 65536), ('max-steps', 80)):
        verification.add_argument('--' + flag, type=positive, default=default)
    validation = sub.add_parser('validate-verify', help='check result shape, job binding and proof hashes; never decide a gate')
    validation.add_argument('result', type=Path, help='finished verify.json')
    validation.add_argument('--job', required=True, type=Path, help='verify job with required_check_ids')
    diagnostic = sub.add_parser('doctor', help='read-only isolation prerequisite checks; exit 0 ready, 1 missing, 2 error')
    diagnostic.add_argument('--test-user', default='', help='designated dedicated standard macOS trial account')
    diagnostic.add_argument('--target', default='', help='probe a dedicated macOS account through ssh:USER@HOST or ssh:ALIAS')
    attestation = sub.add_parser('attest-sudo', help='administrator audit of sudo denial; writes one root-owned test-home attestation')
    attestation.add_argument('--test-user', required=True)
    attestation.add_argument('--yes', action='store_true')
    setup = sub.add_parser('setup-linux', help='create one size-capped LXD pool; requires --yes')
    setup.add_argument('--pool', default='readtrial')
    setup.add_argument('--size-gib', type=positive, default=30)
    setup.add_argument('--location', default='', help='new absolute loop-file location; default managed by LXD')
    setup.add_argument('--yes', action='store_true')
    discover = sub.add_parser('leftovers', help='read-only discovery of exact journaled identities; removes nothing')
    discover.add_argument('journals', type=Path, help='target output .isolation/journals directory')
    recovery = sub.add_parser('recover', help='delete only exact journal-owned LXC instances; requires --yes')
    recovery.add_argument('journals', type=Path)
    recovery.add_argument('--yes', action='store_true')
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
    if args.mode == 'validate-verify':
        from .verify_result import validate_result
        try:
            count = validate_result(args.result, json.loads(args.job.read_text()))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            reason = friendly_error(exc) if isinstance(exc, OSError) else str(exc)
            print('Invalid verify result: ' + ' '.join(reason.splitlines()))
            return 2
        print('Valid verify result: ' + str(count) + ' checks; shape and proof hashes match the job.')
        return 0
    try:
        if args.mode == 'verify':
            from .verify import PlanAgent, verify
            job = json.loads(args.job.read_text()) if args.job else {}
            if args.job and job.get('stage') != 'verify':
                raise ValueError('--job must describe a verify job')
            items = json.loads(args.requirements.read_text())
            required = job.get('required_check_ids')
            if items == []:
                items = [{'id': 'smoke', 'text': 'Invoke the product and observe successful startup.'}]
            if required and 'regression-smoke' in required and not any(item.get('id') == 'regression-smoke' for item in items):
                items.append({'id': 'regression-smoke', 'text': 'Invoke the real product in local isolated state and prove it still starts.'})
            recipe_path = args.recipe or job.get('recipe')
            recipe = json.loads(Path(recipe_path).read_text()) if recipe_path else None
            model = args.model or job.get('route', {}).get('model')
            source_hash = args.source_hash or job.get('source_hash')
            if args.job and (model != job.get('route', {}).get('model') or source_hash != job.get('source_hash')):
                raise ValueError('model and source hash overrides must match the supplied job')
            if job.get('evidence_dir'):
                boundary = Path(job['evidence_dir']).resolve()
                if boundary not in args.out.absolute().resolve().parents:
                    raise ValueError('--out must be a new folder beneath job.evidence_dir')
            backend_options = {}
            if args.scripted_plan:
                if model != 'scripted-verifier':
                    raise ValueError('--scripted-plan requires --model scripted-verifier; it cannot impersonate a model')
                backend_options['agent_factory'] = lambda template, limits, **options: PlanAgent(args.scripted_plan, limits, **options)
            evidence, code = verify(args.project, items, args.out, model=model,
                                    builder_model=args.builder_model, source_hash=source_hash,
                                    agent_command=args.agent_command or 'scripted-plan', trusted_local=args.trusted_local,
                                    independent_agent=args.independent_agent, recipe=recipe,
                                    keep_running=args.keep_running, required_check_ids=required,
                                    limits=Limits(command_seconds=args.command_seconds, total_seconds=args.total_seconds,
                                                  output_bytes=args.output_bytes, max_steps=args.max_steps), **backend_options)
            print(str(args.out.absolute() / 'verify.json'))
            print('Recorded ' + str(len(evidence['checks'])) + ' requirements; Factory decides gate passage.')
            for problem in evidence.get('tool_problems', []):
                print(problem['tool'] + ': ' + problem['problem'], file=sys.stderr)
            return code
        if args.mode == 'doctor':
            from .setup import doctor
            report = doctor(test_user=args.test_user, target=args.target)
            print('Isolation prerequisites for ' + report['os'])
            for item in report['checks']:
                print(('OK: ' if item['ready'] else 'Missing: ') + item['check'])
                if item['fix']:
                    print(item['fix'])
            if report.get('instructions'):
                print('Only Missing items need action. Setup reference follows; preserve existing accounts and keys.')
                print(report['instructions'])
            return 0 if report['ready'] else 1
        if args.mode == 'attest-sudo':
            from .account import attest_sudo
            return attest_sudo(args.test_user, yes=args.yes)
        if args.mode == 'setup-linux':
            from .setup import setup_linux
            return setup_linux(pool=args.pool, size_gib=args.size_gib, location=args.location, yes=args.yes)
        if args.mode == 'recover':
            from .isolation import recover
            results = recover(args.journals.expanduser().absolute(), yes=args.yes)
            print(json.dumps({'resources': results}, indent=2))
            return 0
        if args.mode == 'leftovers':
            from .isolation import leftovers
            results = leftovers(args.journals.expanduser().absolute())
            print(json.dumps({'resources': results}, indent=2))
            return 2 if any(item['status'] != 'absent' for item in results) else 0
        if args.mode == "demo":
            from .demo import run_demo
            return run_demo(args.out.expanduser().resolve())
        if args.mode == "rerun":
            prior = args.previous.expanduser().resolve()
            saved = json.loads((prior / "run.json").read_text())
            if saved.get("version") != 2 or saved.get("error"):
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
                      'dedicated_account': args.dedicated_account,
                      "instance_prefix": args.instance_prefix,
                      "checks": checks([{"command": command, "match": args.match[index] if args.match else None}
                                       for index, command in enumerate(args.check)]),
                      "limits": asdict(limits), "lxc_pool": args.lxc_pool, "container_image": args.container_image}
            config.update(isolation_version=1,
                          protected_paths=[str(path.expanduser().absolute()) for path in args.protected_path],
                          readonly_inputs=[str(path.expanduser().absolute()) for path in args.readonly_input],
                          denied_endpoints=args.denied_endpoint)
        if config.get('isolation_version') != 1:
            raise ValueError('saved isolation configuration is incompatible; create a new run')
        import re
        for endpoint in config.get('denied_endpoints', []):
            if not isinstance(endpoint, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*:[0-9]{1,5}', endpoint) or not 1 <= int(endpoint.rsplit(':', 1)[1]) <= 65535:
                raise ValueError('denied endpoint must be a lowercase HOST:PORT')
        if args.dry_run:
            print(json.dumps({'dry_run': True, 'config': config, 'out': str(args.out.expanduser().resolve()),
                              'actions': ['snapshot committed source', 'create fresh target(s)', 'read numbered README',
                                          'ask tool-disabled agent for commands', 'validate and execute command segments',
                                          'record preservation precheck and durable resource intents',
                                          'run independent declared checks', 'verify exact cleanup and preservation postcheck',
                                          'write reports/diff and requested/effective capability evidence']}, indent=2))
            return 0
        return run(config, args.out.expanduser().resolve(), prior=prior)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        root.exit(2, "readtrial: " + (friendly_error(exc) if isinstance(exc, OSError) else str(exc)) + "\n")
