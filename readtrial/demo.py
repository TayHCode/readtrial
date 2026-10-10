"""Offline learning loop with fake targets and agents; no subprocesses or network."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .process import Result
from .targets import Limits
from .trial import run_target


class DemoTarget:
    platform = 'Linux'
    network = 'offline fake target'
    isolation = 'FAKE: no repository code executes'

    def __init__(self, spec, limits, log, **kwargs):
        self.log = log
        self.fixed = spec == 'fake:fixed'

    def prepare(self, bundle):
        self.log('setup', 'create fake target', Result(0, 'fake target ready', 0))

    def command(self, command, **kwargs):
        if 'README.md' in command:
            result = Result(0, '1 # Example\n2 ## Quickstart\n3 ```sh\n4 example --help\n5 ```\n', 0)
        elif self.fixed:
            result = Result(0, 'usage: example --help\n', 0,
                            steps=[{'segment': 1, 'command': command, 'exit_code': 0}])
        else:
            result = Result(3, 'missing documented prerequisite\n', 0,
                            steps=[{'segment': 1, 'command': command, 'exit_code': 3}])
        self.log('command', command, result)
        return result

    def close(self):
        self.log('cleanup-confirmation', 'remove fake target', Result(0, 'fake target absent', 0))


class DemoAgent:
    def __init__(self, template, limits):
        pass

    def ask(self, context, timeout, directory):
        action: dict = {'command': 'example --help'}
        if context['history']:
            failed = context['history'][0]['exit_code'] != 0
            items = [{'id': 'missing-prerequisite', 'severity': 'MAJOR', 'type': 'DOCS',
                      'machine': context['machine'], 'command': 'example --help',
                      'expected': 'Documented help works', 'actual': 'missing documented prerequisite',
                      'doc_line': 'README.md:2'}] if failed else []
            action = {'done': True, 'findings': items}
        return action, Result(0, json.dumps(action), 0)


def run_demo(out: Path) -> int:
    out.mkdir(parents=True, mode=0o700)
    config: dict = {'repo': 'offline-example', 'agent_command': 'fake',
              'limits': asdict(Limits()), 'checks': [{'command': 'example --help', 'match': None}]}
    before = run_target(config, 'fake:broken', out / 'before', Path('unused'), 'offline-before',
                        target_factory=DemoTarget, agent_factory=DemoAgent)
    old = json.loads((out / 'before' / 'findings.json').read_text())
    after = run_target(config, 'fake:fixed', out / 'after', Path('unused'), 'offline-after',
                       target_factory=DemoTarget, agent_factory=DemoAgent, previous=old,
                       previous_records=[json.loads(line) for line in (out / 'before/commands.jsonl').read_text().splitlines()],
                       fixed_checks=config['checks'])
    (out / 'diff.json').write_text((out / 'after/diff.json').read_text(), encoding='utf-8')
    (out / 'run.json').write_text(json.dumps({'demo': True, 'results': [before, after]}, indent=2) + '\n', encoding='utf-8')
    print('OFFLINE DEMO: fake agent and fake target; no model, SSH, LXC or repository execution.')
    print(f"Before: {before['verdict']} (README step exit 3; declared check failed)")
    print(f"After: {after['verdict']} (README step exit 0; declared check passed)")
    print('diff.json: 1 resolved finding; evidence in before/ and after/.')
    return 0 if (before['verdict'], after['verdict']) == ('FAIL', 'PASS') else 2
