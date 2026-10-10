"""Inspect top-level shell segments and record their statuses independently."""
from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from pathlib import Path


def split(command: str, windows: bool = False) -> tuple[list[str], list[str]]:
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
            segment = command[start:index].strip()
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
    final = command[start:].strip()
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
        return [command], []
    return segments, operators


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
    if len(segments) == 1:
        # Still record a single opaque shell's status separately from later output.
        return ('sh -c ' + shlex.quote(command) + '; rt_code=$?; '
                f"printf '1:%s\\n' \"$rt_code\" >> {status}; exit \"$rt_code\"")
    output = []
    for index, segment in enumerate(segments, 1):
        output.append('{\n' + segment + '\nrt_code=$?\n' +
                      f"printf '{index}:%s\\n' \"$rt_code\" >> {status}\n" +
                      '(exit "$rt_code")\n}')
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
