"""Dedicated macOS account probes and bounded cleanup; not a VM boundary.

The functions above REMOTE_END also run on stock macOS Python 3.9. They never
import repository code, inspect process arguments, or read another user's data.
"""
from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import pwd
import plistlib
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

_MAC_LIB = None


def file_metadata(path):
    """Stock macOS Python lacks os.listxattr; use the native no-follow APIs."""
    if sys.platform != 'darwin':
        return {name: hashlib.sha256(os.getxattr(path, name, follow_symlinks=False)).hexdigest()
                for name in sorted(os.listxattr(path, follow_symlinks=False))}
    global _MAC_LIB
    if _MAC_LIB is None:
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        lib.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        lib.listxattr.restype = ctypes.c_ssize_t
        lib.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        lib.getxattr.restype = ctypes.c_ssize_t
        lib.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.acl_get_link_np.restype = ctypes.c_void_p
        lib.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        lib.acl_to_text.restype = ctypes.c_void_p
        lib.acl_free.argtypes = [ctypes.c_void_p]
        _MAC_LIB = lib
    lib, encoded = _MAC_LIB, os.fsencode(path)
    def bounded(size):
        if size < 0:
            raise OSError(ctypes.get_errno(), 'native account metadata unreadable')
        if size > 256 * 1024:
            raise ValueError('account metadata byte limit')
        return size
    size = bounded(lib.listxattr(encoded, None, 0, 1))  # XATTR_NOFOLLOW
    buffer = ctypes.create_string_buffer(max(size, 1))
    size = bounded(lib.listxattr(encoded, buffer, size, 1))
    result = {}
    for name in sorted(filter(None, buffer.raw[:size].split(b'\0'))):
        length = bounded(lib.getxattr(encoded, name, None, 0, 0, 1))
        value = ctypes.create_string_buffer(max(length, 1))
        length = bounded(lib.getxattr(encoded, name, value, length, 0, 1))
        result[os.fsdecode(name)] = hashlib.sha256(value.raw[:length]).hexdigest()
    acl = lib.acl_get_link_np(encoded, 0x100)  # ACL_TYPE_EXTENDED, no follow
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            # Darwin reports ENOENT for an existing file with no extended ACL.
            # A removed directory entry remains a guard failure.
            path.lstat()
            result['ACL'] = None
            return result
        raise OSError(ctypes.get_errno(), 'native account ACL unreadable')
    text = None
    try:
        length = ctypes.c_ssize_t()
        text = lib.acl_to_text(acl, ctypes.byref(length))
        if not text:
            raise OSError(ctypes.get_errno(), 'native account ACL unreadable')
        result['ACL'] = hashlib.sha256(ctypes.string_at(text, bounded(length.value))).hexdigest()
    finally:
        if text:
            lib.acl_free(text)
        lib.acl_free(acl)
    return result


def output(argv):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=10, env={**os.environ, 'LC_ALL': 'C'})
    return result.returncode, result.stdout + result.stderr


def canonical(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('noncanonical account path')
    for entry in reversed((path, *path.parents)):
        if entry.is_symlink():
            raise ValueError('linked account path')
    return path


def home_manifest(home, exclude=''):
    """Hash only the test account's home; preserve links without following them."""
    root = canonical(home)
    result = {}
    total = 0
    stack = [(root, '.')]
    while stack:
        path, relative = stack.pop()
        if str(path) == exclude:
            continue
        if len(result) >= 20000:
            raise ValueError('account home manifest entry limit')
        info = path.lstat()
        row = {'mode': info.st_mode, 'uid': info.st_uid, 'gid': info.st_gid}
        row['xattrs'] = file_metadata(path)
        if stat.S_ISLNK(info.st_mode):
            row['link'] = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if total > 512 * 1024 * 1024:
                raise ValueError('account home manifest byte limit')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError('account home file changed while opening')
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            row['sha256'] = digest.hexdigest()
        elif stat.S_ISDIR(info.st_mode):
            for entry in sorted(path.iterdir()):
                stack.append((entry, entry.name if relative == '.' else relative + '/' + entry.name))
        else:
            raise ValueError('unsupported account home entry')
        result[relative] = row
    return result


def refused(path):
    """Attempt an open, never consume bytes or enumerate an owner's directory."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        return exc.errno in {errno.EACCES, errno.EPERM}
    else:
        os.close(fd)
        return False


def policy_denied(text, user):
    # On macOS an administrator's `sudo -l -U USER` can exit zero even
    # when it explicitly reports denial. Require the entire user-specific
    # denial response; never infer permission from an interactive prompt/status.
    return bool(re.fullmatch(r'User ' + re.escape(user) + r' is not allowed to run sudo on [^\r\n]+\.',
                             text.strip(), flags=re.IGNORECASE))


def account_probe(user):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', user):
        raise ValueError('designate a standard test user')
    account = pwd.getpwnam(user)
    if os.getuid() != account.pw_uid:
        raise ValueError('probe must run as the designated test account over SSH')
    code, groups = output(['id', '-Gn'])
    if code:
        raise ValueError('account groups unreadable')
    code, users = output(['dscl', '.', '-list', '/Users', 'PrimaryGroupID'])
    if code:
        raise ValueError('primary group inventory unreadable')
    same_group = [line.rsplit(None, 1)[0] for line in users.splitlines()
                  if len(line.rsplit(None, 1)) == 2 and line.rsplit(None, 1)[1] == str(account.pw_gid)]
    code, group = output(['dscl', '.', '-list', '/Groups', 'PrimaryGroupID'])
    group_names = [line.rsplit(None, 1)[0] for line in group.splitlines()
                   if len(line.rsplit(None, 1)) == 2 and line.rsplit(None, 1)[1] == str(account.pw_gid)]
    private = not code and len(group_names) == 1 and same_group == [user]
    if private:
        code, members = output(['dscl', '-plist', '.', '-read', '/Groups/' + group_names[0]])
        # Empty membership is normal for a primary group. Reject other members
        # and nested groups; ambiguous directory-service records fail closed.
        fields = {key.split(':')[-1]: value for key, value in plistlib.loads(members.encode()).items()}
        code_guid, guid = output(['dscl', '.', '-read', '/Users/' + user, 'GeneratedUID'])
        private = not code and not code_guid and not fields.get('NestedGroups') and \
            set(fields.get('GroupMembership', [])) <= {user} and \
            set(fields.get('GroupMembers', [])) <= set(guid.split()[1:])
    home = canonical(account.pw_dir)
    owners = []
    code, uids = output(['dscl', '.', '-list', '/Users', 'UniqueID'])
    if code:
        raise ValueError('owner account inventory unreadable')
    for line in uids.splitlines():
        bits = line.rsplit(None, 1)
        if len(bits) != 2 or not bits[1].isdigit() or int(bits[1]) < 501 or bits[0] == user:
            continue
        candidate = pwd.getpwnam(bits[0]).pw_dir
        if candidate.startswith('/Users/') and candidate != str(home):
            owners.append(candidate)
    reads = [{'home_refused': refused(owner), 'ssh_refused': refused(owner + '/.ssh'),
              'key_refused': refused(owner + '/.ssh/id_ed25519')} for owner in owners]
    code, sudo_text = output(['sudo', '-n', '-l'])
    sudo_state = 'allowed' if code == 0 else 'unknown'
    if policy_denied(sudo_text, user):
        sudo_state = 'denied'
    elif code and ' '.join(('password', 'is', 'required')) in sudo_text.lower():
        sudo_state = 'password-required'
    observed = dict(user=user, uid=account.pw_uid, gid=account.pw_gid, groups=groups.split(),
                    home=str(home), home_owned=home.stat().st_uid == account.pw_uid,
                    private_group=private, owner_homes=owners,
                    owner_reads_refused=bool(reads) and all(all(row.values()) for row in reads),
                    refusal_checks=reads, sudo=sudo_state, ssh=bool(os.environ.get('SSH_CONNECTION')))
    if sudo_state != 'allowed':
        attestation = home / '.readtrial-sudo-denied.json'
        try:
            canonical(attestation)
            info = attestation.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('sudo attestation must be root-owned and non-writable')
            fd = os.open(attestation, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError('sudo attestation changed')
                attested = json.loads(stream.read(8192))
            if all(attested.get(key) == observed[key] for key in ('user', 'uid', 'gid', 'groups')) and \
                    attested.get('sudo') == 'denied' and 0 <= time.time() - attested['audited_at'] < 86400:
                observed['sudo'] = 'denied'
                observed['sudo_evidence'] = 'root-owned policy audit, less than 24 hours old'
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return observed


def validate_account(account, expected):
    if account.get('user') != expected or account.get('uid', 0) < 501:
        raise ValueError('wrong or root SSH account')
    if set(account.get('groups', [])) & {'admin', 'wheel'} or account.get('gid', 0) < 500 or not account.get('private_group'):
        raise ValueError('admin or shared primary group account')
    if not account.get('home_owned') or account.get('home') != '/Users/' + expected:
        raise ValueError('test account home ownership unverified')
    if not account.get('owner_homes') or not account.get('owner_reads_refused'):
        raise ValueError('owner home and SSH key read refusal unverified')
    if account.get('sudo') != 'denied':
        raise ValueError('sudo denial unverified; password-required is ambiguous; administrator must run attest-sudo')
    if not account.get('ssh'):
        raise ValueError('key-only SSH connection unverified; run doctor through SSH or use --target')


def process_identity(pid):
    code, text = output(['ps', '-p', str(pid), '-o', 'pid=,uid=,pgid=,lstart='])
    fields = text.strip().split(None, 3)
    if code or len(fields) != 4:
        return None
    return dict(pid=int(fields[0]), uid=int(fields[1]), pgid=int(fields[2]), start=fields[3])


def verified_process(saved, current, uid):
    return bool(current and saved.get('uid') == uid and saved.get('pid') == saved.get('pgid') and
                all(saved.get(key) == current.get(key) for key in ('pid', 'uid', 'pgid', 'start')))


def atomic_record(path, value):
    canonical(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())


def root_check(data):
    root = canonical(data['base'])
    if root.parent != Path(pwd.getpwuid(os.getuid()).pw_dir) or root.name != data['id']:
        raise ValueError('owned path outside exact test home identity')
    info = root.lstat()
    expected = data['root_identity']
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or \
            (info.st_dev, info.st_ino) != (expected['device'], expected['inode']):
        raise ValueError('owned directory identity changed; preserved')
    return root


def kill_verified(saved):
    current = process_identity(saved['pid'])
    if current is None:
        # If a leader disappeared while descendants survive, do not kill a
        # reused group. Leave evidence and the directory for manual recovery.
        code, table = output(['ps', '-axo', 'uid=,pgid=,stat='])
        if code or any(len(bits) == 3 and bits[1] == str(saved['pgid']) and not bits[2].startswith('Z')
                       for bits in (line.split() for line in table.splitlines())):
            raise ValueError('leader missing with possible group leftovers; preserved')
        return
    if not verified_process(saved, current, os.getuid()):
        raise ValueError('process identity changed; refused to kill reused PID')
    code, table = output(['ps', '-axo', 'uid=,pgid=,stat='])
    if code or any(len(bits) == 3 and bits[1] == str(saved['pgid']) and int(bits[0]) != os.getuid()
                   for bits in (line.split() for line in table.splitlines())):
        raise ValueError('process group includes an unowned process')
    # Keep the live leader until this identity check and signal. macOS has no
    # pidfd; this is start-identity verification, not a hostile-code guarantee.
    os.killpg(saved['pgid'], signal.SIGKILL)


def launch_command(data):
    root = root_check(data)
    slot = canonical(root / 'control' / str(data['sequence']))
    env = {'PATH': str(root / 'bin') + ':' + str(root / 'repo/.venv/bin') + ':/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin',
           'HOME': str(root / 'home'), 'TMPDIR': str(root / 'tmp'), 'LANG': 'en_US.UTF-8',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
    env.update(data['environment'])
    # The leader waits until its identity has been fsynced, then executes the
    # measured shell and remains alive to prevent PID reuse during cleanup.
    child = """import os,subprocess,sys,time
while not os.path.exists(sys.argv[1]+'.go'): time.sleep(.02)
code=subprocess.call(['sh','-c',sys.argv[2]],stdin=subprocess.DEVNULL)
with open(sys.argv[1]+'.exit','x') as stream: stream.write(str(code))
time.sleep(86400)
"""
    outpath = Path(str(slot) + '.out')
    fd = os.open(outpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        proc = subprocess.Popen(['/usr/bin/python3', '-c', child, str(slot), data['measured']],
                                cwd=root / 'repo', env=env, stdin=subprocess.DEVNULL,
                                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    saved = process_identity(proc.pid)
    if not verified_process(saved or {}, saved, os.getuid()):
        raise ValueError('new owned process identity unavailable; no commands released')
    record = Path(str(slot) + '.process.json')
    atomic_record(record, saved)
    return saved


def run_command(data):
    root = root_check(data)
    slot = canonical(root / 'control' / str(data['sequence']))
    saved = data['process']
    recorded = json.loads(Path(str(slot) + '.process.json').read_text())
    if saved != recorded or not verified_process(saved, process_identity(saved['pid']), os.getuid()):
        raise ValueError('process journal or start identity changed; command not released')
    started = time.monotonic()
    outpath = Path(str(slot) + '.out')
    atomic_record(Path(str(slot) + '.go'), {})
    code, timed, capped = 0, False, False
    try:
        while not Path(str(slot) + '.exit').exists():
            if not verified_process(saved, process_identity(saved['pid']), os.getuid()):
                raise ValueError('process leader exited before result; inspect process journal')
            if outpath.stat().st_size > data['cap']:
                code, capped = 125, True
                break
            if time.monotonic() - started >= data['budget']:
                code, timed = 124, True
                break
            time.sleep(.05)
        if not code:
            code = int(Path(str(slot) + '.exit').read_text())
    finally:
        kill_verified(saved)
        for _ in range(30):
            if process_identity(saved['pid']) is None:
                break
            time.sleep(.1)
    # Confirm the entire journaled process group disappeared, including children.
    kill_verified(saved)
    data_out = outpath.read_bytes()[:data['cap']]
    if outpath.stat().st_size > data['cap']:
        code, capped = 125, True
    steps = Path(str(slot) + '.steps')
    return dict(exit_code=code, seconds=round(time.monotonic()-started, 3), timed_out=timed,
                truncated=capped, output_b64=base64.b64encode(data_out).decode(),
                steps_b64=base64.b64encode(steps.read_bytes() if steps.exists() else b'').decode(),
                )


def remote_action(action, data):
    if action == 'probe':
        user = pwd.getpwuid(os.getuid()).pw_name
        return account_probe(user)
    if action == 'manifest':
        account = account_probe(data['user'])
        validate_account(account, data['user'])
        return dict(account=account, manifest=home_manifest(account['home'], data.get('exclude', '')))
    if action == 'create':
        account = account_probe(data['user'])
        validate_account(account, data['user'])
        root = canonical(data['base'])
        if root.parent != Path(account['home']) or root.name != data['id']:
            raise ValueError('invalid owned root')
        root.mkdir(mode=0o700)
        info = root.lstat()
        return dict(device=info.st_dev, inode=info.st_ino, uid=info.st_uid)
    if action == 'launch':
        return launch_command(data)
    if action == 'command':
        return run_command(data)
    if action == 'cleanup':
        root = root_check(data)
        control = canonical(root / 'control')
        if control.exists():
            recorded = []
            for record in sorted(control.glob('*.process.json'), key=lambda path: int(path.name.split('.')[0])):
                canonical(record)
                if record.lstat().st_uid != os.getuid():
                    raise ValueError('unowned process record')
                saved = json.loads(record.read_text())
                recorded.append(saved)
            if recorded != data['processes']:
                raise ValueError('process journal differs from controller ownership evidence; preserved')
            for saved in recorded:
                kill_verified(saved)
                time.sleep(.1)
                kill_verified(saved)
        # Verify every entry is owned before removing the exact journaled tree.
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                if (Path(directory) / name).lstat().st_uid != os.getuid():
                    raise ValueError('unowned entry inside trial path; preserved')
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError('safe directory cleanup unavailable')
        shutil.rmtree(root)
        if os.path.lexists(root):
            raise ValueError('owned root remains')
        return {'removed': data['base'], 'processes': 'verified absent'}
    raise ValueError('unknown dedicated-account operation')


def attest_sudo(test_user, *, yes=False):
    """Administrator-only audit; never grant privileges or modify sudo policy."""
    if not yes or os.getuid() != 0 or not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', test_user):
        raise ValueError('administrator audit requires root, --test-user and --yes')
    account = pwd.getpwnam(test_user)
    code, groups = output(['id', '-Gn', test_user])
    if code or account.pw_uid == 0 or set(groups.split()) & {'admin', 'wheel'}:
        raise ValueError('standard test account required')
    code, policy = output(['sudo', '-l', '-U', test_user])
    if not policy_denied(policy, test_user):
        raise ValueError('sudo policy denial not established; attestation not written')
    path = canonical(Path(account.pw_dir) / '.readtrial-sudo-denied.json')
    atomic_record(path, dict(user=test_user, uid=account.pw_uid, gid=account.pw_gid,
                            groups=groups.split(), sudo='denied', audited_at=time.time()))
    os.chmod(path, 0o644)
    print('Root-owned sudo denial attestation written (valid for 24 hours).')
    return 0


# REMOTE_END
if __name__ == '__main__':
    # The administrator audit works with stock Python 3.9, without importing
    # the controller (which needs 3.11+) or installing anything as the owner.
    import argparse
    audit_parser = argparse.ArgumentParser(description='Administrator-only macOS sudo denial audit; no policy changes')
    audit_parser.add_argument('--test-user', required=True)
    audit_parser.add_argument('--yes', action='store_true')
    audit_args = audit_parser.parse_args()
    try:
        raise SystemExit(attest_sudo(audit_args.test_user, yes=audit_args.yes))
    except (ValueError, OSError) as exc:
        audit_parser.exit(2, 'readtrial sudo audit: ' + str(exc) + '\n')

from .process import Result, execute
from .trusted import TrustedSSH


def remote_program(action):
    source = Path(__file__).read_text().split('# REMOTE_END', 1)[0]
    return source + '\nimport sys\nprint(json.dumps(remote_action(' + repr(action) + ', json.load(sys.stdin))))\n'


def local_probe(test_user):
    return account_probe(test_user)


def provisioning_steps(test_user=''):
    user = test_user if re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', test_user) else 'trialuser'
    return '''Dedicated macOS account setup (administrator performs steps 1-3):

1. In an administrator Terminal, create a NEW standard account and its own group.
   If this account already exists, skip creation and use doctor to verify it.
   Remote Login must already be enabled by the administrator. Do not change services.

```sh
TRIAL_USER=''' + shlex.quote(user) + '''
TRIAL_GROUP="${TRIAL_USER}grp"
TRIAL_HOME=/Users
TRIAL_HOME="$TRIAL_HOME/$TRIAL_USER"
if id "$TRIAL_USER" >/dev/null 2>&1; then echo 'Account exists; run doctor instead'; exit 1; fi
if dscl . -read "/Groups/$TRIAL_GROUP" >/dev/null 2>&1; then echo 'Group exists; choose a new name'; exit 1; fi
TRIAL_GID=$(dscl . -list /Groups PrimaryGroupID | awk 'BEGIN {m=500} $2+0>m {m=$2} END {print m+1}')
sudo dscl . -create "/Groups/$TRIAL_GROUP"
sudo dscl . -create "/Groups/$TRIAL_GROUP" PrimaryGroupID "$TRIAL_GID"
sudo sysadminctl -addUser "$TRIAL_USER" -fullName 'README trial' -GID "$TRIAL_GID" -home "$TRIAL_HOME" -shell /bin/sh -password -
sudo dscl . -create "$TRIAL_HOME" PrimaryGroupID "$TRIAL_GID"
sudo chown "$TRIAL_USER:$TRIAL_GROUP" "$TRIAL_HOME"
sudo chmod 700 "$TRIAL_HOME"
# No -admin flag. Do not add this account to admin or wheel.
# If Remote Login allows selected users, add only this user to its existing SSH group:
sudo dseditgroup -o edit -a "$TRIAL_USER" -t user com.apple.access_ssh
```

2. On the Linux controller, make a trial-only key (choose a NEW filename):

```sh
mkdir -p -m 700 .scratch/mac-key
ssh-keygen -t ed25519 -N '' -f .scratch/mac-key/id_ed25519
cat .scratch/mac-key/id_ed25519.pub
```

   Copy that one PUBLIC key into the following administrator-side Terminal block:

```sh
TRIAL_USER=''' + shlex.quote(user) + '''
TRIAL_GROUP="${TRIAL_USER}grp"
TRIAL_HOME=/Users
TRIAL_HOME="$TRIAL_HOME/$TRIAL_USER"
printf 'Paste the trial public key line, then press Enter: '
read -r TRIAL_PUBLIC_KEY
sudo install -d -m 700 -o "$TRIAL_USER" -g "$TRIAL_GROUP" "$TRIAL_HOME/.ssh"
printf 'restrict %s\\n' "$TRIAL_PUBLIC_KEY" | sudo tee -a "$TRIAL_HOME/.ssh/authorized_keys" >/dev/null
sudo chown "$TRIAL_USER:$TRIAL_GROUP" "$TRIAL_HOME/.ssh/authorized_keys"
sudo chmod 600 "$TRIAL_HOME/.ssh/authorized_keys"
```

   On the controller, replace your-mac with the Mac's hostname or IP address:

```sh
ssh -i .scratch/mac-key/id_ed25519 -o PasswordAuthentication=no ''' + user + '''@your-mac 'id; test "$(id -u)" != 0'
# Check the displayed host key fingerprint with the administrator.
# Add an SSH alias using this key so later commands select the same account:
TRIAL_KEY_PATH="$PWD/.scratch/mac-key/id_ed25519"
cat >> "$HOME/.ssh/config" <<SSH_CONFIG
Host readtrial-mac
    HostName your-mac
    User ''' + user + '''
    IdentityFile "$TRIAL_KEY_PATH"
    IdentitiesOnly yes
    PasswordAuthentication no
SSH_CONFIG
# This verifies key authentication; server-side password policy belongs to the administrator.
```

3. Sudo policy audit. A password prompt from sudo -n -l is NOT proof of denial.
   From the supplied readtrial checkout on the Mac, using stock Python 3.9:

```sh
sudo /usr/bin/python3 readtrial/account.py --test-user ''' + user + ''' --yes
```

   With Python 3.11+, the equivalent CLI is readtrial attest-sudo --test-user
   ''' + user + ''' --yes (also administrator-only).

   This checks sudo -l -U, changes no policy, and creates a root-owned denial file
   in the test home, valid for 24 hours. It refuses ambiguous/allowed policy.
   It refuses to overwrite an existing file. To renew, the administrator removes
   only "$TRIAL_HOME/.readtrial-sudo-denied.json" from step 1, then repeats the audit.
   Other users must ensure their home and .ssh deny this test account;
   doctor checks refusal without reading their contents. Resolve failed checks
   as the administrator; readtrial never changes other users' permissions.

4. From the Linux controller checkout:

```sh
python3 -m readtrial doctor --target ssh:readtrial-mac --test-user ''' + user + '''
python3 -m readtrial run ./your-trusted-repo --target ssh:readtrial-mac --dedicated-account --trusted-target --agent-command 'claude -p --model claude-haiku-5-5' --check 'python3 -m unittest discover -s tests -q' --out trial-results/mac-first
```

Use only trusted repositories. A fresh HOME/XDG/tmp/cache/tools is made under the
test account's home for each trial. Commands never switch users. Network access,
world-readable files, and the test account's original home remain reachable.
Pre/post guards hash pre-existing test-home files and verify other-user access refusal.
Cleanup removes only the exact journaled owned directory and start-verified process
groups. It preserves unknown paths/PIDs and reports INCOMPLETE on guard failure.
macOS has no pidfd: start verification cannot guarantee a race-free hostile-code
boundary. Escaped process groups cannot be attributed reliably. Use a VM for that.
'''




class AccountSSH(TrustedSSH):
    def __init__(self, target):
        super().__init__(target)
        self.observed = None
        self.before_manifest = None
        self.root_identity = None
        self.processes = []

    def request(self, action, data, *, timeout=30):
        from .targets import TargetError
        t = self.target
        result = execute(t.outer(['/usr/bin/python3', '-c', remote_program(action)]),
                         data=json.dumps(data).encode(), timeout=timeout, cap=4*1024*1024)
        t.log('account-' + action, 'dedicated account ' + action + ' (fixed management code)', result)
        if result.exit_code or result.timed_out or result.truncated:
            raise TargetError('dedicated account ' + action + ' failed: ' + result.output)
        try:
            return json.loads(result.output)
        except ValueError as exc:
            raise TargetError('invalid dedicated-account response') from exc

    def before(self):
        from .targets import TargetError
        t = self.target
        self.observed = self.request('probe', {})
        expected = t.value.split('@', 1)[0] if '@' in t.value else self.observed['user']
        try:
            validate_account(self.observed, expected)
            if not t.journal:
                raise ValueError('durable ownership journal required')
            self.before_manifest = self.request('manifest', {'user': expected})['manifest']
            from .isolation import atomic_json
            atomic_json(t.guard_directory / 'account-before.json', {'account': self.observed, 'manifest': self.before_manifest})
        except ValueError as exc:
            raise TargetError(str(exc)) from exc
        t.capabilities['preflight'] = {'status': 'account-attested', 'account': self.observed}

    def create_root(self):
        from .isolation import atomic_json
        t = self.target
        t.base = self.observed['home'] + '/' + t.id
        t.journal.intent('target-path', t.base)
        self.root_identity = self.request('create', {'user': self.observed['user'], 'base': t.base, 'id': t.id})
        t.created = True
        atomic_json(t.guard_directory / 'account-root.json', {'base': t.base, 'id': t.id, 'root_identity': self.root_identity})
        return t.base

    def authorize_cleanup(self):
        from .targets import TargetError
        t = self.target
        if not t.journal or {'kind': 'target-path', 'identity': t.base} not in t.journal.value['resources']:
            raise TargetError('exact owned path requires durable journal')
        if not self.observed or t.base != self.observed['home'] + '/' + t.id or not self.root_identity:
            raise TargetError('exact owned directory identity unavailable')

    def transport(self, command, budget, *, guard=True):
        from .isolation import atomic_json
        from .installers import environment
        from .shell import posix_script
        t = self.target
        self.authorize_cleanup()
        slot = t.base + '/control/' + str(self.sequence)
        measured = (self.path_guard(command) if guard else '') + posix_script(command, shlex.quote(slot + '.steps'))
        payload = dict(base=t.base, id=t.id, root_identity=self.root_identity,
                       sequence=self.sequence, budget=budget, cap=t.limits.output_bytes,
                       measured=measured, environment=environment(t.base))
        saved = self.request('launch', payload)
        self.processes.append(saved)
        atomic_json(t.guard_directory / 'account-processes.json', {'processes': self.processes})
        # Controller evidence is durable BEFORE any repository command starts.
        payload['process'] = saved
        value = self.request('command', payload, timeout=budget + 10)
        return Result(0, json.dumps(value), 0)

    def close(self):
        from .isolation import atomic_json
        from .targets import TargetError
        t = self.target
        errors = []
        if t.created:
            self.authorize_cleanup()
            self.request('cleanup', dict(base=t.base, id=t.id, root_identity=self.root_identity,
                                         processes=self.processes))
            t.created = False
        if self.before_manifest is not None:
            try:
                after = self.request('manifest', {'user': self.observed['user']})
                atomic_json(t.guard_directory / 'account-after.json', after)
                if self.before_manifest != after['manifest']:
                    raise ValueError('test account pre-existing home files changed; preserved')
                t.capabilities['guards']['account_home'] = 'unchanged; owner access still refused'
            except (ValueError, TargetError) as exc:
                t.capabilities['guards']['account_home'] = 'failed'
                errors.append(str(exc))
        if errors:
            raise TargetError('; '.join(errors))
