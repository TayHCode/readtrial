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
from urllib.parse import quote

from .installers import prepare
from . import isolation as guards
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
                 trusted_target: bool = False, ssh_linux_ack: bool = False, instance_prefix: str = "readtrial-",
                 dedicated_account: bool = False):
        self.spec, self.limits, self.log = spec, limits, log
        self.pool, self.domains, self.container_image = pool, domains or set(), container_image
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", instance_prefix):
            raise TargetError("instance prefix must start with a lowercase letter and contain only letters, digits or hyphens (max 31)")
        self.id = instance_prefix + uuid.uuid4().hex
        self.base = "/tmp/" + self.id
        self.created = False
        self.platform = "Linux"
        self.trusted_target = trusted_target
        self.dedicated_account = dedicated_account
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
        self.journal = None
        self.guard_paths: list[Path] = []
        self.guard_directory: Path | None = None
        self.before_manifest = None
        self.denied_endpoints: set[str] = set()
        self.capabilities = {
            'version': 1, 'backend': self.kind,
            'requested': {'filesystem': 'fresh environment and declared-set preservation',
                          'network': 'allowlist' if self.kind == 'lxc' else 'backend-specific',
                          'identity': 'unprivileged instance' if self.kind == 'lxc' else 'backend-specific',
                          'command_seconds': limits.command_seconds, 'total_seconds': limits.total_seconds,
                          'output_bytes': limits.output_bytes, 'memory_mb': limits.memory_mb,
                          'workspace_disk_mb': limits.disk_mb, 'root_disk_mb': limits.disk_mb + 2048,
                          'cpu_seconds': limits.cpu_seconds, 'pids': 128},
            'effective': {'filesystem': {'status': 'unverified'}, 'network': {'status': 'unverified'},
                          'identity': {'status': 'unverified'}, 'resources': {'status': 'unverified'},
                          'root_disk': {'status': 'unverified'}},
            'preflight': {'status': 'not-run'}, 'guards': {'status': 'not-run'},
            'cleanup': {'status': 'not-run', 'leftovers': []}}
        if self.kind == "ssh":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,200}", self.value):
                raise TargetError("SSH target must be a hostname or user@hostname")
            self.prefix = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                           "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
                           "-o", "ConnectTimeout=10", self.value]
            if dedicated_account:
                self.prefix[-1:-1] = ['-o', 'PreferredAuthentications=publickey',
                                     '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no']
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
        if dedicated_account and self.kind != 'ssh':
            raise TargetError('dedicated account mode requires macOS SSH')

    def outer(self, argv: list[str]) -> list[str]:
        # SSH concatenates arguments into a remote shell command. Quote every
        # token once; never interpolate agent/repository text into that shell.
        if self.kind == "ssh":
            return self.prefix + [shlex.join(argv)]
        return self.prefix + argv

    def configure_isolation(self, destination: Path, config: dict, bundle: Path) -> None:
        guards.secure_directory(destination)
        self.guard_directory = destination / '.isolation'
        guards.secure_directory(self.guard_directory)
        self.journal = guards.Journal(self.guard_directory / 'journals', self.id, self.kind)
        uv = shutil.which('uv')
        # The default includes both the resolved executable and its directory
        # entry; a linked executable is refused rather than followed blindly.
        self.guard_paths = ([Path(uv)] if uv else [Path.home() / '.local/bin/uv']) + [bundle]
        self.guard_paths += [Path(path) for path in config.get('protected_paths', []) + config.get('readonly_inputs', [])]
        self.denied_endpoints = set(config.get('denied_endpoints', []))

    def guard_before(self) -> None:
        if not self.guard_directory:
            raise TargetError('controller preservation evidence directory is required')
        try:
            self.before_manifest = guards.manifest(self.guard_paths)
            guards.atomic_json(self.guard_directory / 'before.json', self.before_manifest)
            self.capabilities['guards'] = {'status': 'before-recorded', 'scope': 'declared controller-local paths',
                                           'path_count': len(self.guard_paths)}
        except (OSError, ValueError) as exc:
            self.capabilities['guards'] = {'status': 'failed', 'reason': 'required precheck unreadable or unsafe'}
            raise TargetError('host preservation precheck failed: ' + str(exc)) from exc

    def root_quota_preflight(self) -> None:
        """Reject unsupported/unknown pool quota before any instance mutation."""
        try:
            pool = json.loads(self.management(['lxc', 'query', '/1.0/storage-pools/' + quote(self.pool, safe='')]).output)
            driver = pool['driver']
            details = {'driver': driver}
            supported = driver in {'zfs', 'btrfs', 'lvm', 'ceph'}
            if driver == 'dir':
                source = pool['config'].get('source', '')
                if not source.startswith('/'):
                    raise ValueError('storage source is not locally inspectable')
                mount = json.loads(self.management(['findmnt', '-J', '-T', source, '-o', 'FSTYPE,OPTIONS']).output)
                filesystem = mount['filesystems'][0]
                options = set(filesystem['options'].split(','))
                supported = filesystem['fstype'] in {'ext4', 'xfs'} and bool(options & {'prjquota', 'pquota'})
                details.update(filesystem=filesystem['fstype'], project_quota_mount=supported)
            self.capabilities['preflight'] = {'status': 'supported' if supported else 'failed', 'root_quota': details}
            self.capabilities['effective']['root_disk'] = {'status': 'driver-capable' if supported else 'unsupported',
                                                          'independent_enforcement_proof': False, **details}
            if not supported:
                raise TargetError('root quota unsupported or unenforced; no target created')
        except (ValueError, KeyError, IndexError, OSError, TargetError) as exc:
            self.capabilities['preflight']['status'] = 'failed'
            if self.capabilities['effective']['root_disk']['status'] != 'unsupported':
                self.capabilities['effective']['root_disk']['status'] = 'unknown'
            raise TargetError('root quota capability could not be established: ' + str(exc)) from exc

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
        if self.guard_directory:
            self.guard_before()
        if self.kind == "ssh":
            self.platform = self.probe_platform()
            if self.dedicated_account and self.platform != 'Darwin':
                raise TargetError('dedicated account mode requires macOS SSH')
            if self.platform in {"Darwin", "Windows"}:
                if not self.trusted_target:
                    raise TargetError(self.platform + " SSH requires --trusted-target; no OS confinement or address-space ceiling")
                if self.dedicated_account:
                    from .account import AccountSSH
                    self.trusted = AccountSSH(self)
                    self.trusted.before()
                else:
                    self.trusted = TrustedSSH(self)
                self.isolation = "TRUSTED-TARGET: user permissions; command inspection only, no OS confinement"
                self.network = "explicit URL/host inspection only; repository subprocess networking is unrestricted"
                self.trusted.prepare(bundle)
                self.record_ready()
                if self.dedicated_account:
                    self.isolation = 'DEDICATED-ACCOUNT: standard macOS account; owner home access refused; trusted repository only'
                    self.capabilities['effective']['identity'] = {'status': 'account-attested', 'uid': self.trusted.observed['uid'],
                                                                 'policy': 'all commands as dedicated SSH account; no sudo or su switching'}
                    self.capabilities['effective']['filesystem']['boundary'] = self.isolation
                    self.capabilities['effective']['filesystem']['limits'] = ['test account native home and world-readable files still reachable']
                return
            if self.platform != "Linux":
                raise TargetError("unsupported SSH platform")
            if not self.ssh_linux_ack:
                raise TargetError("Linux SSH requires --i-understand-ssh-linux-is-not-confined; fresh folder, time/output limits and deny list only; no network/filesystem confinement")
            self.trusted = TrustedSSH(self)
            self.isolation = "SSH-LINUX-UNCONFINED: user permissions; fresh folder, time/output limits and deny list; no network/filesystem confinement"
            self.network = "explicit URL/host inspection only; repository subprocess networking is unrestricted"
            self.trusted.prepare(bundle)
            self.record_ready()
            return
        if self.kind == "lxc":
            if not self.pool:
                pools = json.loads(self.management(["lxc", "storage", "list", "--format", "json"]).output)
                if len(pools) != 1:
                    raise TargetError("choose a quota-capable LXC storage pool with --lxc-pool")
                self.pool = pools[0]["name"]
            self.root_quota_preflight()
            if not self.journal:
                raise TargetError('durable ownership journal required for target creation')
            inventory = json.loads(self.management(['lxc', 'list', self.id, '--format', 'json']).output)
            if not isinstance(inventory, list) or any(item.get('name') == self.id for item in inventory):
                raise TargetError('target identity already exists or inventory is unreadable')
            self.journal.intent('lxc', self.id)
            # Never inherit the default profile: it may mount homes or sockets.
            # Mark owned name before mutation so partial init is cleaned up too.
            self.created = True
            self.management(["lxc", "init", self.value, self.id, "--no-profiles", "-s", self.pool,
                             "-d", f"root,size={self.limits.disk_mb + 2048}MiB",
                             "-c", "security.privileged=false", "-c", "security.nesting=true",
                             "-c", 'user.readtrial.id=' + self.id,
                             "-c", "security.idmap.isolated=true", "-c", "limits.cpu=1",
                             "-c", "limits.cpu.allowance=100ms/100ms", "-c", f"limits.memory={self.limits.memory_mb}MiB",
                             "-c", "limits.processes=128"], timeout=180)
            self.proxy = Proxy(self.domains)
            self.proxy.start()
            self.management(["lxc", "config", "device", "add", self.id, "registry-proxy", "proxy",
                             "bind=container", "listen=tcp:127.0.0.1:18080",
                             f"connect=tcp:127.0.0.1:{self.proxy.port}"])
            self.attest_lxc_config()
            self.management(["lxc", "start", self.id])
            self.management(self.outer(["sh", "-c",
                ("export http_proxy=http://127.0.0.1:18080 https_proxy=http://127.0.0.1:18080; "
                "export DEBIAN_FRONTEND=noninteractive; apt-get update && "
                "apt-get install -y --no-install-recommends python3 git curl bubblewrap ca-certificates util-linux")]),
                timeout=180)
            self.attest_lxc_kernel()
            self.network = "registry/repository allowlist through loopback proxy; no NIC"
        elif self.kind == "local-container":
            if not self.journal:
                raise TargetError('durable ownership journal required for target creation')
            self.journal.intent('oci', self.id)
            self.created = True
            self.management([self.engine, "run", "-d", "--name", self.id, "--network", "none",
                             "--read-only", "--cap-drop", "ALL", "--pids-limit", "128",
                             '--label', 'readtrial.id=' + self.id,
                             "--cpus", "1", "--memory", f"{self.limits.memory_mb}m",
                             "--tmpfs", f"/tmp:rw,size={self.limits.disk_mb}m",
                             self.container_image, "sleep", str(self.limits.total_seconds + 60)])
        # This directory holds only the control scripts and immutable source
        # bundle. The stranger never sees the remote temp directory or home.
        if self.journal:
            self.journal.intent('target-path', self.base)
        self.management(self.outer(["mkdir", "-m", "700", self.base]))
        self.created = True
        if self.kind == 'lxc':
            self.attest_root_quota()
        for name in ("worker.py", "process.py", "installers.py", "shell.py", 'isolation.py'):
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
        if self.kind == 'lxc':
            self.attest_namespace(argv, bootstrap)
        self.session = subprocess.Popen(self.outer(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        assert self.session.stdout
        os.set_blocking(self.session.stdout.fileno(), False)
        result = self.command("git -c core.hooksPath=/dev/null clone /source.bundle .", setup=True)
        if result.exit_code:
            raise TargetError(result.output or "sandbox session could not start")
        self.isolation = "Linux namespace/tmpfs and per-process limits"
        self.record_ready()

    def attest_lxc_config(self) -> None:
        """Read expanded daemon state; reject extra inherited access before bootstrap."""
        try:
            observed = json.loads(self.management(['lxc', 'query', '/1.0/instances/' + self.id]).output)
            config, devices = observed['expanded_config'], observed['expanded_devices']
            required = {'user.readtrial.id': self.id, 'security.privileged': 'false',
                        'security.nesting': 'true', 'security.idmap.isolated': 'true',
                        'limits.cpu': '1', 'limits.cpu.allowance': '100ms/100ms',
                        'limits.memory': f'{self.limits.memory_mb}MiB', 'limits.processes': '128'}
            expected_devices = {
                'root': {'type': 'disk', 'path': '/', 'pool': self.pool,
                         'size': f'{self.limits.disk_mb + 2048}MiB'},
                'registry-proxy': {'type': 'proxy', 'bind': 'container',
                                   'listen': 'tcp:127.0.0.1:18080',
                                   'connect': f'tcp:127.0.0.1:{self.proxy.port}'}}
            if observed['name'] != self.id or observed['profiles'] != [] or devices != expected_devices:
                raise ValueError('unexpected identity, inherited profile, mount, device or network access')
            if any(config.get(key) != value for key, value in required.items()):
                raise ValueError('required identity or resource setting missing or changed')
            if any(key.startswith(('raw.', 'linux.', 'security.')) and key not in required for key in config):
                raise ValueError('unexpected low-level or security configuration')
            self.capabilities['effective']['identity'] = {'status': 'config-attested', 'unprivileged': True, 'isolated_idmap': True}
            self.capabilities['effective']['network'] = {'status': 'config-attested', 'no_nic': True,
                'proxy': 'container loopback to controller loopback; exact public-domain allowlist'}
            self.capabilities['effective']['root_disk'].update(configured_mb=self.limits.disk_mb + 2048)
            if self.guard_directory:
                guards.atomic_json(self.guard_directory / 'expanded-boundary.json', observed)
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            raise TargetError('effective LXC configuration unproven: ' + str(exc)) from exc

    def attest_lxc_kernel(self) -> None:
        """Read user mapping, network and aggregate cgroup ceilings inside the guest."""
        script = ("import json,os;from pathlib import Path;"
                  "p=Path('/sys/fs/cgroup');"
                  "print(json.dumps({'uid_map':[list(map(int,x.split())) for x in Path('/proc/self/uid_map').read_text().splitlines()],"
                  "'memory':(p/'memory.max').read_text().strip(),'pids':(p/'pids.max').read_text().strip(),"
                  "'cpu':(p/'cpu.max').read_text().strip(),'interfaces':sorted(os.listdir('/sys/class/net')),"
                  "'routes':[x for x in Path('/proc/net/route').read_text().splitlines()[1:] if x.split()[0]!='lo']+"
                  "[x for x in Path('/proc/net/ipv6_route').read_text().splitlines() if x.split()[-1]!='lo']}))")
        try:
            observed = json.loads(self.management(self.outer(['python3', '-c', script])).output)
            quota, period = map(int, observed['cpu'].split())
            mappings = observed['uid_map']
            if (not mappings or any(len(row) != 3 or row[1] <= 0 or row[2] <= 0 for row in mappings)
                    or not any(row[0] == 0 for row in mappings)
                    or not 0 < int(observed['memory']) <= self.limits.memory_mb * 1024 * 1024
                    or not 0 < int(observed['pids']) <= 128 or not 0 < quota <= period
                    or observed['interfaces'] != ['lo'] or observed['routes'] != []):
                raise ValueError('guest identity, network or cgroup ceiling mismatch')
            self.capabilities['effective']['identity']['status'] = 'kernel-attested'
            self.capabilities['effective']['resources'] = {'status': 'kernel-attested',
                'memory_bytes': int(observed['memory']), 'pids': int(observed['pids']),
                'cpu_quota': quota, 'cpu_period': period}
            self.capabilities['effective']['network'].update(status='kernel-attested', no_direct_route=True)
            if self.guard_directory:
                guards.atomic_json(self.guard_directory / 'kernel-boundary.json', observed)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise TargetError('effective Linux kernel boundary unproven: ' + str(exc)) from exc

    def attest_root_quota(self) -> None:
        """Bounded owned-file EDQUOT witness; never fill an unbounded shared pool."""
        ceiling = (self.limits.disk_mb + 2048) * 1024 * 1024
        budget = ceiling + 1024 * 1024
        endpoint = '/1.0/storage-pools/' + quote(self.pool, safe='') + '/resources'
        try:
            before = json.loads(self.management(['lxc', 'query', endpoint]).output)['space']
            free = int(before['total']) - int(before['used'])
            # Preserve three quarters of currently available space even if the
            # root quota is ignored. Unsupported accounting fails before write.
            if budget > 4 * 1024**3 + 1024 * 1024 or budget * 4 > free:
                raise ValueError('insufficient independent pool headroom for a bounded root quota proof')
            destination = self.base + '/root-quota-probe'
            self.journal.intent('target-path', destination)
            script = ("import errno,json,os,signal,sys;from pathlib import Path;"
                      "signal.alarm(45);p=Path(sys.argv[1]);budget=int(sys.argv[2]);written=0;error=0;"
                      "f=p.open('xb',buffering=0)\n"
                      "try:\n"
                      " while written<budget:\n"
                      "  chunk=b'x'*min(1024*1024,budget-written)\n"
                      "  written+=f.write(chunk)\n"
                      "except OSError as exc:\n"
                      " error=exc.errno\n"
                      "finally:\n"
                      " f.close();p.unlink()\n"
                      "print(json.dumps({'errno':error,'bytes_written':written,'probe_file_absent':not p.exists()}))")
            proof = json.loads(self.management(self.outer(['python3', '-c', script, destination, str(budget)]), timeout=60).output)
            after = json.loads(self.management(['lxc', 'query', endpoint]).output)['space']
            if (proof['errno'] != 122 or proof['probe_file_absent'] is not True
                or not 0 <= proof['bytes_written'] < budget
                or int(after['total']) - int(after['used']) < budget):
                raise ValueError('owned-root EDQUOT or independent pool headroom not proved')
            self.capabilities['effective']['root_disk'].update(status='enforcement-attested',
                independent_enforcement_proof=True, probe_bytes_written=proof['bytes_written'],
                proof='bounded EDQUOT; probe removed; shared pool not exhausted')
            if self.guard_directory:
                guards.atomic_json(self.guard_directory / 'root-quota-boundary.json',
                    {'before': before, 'after': after, 'budget_bytes': budget, **proof})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.capabilities['effective']['root_disk']['independent_enforcement_proof'] = False
            raise TargetError('effective root quota unproven: ' + str(exc)) from exc

    def attest_namespace(self, argv: list[str], bootstrap: str) -> None:
        """Exercise the same fixed bubblewrap/prlimit path before cloning source."""
        script = ("import os,json,resource;"
                  "print(json.dumps({'workspace_bytes':os.statvfs('/workspace').f_blocks*os.statvfs('/workspace').f_frsize,"
                  "'root_readonly':bool(os.statvfs('/').f_flag & os.ST_RDONLY),"
                  "'usr_readonly':bool(os.statvfs('/usr').f_flag & os.ST_RDONLY),"
                  "'as':resource.getrlimit(resource.RLIMIT_AS)[0],"
                  "'fsize':resource.getrlimit(resource.RLIMIT_FSIZE)[0],"
                  "'cpu':resource.getrlimit(resource.RLIMIT_CPU)[0]}))")
        probe_bootstrap = bootstrap.replace('os.execvp(argv[0],argv)',
            'argv[-3:]=' + repr(['python3', '-c', script]) + ';os.execvp(argv[0],argv)')
        probe = [probe_bootstrap if item == bootstrap else item for item in argv]
        try:
            observed = json.loads(self.management(self.outer(probe)).output)
            if (not 0 < observed['workspace_bytes'] <= self.limits.disk_mb * 1024 * 1024
                or observed['root_readonly'] is not True or observed['usr_readonly'] is not True
                or observed['as'] != self.limits.memory_mb * 1024 * 1024
                or observed['fsize'] != self.limits.disk_mb * 1024 * 1024
                or observed['cpu'] != self.limits.cpu_seconds):
                raise ValueError('workspace, read-only system or process limit mismatch')
            self.capabilities['effective']['filesystem'] = {'status': 'namespace-attested',
                'workspace_bytes': observed['workspace_bytes'], 'root_readonly': True, 'usr_readonly': True}
            self.capabilities['effective']['resources']['process_limits'] = {
                limit_name: observed[limit_name] for limit_name in ('as', 'fsize', 'cpu')}
            if self.guard_directory:
                guards.atomic_json(self.guard_directory / 'namespace-boundary.json', observed)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise TargetError('effective namespace boundary unproven: ' + str(exc)) from exc

    def record_ready(self) -> None:
        effective = self.capabilities['effective']
        if self.kind == 'lxc':
            if not (effective['identity']['status'] == 'kernel-attested' and effective['resources']['status'] == 'kernel-attested' and effective['filesystem']['status'] == 'namespace-attested' and effective['root_disk'].get('independent_enforcement_proof') is True):
                raise TargetError('effective boundary attestation missing; no repository commands permitted')
            effective['filesystem']['environment'] = 'per-trial HOME/XDG/temp/cache/tools'
            if self.journal:
                self.journal.phase('running')
            return
        effective['filesystem'] = {'status': 'active', 'boundary': self.isolation,
                                   'environment': 'per-trial HOME/XDG/temp/cache/tools'}
        effective['network'] = {'status': 'active', 'policy': self.network}
        effective['identity'] = {'status': 'unverified' if self.kind != 'ssh' else 'unconfined',
                                 'policy': 'backend identity; full attestation pending'}
        effective['resources'] = {'status': 'requested-not-attested' if self.kind != 'ssh' else 'limited',
                                  'verified': ['transport time/output bounds'],
                                  'unsupported': ['aggregate memory/disk/CPU/PID controls'] if self.kind == 'ssh' else []}
        if self.kind == 'ssh':
            effective['root_disk'] = {'status': 'unsupported'}
        if self.journal:
            self.journal.phase('running')

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
        if self.journal:
            self.journal.intent('target-path', destination)
        script = "import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        # Before Python bootstrap (LXC) is complete, no writes occur.
        self.management(self.outer(["python3", "-c", script, destination]), data=data)

    def command(self, command: str, *, setup: bool = False, timeout: float | None = None) -> Result:
        if self.kind == 'lxc' and not (
                self.capabilities['effective']['identity']['status'] == 'kernel-attested'
                and self.capabilities['effective']['resources']['status'] == 'kernel-attested'
                and self.capabilities['effective']['filesystem']['status'] == 'namespace-attested'
                and self.capabilities['effective']['root_disk'].get('independent_enforcement_proof') is True):
            raise TargetError('effective boundary unproven; repository command refused')
        if self.trusted:
            return self.trusted.command(command, setup=setup, timeout=timeout)
        if not self.session or not self.session.stdin or not self.session.stdout:
            raise TargetError("sandbox session is not running")
        runnable, _, denial = prepare(command, "/workspace", repository=self.repository)
        budget = timeout if timeout is not None else self.limits.command_seconds
        request = {"command": command, "timeout": budget,
                   "output_limit": self.limits.output_bytes, "repository": self.repository, "disposable": self.kind != "ssh", "setup": setup,
                   'denied_endpoints': sorted(self.denied_endpoints)}
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

    def _close_resources(self) -> None:
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
                probe = execute(['lxc', 'list', self.id, '--format', 'json'], timeout=10, cap=self.limits.output_bytes)
                if probe.exit_code or probe.truncated or probe.timed_out:
                    raise TargetError('owned instance inventory unreadable; preserved for explicit recovery')
                inventory = json.loads(probe.output)
                if not isinstance(inventory, list):
                    raise TargetError('malformed owned instance inventory')
                exact = [item for item in inventory if item.get('name') == self.id]
                if exact and (len(exact) != 1 or exact[0].get('config', {}).get('user.readtrial.id') != self.id):
                    raise TargetError('instance ownership tag mismatch; instance preserved')
                if not exact:
                    self.created = False
                    self.log('cleanup-confirmation', 'confirm exact instance absent', Result(0, 'instance absent', 0))
                    return
                argv = ["lxc", "delete", "--force", self.id]
            elif self.kind == "local-container":
                probe = execute([self.engine, 'inspect', self.id], timeout=10, cap=self.limits.output_bytes)
                if probe.exit_code or probe.truncated or probe.timed_out:
                    raise TargetError('owned container inventory unreadable; preserved for explicit recovery')
                inventory = json.loads(probe.output)
                if len(inventory) != 1 or inventory[0].get('Config', {}).get('Labels', {}).get('readtrial.id') != self.id:
                    raise TargetError('container ownership tag mismatch; container preserved')
                argv = [self.engine, "rm", "-f", self.id]
            else:
                argv = self.outer(["rm", "-rf", "--", self.base])
            result = execute(argv, timeout=30, cap=self.limits.output_bytes)
            self.log("cleanup", shlex.join(argv), result)
            # A failed create may leave no instance; confirm absence rather than
            # treating a "not found" cleanup error as proof of a leak.
            if result.exit_code and not (result.truncated or result.timed_out) and self.kind == "lxc":
                check = execute(["lxc", "list", self.id, "--format", "json"], timeout=10)
                if check.exit_code == 0 and not (check.truncated or check.timed_out) and json.loads(check.output) == []:
                    result = Result(0, "instance absent", 0)
                    self.log("cleanup-confirmation", "confirm instance absent", result)
            if result.exit_code or result.truncated or result.timed_out:
                errors.append("could not remove owned target: " + result.output)
            elif self.kind == "ssh":
                check = execute(self.outer(["test", "!", "-e", self.base]), timeout=10)
                self.log("cleanup-confirmation", "confirm temporary directory absent", check)
                if check.exit_code or check.truncated or check.timed_out:
                    errors.append("temporary directory cleanup not confirmed")
            elif self.kind == "lxc":
                check = execute(["lxc", "list", self.id, "--format", "json"], timeout=10)
                self.log("cleanup-confirmation", "confirm owned instance absent", check)
                if check.exit_code or check.truncated or check.timed_out or any(item.get('name') == self.id for item in json.loads(check.output)):
                    errors.append("container cleanup not confirmed")
            elif self.kind == 'local-container':
                check = execute([self.engine, 'ps', '-a', '--filter', 'name=' + self.id, '--format', '{{.Names}}'], timeout=10, cap=self.limits.output_bytes)
                if check.exit_code or check.truncated or check.timed_out or self.id in check.output.splitlines():
                    errors.append('container cleanup not confirmed')
        if errors:
            raise TargetError("; ".join(errors))

    def close(self) -> None:
        errors = []
        try:
            if self.journal:
                self.journal.phase('closing')
        except (OSError, ValueError) as exc:
            errors.append('ownership journal: ' + str(exc))
        try:
            self._close_resources()
            self.capabilities['cleanup'] = {'status': 'confirmed', 'leftovers': []}
        except (OSError, ValueError, TargetError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
            self.capabilities['cleanup'] = {'status': 'failed', 'leftovers': [self.id],
                                             'reason': 'exact target cleanup not confirmed'}
        finally:
            if self.proxy:
                try:
                    self.proxy.close()
                except (OSError, RuntimeError) as exc:
                    errors.append('proxy cleanup: ' + str(exc))
                    self.capabilities['cleanup']['status'] = 'failed'
        if self.guard_directory:
            try:
                if self.before_manifest is None:
                    raise ValueError('required preservation precheck missing')
                after = guards.manifest(self.guard_paths)
                guards.atomic_json(self.guard_directory / 'after.json', after)
                if after != self.before_manifest:
                    raise ValueError('declared protected set changed')
                self.capabilities['guards']['status'] = 'unchanged'
            except (OSError, ValueError) as exc:
                self.capabilities['guards']['status'] = 'failed'
                errors.append('host preservation postcheck: ' + str(exc))
        if self.journal:
            try:
                self.journal.phase('incomplete' if errors else 'complete')
            except (OSError, ValueError) as exc:
                errors.append('ownership journal: ' + str(exc))
        if errors:
            raise TargetError('; '.join(errors))
