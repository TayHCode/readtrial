"""Read-only checks of the verify evidence wire format, never gate passage."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

from .verify import (command_digest, command_value, execution_problem, exit_expectations, nonempty, proof_supported, recipe_value,
                     requirements, STEP_FIELDS, step_summary)
from .shell import direct_argv, split


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def fields(value, required, optional=(), label='record'):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
            label + ': missing or unknown fields')


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate JSON field: ' + key)
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=unique)


def exit_code(value):
    return value is None or type(value) is int and -255 <= value <= 255


def process_detail(row):
    """Validate a complete controller result, including every shell segment."""
    require(exit_code(row['exit_code']), 'invalid actual exit code')
    require(isinstance(row['output'], str) and isinstance(row['denied'], str)
            and isinstance(row['execution_error'], str), 'invalid execution detail')
    require(row['execution_mode'] in ('argv', 'shell'), 'invalid execution mode')
    require(type(row['seconds']) in (int, float) and math.isfinite(row['seconds']) and row['seconds'] >= 0,
            'invalid duration')
    for key in ('timed_out', 'truncated'):
        require(type(row[key]) is bool, 'invalid ' + key)
    require(not row['timed_out'] or row['exit_code'] == 124, 'invalid timeout exit code')
    require(not row['truncated'] or row['exit_code'] == (124 if row['timed_out'] else 125),
            'invalid output-limit exit code')
    require(isinstance(row['steps'], list), 'missing shell segment detail')
    seen = []
    for segment in row['steps']:
        fields(segment, ('segment', 'command', 'exit_code'), label='shell segment')
        require(type(segment['segment']) is int and segment['segment'] > 0
                and segment['segment'] not in seen, 'invalid or duplicate shell segment')
        require(nonempty(segment['command']) and type(segment['exit_code']) is int
                and -255 <= segment['exit_code'] <= 255, 'invalid shell segment status')
        seen.append(segment['segment'])
    require(seen == sorted(seen), 'reordered shell segment detail')


def command_detail(row, index):
    fields(row, (*STEP_FIELDS, 'output', 'seconds', 'denied', 'steps'), label='command')
    process_detail(row)
    require(type(row['step']) is int and row['step'] == index and nonempty(row['command']),
            'invalid step identity')
    expected = exit_expectations(row['expect_exit'])
    require((type(row['expected_exit_code']) is int and row['expected_exit_code'] == expected[0])
            if len(expected) == 1 else row['expected_exit_code'] is None, 'invalid expected exit code')
    for key in ('expectation_matched', 'refused_by_policy', 'superseded'):
        require(type(row[key]) is bool, 'invalid ' + key)
    require(row['expectation_matched'] == (row['exit_code'] in expected),
            'incorrect expectation match')
    require(isinstance(row['superseded_reason'], str) and
            (nonempty(row['superseded_reason']) if row['superseded'] else row['superseded_reason'] == ''),
            'invalid superseded reason')
    require(row['refused_by_policy'] == bool(row['denied']) and
            (not row['refused_by_policy'] or row['exit_code'] == 126), 'invalid refusal detail')
    segments, _ = split(row['command'])
    require(row['execution_mode'] != 'argv' or direct_argv(row['command']) is not None,
            'command cannot execute as literal argv')
    for segment in row['steps']:
        require(segment['segment'] <= len(segments) and
                segment['command'] == segments[segment['segment'] - 1], 'segment differs from declared command')
    if not (row['denied'] or row['timed_out'] or row['truncated'] or row['execution_error']):
        require(not execution_problem(row['command'], row['exit_code'], row['steps'], direct=row['execution_mode'] == 'argv'),
                'command lacks completed execution proof')
        require(row['exit_code'] == row['steps'][-1]['exit_code'], 'exit code differs from executed segments')


def lifecycle_detail(value):
    fields(value, ('recipe_reused', 'saved_source_hash', 'source_changed', 'ready_attempts', 'cleanup', 'launch'),
           ('ready_attempt_count', 'saved_recipe_rejection'), 'lifecycle')
    require(type(value['recipe_reused']) is bool and type(value['source_changed']) is bool,
            'invalid lifecycle flags')
    require(value['saved_source_hash'] is None or isinstance(value['saved_source_hash'], str),
            'invalid saved fingerprint')
    require(isinstance(value['ready_attempts'], list), 'invalid readiness detail')
    if 'ready_attempt_count' in value:
        require(type(value['ready_attempt_count']) is int and value['ready_attempt_count'] >= len(value['ready_attempts']),
                'invalid readiness count')
    for row in value['ready_attempts'] + ([value['cleanup']] if value['cleanup'] is not None else []):
        fields(row, ('exit_code', 'output', 'seconds', 'timed_out', 'truncated', 'denied', 'steps', 'execution_error', 'execution_mode'),
               ('expect_exit', 'expectation_matched'), label='lifecycle command')
        if 'expect_exit' in row:
            exit_expectations(row['expect_exit'])
            require(type(row.get('expectation_matched')) is bool
                    and row['expectation_matched'] == (row['exit_code'] in row['expect_exit']),
                    'invalid lifecycle expectation match')
        process_detail(row)
    if value['launch'] is not None:
        fields(value['launch'], ('exit_code', 'output', 'truncated', 'stopped'), label='launch')
        require(exit_code(value['launch']['exit_code']) and isinstance(value['launch']['output'], str)
                and type(value['launch']['truncated']) is bool and type(value['launch']['stopped']) is bool,
                'invalid launch detail')
    if 'saved_recipe_rejection' in value:
        fields(value['saved_recipe_rejection'], ('recipe', 'validated', 'failure', 'source_hash'), label='recipe rejection')
        require(value['saved_recipe_rejection']['validated'] is False
                and nonempty(value['saved_recipe_rejection']['failure']), 'invalid recipe rejection')


def validate_result(path: Path, job: dict) -> int:
    """Validate job binding, complete coverage and private proof bytes.

    Honest unverified requirements are valid evidence; Factory owns acceptance.
    Hashes bind proof bytes, not an authenticated signature from an untrusted writer.
    """
    require(isinstance(job, dict) and job.get('stage') == 'verify', 'job must have stage verify')
    route = job.get('route')
    require(isinstance(route, dict) and nonempty(route.get('model')), 'job needs route.model')
    source_hash = job.get('source_hash')
    require(isinstance(source_hash, str) and re.fullmatch(r'[0-9a-fA-F]{64}', source_hash),
            'job needs a SHA256 source_hash')
    ids = job.get('required_check_ids')
    require(isinstance(ids, list) and ids and all(nonempty(item) for item in ids),
            'job needs nonempty required_check_ids')
    require(len(ids) == len(set(ids)), 'job has duplicate requirement IDs')
    path = path.resolve()
    if job.get('evidence_dir'):
        require(Path(job['evidence_dir']).resolve() in path.parents, 'result is outside job.evidence_dir')
    report = load_json(path)
    fields(report, ('run_id', 'model', 'source_hash', 'source_commit', 'recipe', 'checks', 'not_checked', 'tool_problems'), label='result')
    require(isinstance(report['run_id'], str) and re.fullmatch(r'[0-9a-f]{32}', report['run_id']), 'invalid run ID')
    require(report['model'] == route['model'], 'result model differs from job')
    require(report['source_hash'] == source_hash, 'result source_hash differs from job')
    require(report['source_commit'] is None or isinstance(report['source_commit'], str) and
            re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', report['source_commit']), 'invalid source commit')
    if 'source_commit' in job:
        require(report['source_commit'] == job['source_commit'], 'result source commit differs from job')
    recipe_value(report['recipe'])
    require(isinstance(report['not_checked'], list) and report['not_checked'] and
            all(nonempty(item) for item in report['not_checked']), 'not_checked must list gaps or none')
    require(isinstance(report['tool_problems'], list), 'tool_problems must be an array')
    for problem in report['tool_problems']:
        fields(problem, ('tool', 'problem'), label='tool problem')
        require(nonempty(problem['tool']) and nonempty(problem['problem']), 'invalid tool problem')
    require(isinstance(report['checks'], list), 'checks must be an array')
    found, bindings, executed_sets = [], [], []
    for check in report['checks']:
        fields(check, ('requirement_id', 'steps', 'observed', 'exit_code', 'artifact', 'artifact_sha256',
                       'status', 'step_results', 'proof_steps'), ('reason', 'blocked_by'), 'check')
        identifier = check['requirement_id']
        require(nonempty(identifier) and identifier in ids, 'check has an unknown requirement ID')
        require(identifier not in found, 'duplicate check for ' + identifier)
        found.append(identifier)
        require(isinstance(check['steps'], list) and check['steps'] and all(nonempty(s) for s in check['steps']),
                identifier + ': steps must be nonempty strings')
        require(nonempty(check['observed']), identifier + ': observed must be nonempty')
        require(check['status'] in ('verified', 'unverified'), identifier + ': invalid status')
        require(exit_code(check['exit_code']), identifier + ': invalid exit_code')
        if check['status'] == 'unverified':
            require(nonempty(check.get('reason')), identifier + ': unverified needs a reason')
        else:
            require('reason' not in check and 'blocked_by' not in check and not report['tool_problems']
                    and report['source_commit'] is not None, identifier + ': verified has runner failure or missing source')
        if 'blocked_by' in check:
            require(check['status'] == 'unverified' and check['blocked_by'] in ('external', 'sandbox'), identifier + ': invalid blocked_by')
        require(nonempty(check['artifact']), identifier + ': proof file is required')
        proof = Path(check['artifact'])
        if not proof.is_absolute():
            proof = path.parent / proof
        require(path.parent in proof.resolve().parents, identifier + ': proof is outside result directory')
        require(proof.is_file(), identifier + ': proof file is missing')
        digest = check['artifact_sha256']
        require(isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest), identifier + ': invalid proof SHA256')
        require(hashlib.sha256(proof.read_bytes()).hexdigest() == digest.lower(), identifier + ': proof SHA256 mismatch')
        rows, selected = check['step_results'], check['proof_steps']
        require(isinstance(rows, list) and isinstance(selected, list), identifier + ': invalid step detail')
        require(all(type(step) is int and 1 <= step <= len(rows) for step in selected)
                and selected == sorted(set(selected)), identifier + ': invalid proof steps')
        contents = load_json(proof)
        fields(contents, ('binding', 'requirement', 'commands', 'proof_steps', 'observation', 'lifecycle'), label='proof')
        commands = contents['commands']
        require(isinstance(commands, list) and len(commands) == len(rows), identifier + ': missing command detail in proof')
        for index, command in enumerate(commands, 1):
            command_detail(command, index)
        executed_sets.append({'requirement_id': identifier, 'commands_sha256': [command_digest(row) for row in commands]})
        require([step_summary(row) for row in commands] == rows, identifier + ': command detail differs from proof')
        # Python equality treats bool as int: validate summary types independently.
        for index, row in enumerate(rows, 1):
            fields(row, STEP_FIELDS, label='step summary')
            command_detail({**commands[index - 1], **row}, index)
        binding = contents['binding']
        fields(binding, ('run_id', 'model', 'source_hash', 'source_commit', 'recipe', 'requirements', 'command_set'), label='binding')
        for key in ('run_id', 'model', 'source_hash', 'source_commit', 'recipe'):
            require(binding[key] == report[key], identifier + ': proof binding differs: ' + key)
        requirements(binding['requirements'])
        require(len(binding['requirements']) == len(report['checks']), 'bound requirement coverage differs')
        require(contents['requirement'] == binding['requirements'][len(found) - 1]
                and contents['requirement']['id'] == identifier, identifier + ': requirement order or content differs')
        requirement = contents['requirement']
        if 'expect_exit' in requirement:
            require(all(row['expect_exit'] == requirement['expect_exit'] for row in commands),
                    identifier + ': command expectation differs from requirement')
        if 'not_verifiable_in_sandbox' in requirement:
            require(check['status'] == 'unverified' and check.get('blocked_by') == 'sandbox'
                    and check.get('reason') == requirement['not_verifiable_in_sandbox']
                    and not commands, identifier + ': sandbox limitation cannot claim product proof')
        bindings.append(binding)
        require(isinstance(contents['proof_steps'], list) and all(type(step) is int for step in contents['proof_steps'])
                and contents['proof_steps'] == selected, identifier + ': proof selection differs')
        observation = contents['observation']
        fields(observation, ('requirement_id', 'status', 'observed', 'reason', 'proof_steps'),
               ('superseded', 'blocked_by'), 'observation')
        require(observation['requirement_id'] == identifier
                and observation['status'] == check['status'] and observation['observed'] == check['observed']
                and isinstance(observation['reason'], str), identifier + ': observation differs from proof')
        require(isinstance(observation['proof_steps'], list) and all(type(step) is int for step in observation['proof_steps'])
                and observation['proof_steps'] == selected, identifier + ': observation proof steps differ')
        require(observation.get('blocked_by') == check.get('blocked_by'), identifier + ': external block differs')
        replacements = observation.get('superseded', [])
        require(isinstance(replacements, list), 'invalid supersession detail')
        for item in replacements:
            fields(item, ('step', 'reason'), label='supersession')
            require(type(item['step']) is int and 1 <= item['step'] <= len(rows) and nonempty(item['reason']),
                    'invalid supersession detail')
        require(replacements == [{'step': row['step'], 'reason': row['superseded_reason']} for row in rows if row['superseded']],
                identifier + ': supersession differs from proof')
        lifecycle_detail(contents['lifecycle'])
        require(check['exit_code'] == (0 if check['status'] == 'verified' else rows[-1]['exit_code'] if rows else None),
                identifier + ': exit code differs from proof outcome')
        require(check['steps'] == ([row['command'] for row in rows] if rows else
                                  ['Could not exercise the product: ' + check['reason']]), identifier + ': steps differ from commands')
        if check['status'] == 'verified':
            require(proof_supported(commands, selected), identifier + ': verified lacks matching active proof')
            require(observation.get('proof_steps') == selected, identifier + ': observation proof steps differ')
            lifecycle = contents['lifecycle']
            require(lifecycle['launch'] is not None and lifecycle['launch']['stopped']
                    and not lifecycle['launch']['truncated'], identifier + ': missing completed cleanup')
            if isinstance(report['recipe'], dict):
                for phase, record in (('ready_check', lifecycle['ready_attempts'][-1] if lifecycle['ready_attempts'] else None),
                                      ('cleanup', lifecycle['cleanup'])):
                    _, expected = command_value(report['recipe'][phase])
                    require(record is not None and record.get('expect_exit', [0]) == expected
                        and record['exit_code'] in expected and not any(
                        record[key] for key in ('denied', 'execution_error', 'timed_out', 'truncated'))
                        and record['steps'] and all(step['exit_code'] == 0 or step['exit_code'] in expected for step in record['steps']),
                        identifier + ': startup or cleanup failed')
        else:
            require(not selected, identifier + ': unverified cannot designate proof')
            require(observation['proof_steps'] == [], identifier + ': unverified observation designates proof')
            require(observation['reason'] == check['reason'], identifier + ': reason differs from proof')
    require(set(found) == set(ids), 'checks must cover every job requirement ID exactly once')
    for binding in bindings:
        require(isinstance(binding['command_set'], list) and len(binding['command_set']) == len(report['checks']),
                'invalid bound command set')
        for declared, check in zip(binding['command_set'], report['checks']):
            fields(declared, ('requirement_id', 'commands_sha256'), label='bound command set')
            require(isinstance(declared['commands_sha256'], list)
                    and len(declared['commands_sha256']) == len(check['step_results'])
                    and all(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)
                            for digest in declared['commands_sha256']),
                    'invalid bound command detail')
        require([item['id'] for item in binding['requirements']] == found, 'bound requirement coverage differs')
        require(binding['command_set'] == executed_sets, 'bound command set differs')
        require(binding == bindings[0], 'proofs belong to different runs')
    return len(found)
