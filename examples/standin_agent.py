"""Small reactive agent-command example, not a model or a list of actions.

Understands requirements of the form OP returns a result of NUMBER, or
OP exits with code NUMBER and output containing TEXT, and
README lines of the form '- OP: `command`'. Decisions use the supplied history;
the program never imports or reads product code, a plan or prewritten verdicts.
"""
import json
import re
import sys


def decide(context):
    if context['recipe'] is None:
        return {'recipe': 'not applicable: documented commands are short-lived arithmetic CLI calls'}
    history = context['history']
    completed = {row['agent_action']['requirement_id'] for row in history
                 if 'status' in row.get('agent_action', {})}
    commands = dict(re.findall(r'^- (\w+): `([^`]+)`$', context['README'], re.M))
    for item in context['requirements']:
        identifier = item['id']
        if identifier in completed:
            continue
        numeric = re.fullmatch(r'(\w+) returns a result of (-?\d+)\.', item['text'])
        outcome = re.fullmatch(r'(\w+) exits with code (\d+) and output containing (.+)\.', item['text'])
        expectation = numeric or outcome
        if expectation is None or expectation[1] not in commands:
            return {'requirement_id': identifier, 'status': 'unverified',
                    'observed': 'No supported documented command and expectation.',
                    'reason': 'This stand-in understands only the sample requirement vocabulary.'}
        records = [row for row in history if row.get('requirement_id') == identifier and 'output' in row]
        if not records:
            return {'requirement_id': identifier, 'command': commands[expectation[1]],
                    'expected_exit_code': int(outcome[2]) if outcome else 0}
        record = records[-1]
        expected = int(expectation[2])
        try:
            actual = json.loads(record['output'])['result']
        except (ValueError, KeyError, TypeError):
            actual = None
        expected_exit = int(outcome[2]) if outcome else 0
        matches = (record['exit_code'] == record.get('expected_exit_code', 0) == expected_exit
                   and not record['denied'] and not record.get('timed_out') and not record.get('truncated')
                   and (outcome[3] in record['output'] if outcome else type(actual) is int and actual == expected))
        observed = (f'Expected exit {expected_exit} and output containing {outcome[3]}; '
                    f'recorded exit {record["exit_code"]}, output {record["output"]}.' if outcome else
                    f'Expected result {expected}; recorded result {actual}; exit {record["exit_code"]}.')
        return {'requirement_id': identifier, 'status': 'verified' if matches else 'unverified',
                'observed': observed, 'proof_steps': [len(records)] if matches else [],
                'reason': '' if matches else 'Recorded output does not satisfy the requirement.'}
    return {'done': True, 'not_checked': ['Only the supplied numeric and exit/output examples; no general semantic judgement.']}


if __name__ == '__main__':
    context = json.loads(sys.stdin.read().split('UNTRUSTED TRIAL DATA:\n', 1)[1])
    print(json.dumps(decide(context)))
