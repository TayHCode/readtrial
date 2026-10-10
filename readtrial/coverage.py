"""Deterministic, conservative coverage of documented quickstart commands."""
from __future__ import annotations

import re

from .shell import split


def declared_steps(numbered: str, platform: str) -> list[dict]:
    """Select the first compatible shell block in each Quickstart section.

    Prefer a matching platform subsection; do not count alternative prerequisite
    routes or optional installation blocks as additional required commands. If
    there is no Quickstart heading, use the first compatible shell block.
    """
    lines = [re.sub(r'^\s*\d+\s', '', line) for line in numbered.splitlines()]
    headings = []
    open_fence = ''
    for i, line in enumerate(lines):
        fence = re.match(r'^\s*(`{3,}|~{3,})', line)
        if fence:
            open_fence = '' if open_fence == fence[1] else fence[1] if not open_fence else open_fence
        elif not open_fence and (m := re.match(r'^(#{1,6})\s+(.+)', line)):
            headings.append((i, len(m[1]), m[2].lower()))
    sections = []
    for start, level, title in headings:
        if 'quickstart' in title or 'quick start' in title:
            end = next((i for i, depth, _ in headings if i > start and depth <= level), len(lines))
            sections.append((start + 1, end, level))
    if not sections:
        sections = [(0, len(lines), 0)]
    wanted = {'Windows': 'windows', 'Darwin': 'macos'}.get(platform, 'linux')
    compatible = {'powershell', 'ps1'} if platform == 'Windows' else {'sh', 'bash', 'shell', 'console', 'zsh'}
    result = []
    for start, end, level in sections:
        routes = [(i, depth, title) for i, depth, title in headings if start <= i < end and depth > level
                  and any(name in title for name in ('linux', 'macos', 'mac os', 'windows'))]
        if routes:
            selected = next(((i, depth, title) for i, depth, title in routes
                             if wanted in title or (wanted == 'macos' and 'mac os' in title)), None)
            if selected:
                start, depth, _ = selected
                end = next((i for i, d, _ in headings if start < i < end and d <= depth), end)
            else:
                # The section's own block may be the POSIX route, followed by
                # only a Windows subsection. Never fall into another OS route.
                end = min(i for i, _, _ in routes)
        index = start
        while index < end:
            fence = re.match(r'^\s*(`{3,}|~{3,})\s*(\w*)\s*$', lines[index])
            if not fence:
                index += 1
                continue
            closing = index + 1
            while closing < end and not re.match(r'^\s*' + re.escape(fence[1]) + r'\s*$', lines[closing]):
                closing += 1
            if fence[2].lower() in compatible and closing < end:
                pending = ''
                first_line = 0
                for row in range(index + 1, closing):
                    text = re.sub(r'^(?:\$ |PS> )', '', lines[row].strip())
                    if not text or text.startswith('#'):
                        continue
                    if not pending:
                        first_line = row + 1
                    continued = text.endswith('`' if platform == 'Windows' else '\\')
                    pending += (text[:-1] if continued else text) + (' ' if continued else '')
                    if not continued:
                        result.append({'line': first_line, 'command': pending.strip()})
                        pending = ''
                break
            index = closing + 1
    return result


def succeeded(entry: dict) -> bool:
    return (entry['exit_code'] == 0 and not any(entry.get(name) for name in ('denied', 'truncated', 'timed_out'))
            and not any(step['exit_code'] for step in entry.get('steps', [])))


def coverage(steps: list[dict], commands: list[dict], windows: bool = False) -> dict:
    # Exact commands or exact top-level segments count. Never let agent opinions,
    # independent checks, a duplicate proposal, or an unexecuted && tail count.
    ran: set[int] = set()
    successful: set[int] = set()
    for entry in commands:
        if entry.get('denied'):
            continue
        observed = {entry['command'].strip(): succeeded(entry)}
        # A segment after cd has a different cwd from a standalone declaration.
        moved = any(re.match(r'(?i)^\s*(?:cd|set-location|pushd)\b', step['command'])
                    for step in entry.get('steps', []))
        if not moved:
            observed.update({step['command'].strip(): step['exit_code'] == 0 for step in entry.get('steps', [])})
        for index, step in enumerate(steps):
            if index in ran:
                continue
            text = step['command'].strip()
            if text in observed:
                ran.add(index)
                if observed.pop(text):
                    successful.add(index)
                continue
            try:
                parts, _ = split(text, windows)
            except ValueError:
                continue
            if len(parts) > 1 and all(part in observed for part in parts):
                ran.add(index)
                if all(observed[part] for part in parts):
                    successful.add(index)
    return {'declared': len(steps), 'ran': len(ran), 'succeeded': len(successful),
            'complete': bool(steps) and len(successful) == len(steps),
            'steps': [dict(step, ran=i in ran, succeeded=i in successful) for i, step in enumerate(steps)]}
