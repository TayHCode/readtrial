"""Inspect top-level shell segments and record their statuses independently."""
from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from pathlib import Path


EXECUTION_WRAPPERS = {'sh', 'bash', 'dash', 'zsh', 'ksh', 'csh', 'tcsh', 'fish', 'busybox',
                      'env', 'timeout', 'nice', 'nohup', 'xargs', 'eval', 'exec', 'command', '.', 'source'}


def direct_argv(command: str) -> list[str] | None:
    """Recognize literal external argv, without shell expansions or builtins."""
    quote = ''
    index = 0
    while index < len(command):
        char = command[index]
        if char == '\\' and quote != "'":
            if index + 1 == len(command) or command[index + 1] == '\n':
                return None
            # POSIX double quotes remove these backslashes; shlex preserves
            # them. Let the shell interpret the original text instead.
            if quote == '"' and command[index + 1] in '$`':
                return None
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = ''
        elif quote:
            if char in '$`':
                return None
            if char == quote:
                quote = ''
        elif char in "\"'":
            quote = char
        elif char in '\r\n;&|()<>{}*?[]~`$#':
            return None
        index += 1
    if quote:
        return None
    words = shlex.split(command)
    builtins = {':', '.', 'alias', 'bg', 'break', 'cd', 'command', 'continue', 'echo', 'eval', 'exec',
                'exit', 'export', 'false', 'fc', 'fg', 'getopts', 'hash', 'jobs', 'kill', 'local',
                'printf', 'pwd', 'read', 'readonly', 'return', 'set', 'shift', 'source', 'test',
                'times', 'trap', 'true', 'type', 'ulimit', 'umask', 'unalias', 'unset', 'wait',
                'if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'until', 'do', 'done', 'case',
                'esac', 'in', 'time', '!'}
    if (not words or words[0] in builtins or posixpath.basename(words[0]) in EXECUTION_WRAPPERS
            or '=' in words[0]):
        return None
    return words


def trim_segment(command: str, windows: bool = False) -> str:
    """Remove surrounding shell whitespace, preserving quoted/escaped bytes."""
    if windows:
        return command.strip()
    start = 0
    while start < len(command) and command[start] in ' \t\n':
        start += 1
    quote = ''
    index = end = start
    while index < len(command):
        char = command[index]
        if char == '\\' and quote != "'":
            index = min(index + 2, len(command))
            end = index
            continue
        if quote:
            if char == quote:
                quote = ''
            end = index + 1
        elif char in "\"'`":
            quote = char
            end = index + 1
        elif char not in ' \t\n':
            end = index + 1
        index += 1
    return command[start:end]


def split(command: str, windows: bool = False, *, reject_opaque: bool = False) -> tuple[list[str], list[str]]:
    segments: list[str] = []
    operators: list[str] = []
    quote = ''
    depth = 0
    start = index = 0
    while index < len(command):
        char = command[index]
        if char == ('`' if windows else '\\') and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                if windows and index + 1 < len(command) and command[index + 1] == quote:
                    index += 2
                    continue
                quote = ''
        elif char in ("\"'" if windows else "\"'`"):
            quote = char
        elif char in '({':
            depth += 1
        elif char in ')}':
            depth -= 1
            if depth < 0:
                raise ValueError('unbalanced shell grouping')
        elif depth == 0 and char in '&|;\n':
            op = command[index:index + 2] if command[index:index + 2] in {'&&', '||'} else char
            # Redirection 2>&1 and background jobs are not list separators.
            if char == '&' and index and command[index - 1] in '<>':
                index += 1
                continue
            if op == '&':
                raise ValueError('background commands are unsupported; use one foreground command')
            segment = trim_segment(command[start:index], windows)
            if segment:
                segments.append(segment)
                operators.append(';' if op == '\n' else op)
            elif op not in {';', '\n'}:
                raise ValueError('empty shell segment')
            index += len(op)
            start = index
            continue
        index += 1
    if quote or depth:
        raise ValueError('unbalanced shell quoting or grouping')
    final = trim_segment(command[start:], windows)
    if final:
        segments.append(final)
    elif operators:
        if operators[-1] not in {';'}:
            raise ValueError('trailing shell operator requires a command')
        operators.pop()
    if not segments:
        raise ValueError('empty command')
    # Control structures are opaque units. Their internals need separate proposals
    # for independent step evidence; do not rewrite their grammar.
    if not windows and any(re.match(r'^(?:if|then|else|elif|fi|for|while|until|do|done|case|esac)\b', s) for s in segments):
        if reject_opaque:
            raise ValueError('opaque shell control flow cannot prove execution')
        return [command], []
    return segments, operators


def opaque_syntax(command: str) -> bool:
    """Nested shell code has no independent status witness in this protocol."""
    quote = ''
    index = 0
    while index < len(command):
        char = command[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = ''
        elif char == '`' or command[index:index + 2] == '$(':
            return True
        elif quote:
            if char == quote:
                quote = ''
        elif char in "\"'":
            quote = char
        elif char in '({<>':
            return True
        index += 1
    return False


def denied(index: int, segment: str, reason: str) -> str:
    return f'segment {index} `{segment[:160]}` is denied: {reason}'


def path_requests(command: str, windows: bool = False) -> list[str]:
    """Visible relative paths, with cd state inside this one shell proposal.

    Preserve .. until physical resolution: collapsing link/.. beforehand would
    hide a symlink escape. Computed paths/child code remain outside text policy.
    """
    segments, _ = split(command, windows)
    cwd = '.'
    requests = []
    for segment in segments:
        words = shlex.split(segment, posix=not windows)
        words = [word.strip('\"\'') for word in words]
        changing = bool(words and words[0].lower() in {'cd', 'set-location', 'pushd'})
        for index, word in enumerate(words):
            # Quoted program bodies are code, not shell path arguments.
            if index and words[index - 1].lower() in {'-c', '-command'}:
                continue
            text = word.replace('\\', '/') if windows else word
            if not text or re.search(r'\s|://|[${}%`]', text) or text.startswith('-'):
                continue
            # Bare file arguments can be symlinks too (python3 hello.py, cat link).
            # Resolve visible arguments conservatively; nonexistent names remain
            # harmless in-clone paths. The program name itself is a PATH lookup.
            if index > 0 or text == '..' or '/' in text:
                if text.startswith('/') or re.match(r'^[A-Za-z]:', text):
                    requests.append(text)
                else:
                    requests.append(cwd + '/' + text)
                if changing and index == len(words) - 1:
                    cwd = requests[-1]
    return requests


def path_policy(command: str, root: Path | None = None, windows: bool = False) -> str:
    try:
        requests = path_requests(command, windows)
        base = root.resolve() if root else None
        for text in requests:
            if base is not None and not windows:
                if not (base / text).resolve().is_relative_to(base):
                    return 'resolved path outside workspace'
            else:
                text = re.sub(r'^/workspace(?=/|$)', '.', text)
                normalized = ntpath.normpath(text) if windows else posixpath.normpath(text)
                if normalized == '..' or normalized.startswith(('../', '..\\')):
                    return 'path outside workspace'
    except (ValueError, OSError, RuntimeError):
        return 'path inspection failed'
    return ''


def posix_script(command: str, status: str) -> str:
    segments, operators = split(command)
    def record(index):
        # Expand $? before starting the observer. Its positional arguments and
        # variables live in a child shell; no assignment touches the user's shell.
        # Returning the observed status preserves $? and && / || semantics.
        logger = f"printf '{index}:%s\\n' \"$1\" >> {status} || exit 125; exit \"$1\""
        return '(/bin/sh -c ' + shlex.quote(logger) + ' readtrial-status "$?")'
    if len(segments) == 1:
        return command + '\n' + record(1)
    output = []
    for index, segment in enumerate(segments, 1):
        output.append('{\n' + segment + '\n' + record(index) + '\n}')
        if index <= len(operators):
            output.append(operators[index - 1])
    return ' '.join(output)


def statuses(text: str, command: str, windows: bool = False) -> list[dict]:
    segments, _ = split(command, windows)
    result = []
    for line in text.splitlines():
        match = re.fullmatch(r'(\d+):(-?\d+)', line.strip())
        if not match:
            raise ValueError('invalid step-status evidence')
        index, code = map(int, match.groups())
        if not 1 <= index <= len(segments):
            raise ValueError('unknown step-status index')
        if any(row['segment'] == index for row in result):
            raise ValueError('duplicate step-status index')
        result.append({'segment': index, 'command': segments[index - 1], 'exit_code': code})
    return sorted(result, key=lambda step: step['segment'])


def powershell_script(command: str, status: str) -> str:
    segments, operators = split(command, True)
    if any(op in {'&&', '||'} for op in operators):
        return command  # PowerShell 5.1 records its real parse failure.
    # Script-block pipeline stages consume $input explicitly. Record each native
    # exit before the next stage or an echo overwrites it. Finite output is still
    # bounded by the trusted runner's watchdog and output cap.
    groups = []
    group: list[tuple[int, str]] = []
    for index, segment in enumerate(segments, 1):
        group.append((index, segment))
        if index == len(segments) or operators[index - 1] != '|':
            groups.append(group)
            group = []
    output = ["$script:rt_failed=0; $script:rt_previous_ok=$true"]
    for group in groups:
        stages = []
        for offset, (index, segment) in enumerate(group):
            # Status logging itself changes $?. Preserve documented conditional
            # guards (including inside double-quoted strings) across statements.
            parts = re.split("('(?:[^']|'')*')", segment)
            segment = ''.join(part if i % 2 else part.replace('$?', '$script:rt_previous_ok')
                              for i, part in enumerate(parts))
            body = ["$global:LASTEXITCODE=0", ('$input | ' if offset else '') + segment,
                    '$rt_ok=$?; $rt_code=$global:LASTEXITCODE',
                    'if (!$rt_ok -and !$rt_code) { $rt_code=1 }',
                    f"[IO.File]::AppendAllText({status}, '{index}:'+[string]$rt_code+[Environment]::NewLine)",
                    'if ($rt_code -ne 0 -and $script:rt_failed -eq 0) { $script:rt_failed=$rt_code }',
                    '$script:rt_previous_ok=$rt_ok; $global:LASTEXITCODE=$rt_code']
            stages.append('& {\n' + '\n'.join(body) + '\n}')
        output.append(' | '.join(stages) if len(group) > 1 else '\n'.join(body))
    output.append('exit $script:rt_failed')
    return '\n'.join(output)
