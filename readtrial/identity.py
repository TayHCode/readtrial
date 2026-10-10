"""Finding identity comes from observed failures, never model wording."""
from __future__ import annotations

import hashlib
import json

from .coverage import succeeded
from .shell import split


def normalized_command(command: str, windows: bool = False) -> str:
    # Normalize separator spacing only. Preserve arguments, quotes, case and
    # whitespace inside shell text: those can change what the command does.
    try:
        parts, operators = split(command, windows)
    except ValueError:
        return command.strip()
    return ' '.join(part.strip() + (' ' + operators[i] if i < len(operators) else '')
                    for i, part in enumerate(parts))


def canonical_findings(items: list[dict], records: list[dict], machine_class: str,
                       windows: bool = False) -> list[dict]:
    failures = [e for e in records if e.get('phase') == 'trial' and e.get('kind') == 'command'
                and not e.get('denied') and not succeeded(e)]
    result = []
    seen = set()
    for original in items:
        item = dict(original)
        command = normalized_command(item['command'], windows)
        matches = [e for e in failures if normalized_command(e['command'], windows) == command]
        if not matches:
            # A model may name the failed segment of a compound proposal. Match
            # only a unique proposal; otherwise its cwd/meaning is ambiguous.
            matches = [e for e in failures if any(step['exit_code'] and
                       normalized_command(step['command'], windows) == command for step in e.get('steps', []))]
        keys = {}
        for entry in matches:
            step = next((s for s in entry.get('steps', []) if s['exit_code']), None)
            key = (machine_class, normalized_command(entry['command'], windows),
                   step['exit_code'] if step else entry['exit_code'], step['segment'] if step else 1)
            keys[key] = entry
        if len(keys) == 1:
            key, entry = next(iter(keys.items()))
            item['command'] = entry['command']
            prefix = 'failure-'
        else:
            # Unknown exit and segment zero distinguish unevidenced opinions
            # from recorded failures. Their wording and model IDs still cannot
            # create a phantom identity change for the same advisory command.
            key = (machine_class, command, None, 0)
            prefix = 'advisory-'
        item['id'] = prefix + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:20]
        if item['id'] not in seen:
            result.append(item)
            seen.add(item['id'])
    return result
