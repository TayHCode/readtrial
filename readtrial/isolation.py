"""Owned environment setup, controller preservation and exact-resource records."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import uuid
from pathlib import Path

process = importlib.import_module('.process', __package__) if __package__ else importlib.import_module('process')
execute = process.execute

MAX_ENTRIES = 4096
MAX_BYTES = 256 * 1024 * 1024
IDENTITY = re.compile(r'[a-z][a-z0-9-]{0,30}[0-9a-f]{32}')


def no_links(path: Path) -> Path:
    """Reject noncanonical paths and linked ancestors, including dangling links."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('expected canonical absolute path')
    for parent in reversed((path, *path.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('symlink in protected or evidence path')
    return path


def make_environment(root: Path) -> None:
    """Make only fresh owned roots; never accept linked existing destinations."""
    no_links(root)
    for name in ('home', 'tmp', 'bin', 'python', 'cache', 'tools',
                 'home/config', 'home/data', 'home/cache', 'home/state'):
        path = root / name
        no_links(path)
        path.mkdir(mode=0o700, exist_ok=False)


def open_directory(path: Path) -> int:
    """Open each component relative to a held descriptor, without link races."""
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def extended_metadata(descriptor: int) -> dict:
    """Fingerprint extended metadata, including POSIX ACLs, without publishing it."""
    names = os.listxattr(descriptor)
    if len(names) > 128:
        raise ValueError('extended metadata entry limit exceeded')
    result = {}
    total = 0
    for name in sorted(names):
        value = os.getxattr(descriptor, name)
        total += len(value)
        if total > 256 * 1024:
            raise ValueError('extended metadata byte limit exceeded')
        result[name] = hashlib.sha256(value).hexdigest()
    return result


def manifest(paths: list[Path]) -> dict:
    """Bounded declared-set fingerprint. No file contents enter evidence."""
    entries = {}
    total_bytes = 0
    for index, path in enumerate(paths):
        root = no_links(Path(path))
        stack = [(root, '.')]
        while stack:
            current, relative = stack.pop()
            if len(entries) >= MAX_ENTRIES:
                raise ValueError('preservation manifest entry limit exceeded')
            key = f'{index}:{relative}'
            try:
                info = current.lstat()
            except FileNotFoundError:
                if relative != '.':
                    raise ValueError('protected entry disappeared during manifest')
                entries[key] = {'type': 'absent'}
                continue
            entry = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
                     'device': info.st_dev, 'inode': info.st_ino, 'mtime_ns': info.st_mtime_ns,
                     'ctime_ns': info.st_ctime_ns, 'size': info.st_size}
            if stat.S_ISREG(info.st_mode):
                total_bytes += info.st_size
                if total_bytes > MAX_BYTES:
                    raise ValueError('preservation manifest byte limit exceeded')
                parent_fd = open_directory(current.parent)
                try:
                    fd = os.open(current.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                finally:
                    os.close(parent_fd)
                with os.fdopen(fd, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                        raise ValueError('protected file changed before read')
                    entry['xattrs'] = extended_metadata(stream.fileno())
                    digest = hashlib.sha256()
                    remaining = info.st_size
                    while remaining:
                        chunk = stream.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ValueError('protected file changed during read')
                        digest.update(chunk)
                        remaining -= len(chunk)
                    # atime is deliberately excluded: reading may update it.
                    after = os.fstat(stream.fileno())
                    if (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                        raise ValueError('protected file changed during read')
                entry.update(type='file', sha256=digest.hexdigest())
            elif stat.S_ISDIR(info.st_mode):
                entry['type'] = 'directory'
                directory_fd = open_directory(current)
                try:
                    opened = os.fstat(directory_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError('protected directory changed before read')
                    entry['xattrs'] = extended_metadata(directory_fd)
                    names = []
                    with os.scandir(directory_fd) as children:
                        for child in children:
                            if len(names) >= MAX_ENTRIES:
                                raise ValueError('preservation manifest entry limit exceeded')
                            names.append(child.name)
                    names.sort()
                    after = os.fstat(directory_fd)
                    if (after.st_mtime_ns, after.st_ctime_ns) != (info.st_mtime_ns, info.st_ctime_ns):
                        raise ValueError('protected directory changed during read')
                finally:
                    os.close(directory_fd)
                if len(names) + len(stack) + len(entries) >= MAX_ENTRIES:
                    raise ValueError('preservation manifest entry limit exceeded')
                entry['entries'] = names
                for name in reversed(names):
                    stack.append((current / name, name if relative == '.' else relative + '/' + name))
            elif stat.S_ISLNK(info.st_mode):
                # Record internal links without dereferencing. Escape or chains
                # are refused, even when their final destination is absent.
                link = os.readlink(current)
                destination = Path(os.path.abspath(current.parent / link))
                if not destination.is_relative_to(root):
                    raise ValueError('escaping protected symlink')
                no_links(destination)
                entry.update(type='symlink', target=link)
            else:
                raise ValueError('special file in protected set')
            entries[key] = entry
    return entries


def atomic_json(path: Path, value: dict) -> None:
    no_links(path)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def secure_directory(directory: Path) -> None:
    no_links(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('evidence directory must belong to current user with mode 0700')


class Journal:
    def __init__(self, directory: Path, identity: str, backend: str):
        if not IDENTITY.fullmatch(identity) or backend not in {'lxc', 'local-container', 'ssh'}:
            raise ValueError('invalid journal identity/backend')
        secure_directory(directory)
        self.id = identity
        self.path = directory / (identity + '.json')
        if os.path.lexists(self.path):
            raise ValueError('journal already exists')
        self.value = {'version': 1, 'id': identity, 'backend': backend, 'phase': 'planned', 'resources': []}
        # Claim the exact journal filename before replacing it atomically.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.save()

    def save(self) -> None:
        validate_journal(self.value)
        atomic_json(self.path, self.value)

    def intent(self, kind: str, identity: str) -> None:
        resource = {'kind': kind, 'identity': identity}
        if resource not in self.value['resources']:
            self.value['resources'].append(resource)
        self.value['phase'] = 'creating'
        self.save()

    def phase(self, phase: str) -> None:
        self.value['phase'] = phase
        self.save()


def validate_journal(value: dict) -> None:
    if not isinstance(value, dict) or set(value) != {'version', 'id', 'backend', 'phase', 'resources'}:
        raise ValueError('malformed ownership journal')
    if value['version'] != 1 or not isinstance(value['id'], str) or not IDENTITY.fullmatch(value['id']):
        raise ValueError('invalid journal identity/version')
    if value['backend'] not in {'lxc', 'local-container', 'ssh'} or value['phase'] not in {'planned', 'creating', 'running', 'closing', 'complete', 'incomplete'}:
        raise ValueError('invalid journal backend/phase')
    if not isinstance(value['resources'], list) or len(value['resources']) > 64:
        raise ValueError('invalid journal resource list')
    for entry in value['resources']:
        if not isinstance(entry, dict) or set(entry) != {'kind', 'identity'} or not isinstance(entry['identity'], str):
            raise ValueError('malformed journal resource')
        kind, identity = entry['kind'], entry['identity']
        if kind in {'lxc', 'oci'}:
            if identity != value['id'] or (kind == 'lxc' and value['backend'] != 'lxc') or (kind == 'oci' and value['backend'] != 'local-container'):
                raise ValueError('resource does not match journal identity/backend')
        elif kind in {'local-path', 'target-path'}:
            path = Path(identity)
            if not path.is_absolute() or '..' in path.parts or '\x00' in identity or value['id'] not in path.parts:
                raise ValueError('resource path does not match journal identity')
        else:
            raise ValueError('unsupported journal resource')


def leftovers(directory: Path) -> list[dict]:
    """Read-only discovery. Never sweep resources by prefix or remove anything."""
    no_links(directory)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('untrusted journal directory')
    files = []
    for path in directory.iterdir():
        if len(files) >= MAX_ENTRIES:
            raise ValueError('too many journals')
        files.append(path)
    files.sort()
    results = []
    for path in files:
        if path.suffix != '.json':
            continue
        no_links(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024 * 1024:
            raise ValueError('untrusted journal file')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            value = json.load(stream)
        validate_journal(value)
        if path.name != value['id'] + '.json':
            raise ValueError('journal filename identity mismatch')
        for resource in value['resources']:
            kind, identity = resource['kind'], resource['identity']
            status = 'unknown'
            if kind == 'local-path':
                no_links(Path(identity).parent)
                status = 'present' if os.path.lexists(identity) else 'absent'
            elif kind == 'target-path' and value['backend'] == 'lxc':
                # An absent enclosing instance proves all its paths absent.
                enclosing = next((item for item in results if item['id'] == value['id'] and item['kind'] == 'lxc'), None)
                if enclosing and enclosing['status'] == 'absent':
                    status = 'absent'
            elif kind == 'lxc':
                probe = execute(['lxc', 'list', identity, '--format', 'json'], timeout=10, cap=65536)
                if not (probe.exit_code or probe.truncated or probe.timed_out):
                    entries = json.loads(probe.output)
                    if not isinstance(entries, list):
                        raise ValueError('malformed LXC inventory')
                    exact = [item for item in entries if item.get('name') == identity]
                    status = 'absent' if not exact else ('present' if len(exact) == 1 and exact[0].get('config', {}).get('user.readtrial.id') == value['id'] else 'ownership-unverified')
            results.append({'id': value['id'], 'phase': value['phase'], **resource, 'status': status})
    return results


def recover(directory: Path, *, yes: bool = False) -> list[dict]:
    """Explicit exact-instance recovery. Journals remain unchanged as evidence."""
    if not yes:
        raise ValueError('Recovery deletes exact journal-owned LXC instances only; repeat with --yes.')
    found = leftovers(directory)
    if any(item['kind'] not in {'lxc', 'target-path'} or item['status'] == 'ownership-unverified'
           or (item['kind'] == 'lxc' and item['status'] not in {'absent', 'present'}) for item in found):
        raise ValueError('Recovery ownership or backend unproven; resources preserved.')
    for item in found:
        if item['kind'] != 'lxc' or item['status'] != 'present':
            continue
        identity = item['identity']
        # Re-read immediately before deletion; discovery does not authorize stale IDs.
        probe = execute(['lxc', 'list', identity, '--format', 'json'], timeout=10, cap=65536)
        if probe.exit_code or probe.timed_out or probe.truncated:
            raise ValueError('Recovery identity read-back failed; resource preserved.')
        entries = json.loads(probe.output)
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError('Recovery inventory is malformed; resources preserved.')
        exact = [entry for entry in entries if entry.get('name') == identity]
        if not exact:
            continue
        if len(exact) != 1 or exact[0].get('config', {}).get('user.readtrial.id') != item['id']:
            raise ValueError('Recovery ownership changed; resource preserved.')
        result = execute(['lxc', 'delete', '--force', identity], timeout=30, cap=65536)
        if result.exit_code or result.timed_out or result.truncated:
            raise ValueError('Exact instance recovery could not be confirmed.')
    remaining = leftovers(directory)
    if any(item['status'] != 'absent' for item in remaining):
        raise ValueError('Recovery incomplete; unresolved resources preserved.')
    return remaining
