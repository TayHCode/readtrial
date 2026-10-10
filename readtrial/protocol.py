"""One canonical command field, strict actions, and bounded error descriptions."""
from __future__ import annotations

import json
import os
import re

FIELDS = {'id', 'severity', 'type', 'machine', 'command', 'expected', 'actual', 'doc_line'}
COMMAND_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'command': {'type': 'string'}}, 'required': ['command'],
}
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'command': {'type': ['string', 'null'], 'description': 'Raw shell text when proposing a command; null only when done.'},
        'done': {'type': 'boolean', 'description': 'False when command is a string; true only when command is null.'},
        'findings': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {field: {'type': 'string'} for field in sorted(FIELDS)},
            'required': sorted(FIELDS)}, 'description': 'Must be [] for a command action. Include findings only on done:true.'},
    },
    'required': ['command', 'done', 'findings'],
}


def scrub(text: str) -> str:
    for name, value in os.environ.items():
        if len(value) >= 8 and (name.endswith(('_KEY', '_TOKEN', '_PASSWORD')) or name in {'HOME', 'CODEX_HOME'}):
            text = text.replace(value, '[redacted]')
    text = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[redacted]@', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[redacted-address]', text)
    return text[:1200] + (' [truncated]' if len(text) > 1200 else '')


def normalize(value: object, *, unwrap: bool = True, notes: list[str] | None = None) -> dict:
    if not isinstance(value, dict):
        raise TypeError('action must be a JSON object')
    keys = set(value) & {'command', 'proposed_command', 'cmd'}
    if len(keys) > 1:
        raise ValueError('conflicting command aliases; use only command')
    if keys:
        key = next(iter(keys))
        command = value[key]
        if command is not None:
            if set(value) - {key, 'done', 'findings'} or value.get('done', False) is not False or value.get('findings', []) != []:
                raise ValueError('command actions require done:false and findings:[]; unknown fields are refused')
            if not isinstance(command, str) or not command.strip() or len(command) > 8192 or '\x00' in command:
                raise ValueError('command must be a nonempty bounded string')
            if command.lstrip().startswith('{'):
                try:
                    nested = json.loads(command)
                except ValueError:
                    nested = None
                if isinstance(nested, dict):
                    if not unwrap or 'command' not in nested:
                        raise ValueError('command contains a serialized JSON action; put shell text directly in command')
                    action = normalize(nested, unwrap=False)
                    if set(action) != {'command'}:
                        raise ValueError('serialized command must contain a shell command, not completion')
                    if notes is not None:
                        notes.append('one serialized command level accepted')
                    return action
            return {'command': command}
        value = {k: v for k, v in value.items() if k != key}
    if set(value) != {'done', 'findings'} or value['done'] is not True:
        raise ValueError('expected command, or done:true and findings; unknown fields are refused')
    items = value['findings']
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError('findings must be an array of at most 100 entries')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != FIELDS:
            raise ValueError('invalid finding fields')
        if any(not isinstance(v, str) or len(v) > 4096 for v in item.values()):
            raise ValueError('finding fields must be bounded strings')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', item['id']) or item['id'] in seen:
            raise ValueError('finding ids must be unique and stable')
        if item['severity'] not in {'BLOCKER', 'MAJOR', 'MINOR'} or item['type'] not in {'DOCS', 'TOOL', 'PLATFORM'}:
            raise ValueError('invalid finding classification')
        seen.add(item['id'])
    return value


def claude_response(output: str, *, notes: list[str] | None = None) -> dict:
    # CLI diagnostics may surround the JSON envelope; only one result is accepted.
    decoder = json.JSONDecoder()
    try:
        envelopes = [decoder.decode(output)]
    except ValueError:
        envelopes = []
        for line in output.splitlines():
            if line.lstrip().startswith('{'):
                envelopes.append(decoder.decode(line))
    if len(envelopes) != 1 or not isinstance(envelopes[0], dict):
        raise ValueError('Claude returned no unique JSON result')
    value = envelopes[0]
    if value.get('is_error'):
        raise ValueError('Claude reported an error')
    if 'structured_output' in value:
        return normalize(value['structured_output'], notes=notes)
    if not isinstance(value.get('result'), str):
        raise TypeError('Claude returned no text/structured result')
    return normalize(json.loads(value['result']), notes=notes)
