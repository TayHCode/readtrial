"""Portable read-only diagnostics and explicit LXD storage setup."""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .process import execute


def query(argv: list[str]):
    result = execute(argv, timeout=20, cap=65536)
    if result.exit_code or result.truncated or result.timed_out:
        raise ValueError('Prerequisite check failed; run readtrial doctor before trying again.')
    return json.loads(result.output)


def in_lxd_group() -> bool:
    import grp
    try:
        return grp.getgrnam('lxd').gr_gid in os.getgroups()
    except KeyError:
        return False


NESTED_STORAGE_FIX = (
    'This nested container has no loop-control device; setup-linux cannot create '
    'its quota-capable btrfs loop pool here. Use a real Linux host or VM, or ask '
    'its administrator for an already quota-capable LXD storage pool and pass '
    '--lxc-pool POOL to trials. A dir pool cannot enforce the required root size '
    'limit. Do not expose host devices or sockets to bypass trial isolation.'
)


def nested_loop_unavailable() -> bool:
    """Detect the known nested provisioning failure without touching devices."""
    try:
        container = Path('/run/systemd/container').read_text().strip()
    except OSError:
        return False
    return bool(container and container != 'none') and not Path('/dev/loop-control').exists()


def lxd_cli_installed() -> bool:
    executable = shutil.which('lxc')
    if not executable:
        return False
    try:
        with Path(executable).open('rb') as stream:
            header = stream.read(8192)
    except OSError:
        # Let the ordinary read-only server query diagnose an inaccessible CLI.
        return True
    # Stock Ubuntu's launcher can trigger a snap installation through a local
    # socket. Never execute that bootstrap path from the read-only doctor.
    if header.startswith(b'#!') and b'SNAP_BIN=' in header and b'/run/lxd-installer.socket' in header:
        return os.access('/snap/bin/lxc', os.X_OK)
    return True


def doctor(system: str | None = None, *, test_user: str = '', target: str = '') -> dict:
    system = system or platform.system()
    checks = []
    instructions = ''
    def add(label, ready, fix):
        checks.append({'check': label, 'ready': bool(ready), 'fix': '' if ready else fix})
    if target:
        from .account import AccountSSH
        from .targets import Target, Limits, TargetError
        machine = Target(target, Limits(), lambda *args: None, trusted_target=True, dedicated_account=True)
        try:
            system = machine.probe_platform()
            if system != 'Darwin':
                raise ValueError('dedicated account doctor requires macOS SSH')
            account = AccountSSH(machine).request('probe', {})
            test_user = test_user or (machine.value.split('@', 1)[0] if '@' in machine.value else account['user'])
        except (ValueError, TargetError) as exc:
            add('Dedicated macOS SSH account reachable', False, str(exc))
            from .account import provisioning_steps
            return {'os': system, 'ready': False, 'checks': checks, 'instructions': provisioning_steps(test_user)}
    if system == 'Linux':
        installed = lxd_cli_installed()
        add('LXD installed', installed, 'sudo snap install lxd')
        if installed:
            try:
                server = query(['lxc', 'query', '/1.0'])
                ready = isinstance(server, dict) and server.get('api_version') == '1.0'
            except (OSError, ValueError):
                ready = False
            add('LXD ready for this user', ready, 'sudo lxd init --minimal\nsudo usermod -aG lxd "$(id -un)"\n# Log out and back in, then run readtrial doctor.')
            add('Current login has LXD access', in_lxd_group(), 'sudo usermod -aG lxd "$(id -un)"\n# Log out and back in, then run readtrial doctor.')
            quota = False
            if ready:
                try:
                    pools = query(['lxc', 'storage', 'list', '--format', 'json'])
                    quota = any(p.get('status') == 'Created' and p.get('driver') in {'btrfs', 'zfs', 'lvm', 'ceph'} for p in pools)
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            add('Storage can enforce a size limit', quota,
                NESTED_STORAGE_FIX if not quota and nested_loop_unavailable() else 'readtrial setup-linux --yes')
    elif system == 'Darwin':
        from .account import local_probe, validate_account, provisioning_steps
        account, reason = account if target else {}, 'Designate a standard test account with --test-user.'
        if re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', test_user):
            try:
                if not target:
                    account = local_probe(test_user)
            except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
                reason = str(exc)
        add('Designated standard account (not root/admin)', account.get('user') == test_user and
            account.get('uid', 0) >= 501 and not set(account.get('groups', [])) & {'admin', 'wheel'}, reason)
        add('Distinct primary group (no other primary, explicit or nested members)',
            account.get('gid', 0) >= 500 and account.get('private_group'), 'A separate primary group is required; use the owner setup reference below.')
        add('Test home belongs to the designated account', account.get('home_owned') and
            account.get('home') == '/Users/' + test_user, 'Owner must provide a separate owned home for this test account.')
        add('Owner home and SSH key opens refused', account.get('owner_homes') and account.get('owner_reads_refused'),
            'Owner must ensure this account cannot open their home or .ssh; no owner file contents are read.')
        add('Sudo policy denies this account', account.get('sudo') == 'denied',
            'sudo denial unverified (' + account.get('sudo', 'unknown') + '); password-required is ambiguous. Administrator must run attest-sudo.')
        add('SSH session connected', account.get('ssh'),
            'Run this doctor over SSH as the test account, or from the controller with --target ssh:USER@HOST.')
        # Keep doctor and execution validation in agreement, including future
        # validation changes not covered by a visible prerequisite row.
        if account and all(row['ready'] for row in checks):
            try:
                validate_account(account, test_user)
            except ValueError as exc:
                add('Account execution validation', False, str(exc))
        instructions = provisioning_steps(test_user)
    elif system == 'Windows':
        add('Windows Sandbox is not supported', False,
            'No Windows Sandbox target backend exists. Use a disposable Linux VM '
            'as the controller and follow the Linux LXD setup; its trial guest '
            'must pass the same quota and namespace checks. Native Windows SSH '
            'remains trusted mode only, with --trusted-target; native Windows '
            'controller trials are unverified.')
    else:
        add('Supported operating system', False, '# Use Linux, macOS or Windows.')
    return {'os': system, 'ready': bool(checks) and all(c['ready'] for c in checks), 'checks': checks,
            'instructions': instructions}


def setup_linux(*, pool: str = 'readtrial', size_gib: int = 30, location: str = '', yes: bool = False) -> int:
    if platform.system() != 'Linux':
        raise ValueError('setup-linux requires Linux.')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', pool):
        raise ValueError('Pool name must start with a lowercase letter and contain letters, digits or hyphens.')
    if not isinstance(size_gib, int) or not 1 <= size_gib <= 4096:
        raise ValueError('Size must be between 1 and 4096 GiB.')
    if location:
        from .isolation import no_links
        path = no_links(Path(location))
        if not path.parent.is_dir():
            raise ValueError('Location must be a new absolute loop-file path with an existing parent directory.')
    argv = ['lxc', 'storage', 'create', pool, 'btrfs', f'size={size_gib}GiB', 'user.readtrial.setup=1']
    if location:
        argv.append('source=' + location)
    print(f'Create only the storage pool {pool}, capped at {size_gib} GiB, using an LXD-managed loop file.')
    print('The pool persists after reboot. Existing pools, profiles and instances are preserved.')
    print('Create command: ' + shlex.join(argv))
    print('Undo after all trials finish: ' + shlex.join(['lxc', 'storage', 'delete', pool]))
    if not yes:
        print('No changes made. Repeat with --yes to create this pool.')
        return 2
    pools = query(['lxc', 'storage', 'list', '--format', 'json'])
    instances = query(['lxc', 'list', '--format', 'json'])
    profiles = query(['lxc', 'profile', 'list', '--format', 'json'])
    if not all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows) for rows in (pools, instances, profiles)):
        raise ValueError('Existing resource inventory is unreadable; no changes made.')
    if any(row.get('name') == pool for row in instances + profiles):
        raise ValueError('An existing instance or profile has this name; choose another pool name.')
    existing = [row for row in pools if row.get('name') == pool]
    if existing:
        config = existing[0].get('config', {})
        if len(existing) != 1 or existing[0].get('driver') != 'btrfs' or config.get('user.readtrial.setup') != '1' or config.get('size') != f'{size_gib}GiB' or (location and config.get('source') != location):
            raise ValueError('Refusing to touch an existing pool; choose another name or use --lxc-pool for trials.')
        print('Matching readtrial-created pool already exists; no changes made.')
        return 0
    if location and os.path.lexists(location):
        raise ValueError('Refusing to overwrite an existing loop-file location.')
    if nested_loop_unavailable():
        raise ValueError(NESTED_STORAGE_FIX + ' No changes made.')
    result = execute(argv, timeout=180, cap=65536)
    if result.exit_code or result.truncated or result.timed_out:
        raise ValueError('Pool creation did not complete: quota-capable btrfs loop storage '
                         'could not be confirmed. Use a real Linux host or VM with loop '
                         'device and btrfs support, or an administrator-provided quota-capable '
                         'pool on a suitable filesystem. Inspect this exact pool before '
                         'retrying; no automatic removal attempted.')
    created = query(['lxc', 'query', '/1.0/storage-pools/' + pool])
    config = created.get('config', {})
    if created.get('driver') != 'btrfs' or config.get('size') != f'{size_gib}GiB' or config.get('user.readtrial.setup') != '1':
        raise ValueError('Pool size/ownership could not be confirmed; do not run trials.')
    print('Size-capped pool ready.')
    return 0
