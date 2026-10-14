"""Audit regressions exercised through the real CLI and a committed product."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from readtrial.process import execute
from readtrial.shell import posix_script, statuses
from readtrial.verify import verify
from readtrial.verify_result import validate_result

ROOT = Path(__file__).parents[1]


class ProofIntegrityTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.project = self.directory / 'product'
        self.project.mkdir()
        (self.project / 'README.md').write_text('A committed cold-reader fixture.\n')
        (self.project / 'signal_exit.py').write_text(
            'import os, signal, sys\nos.kill(os.getpid(), getattr(signal, sys.argv[1]))\n')
        self.env = {**os.environ, 'TMPDIR': str(self.directory)}
        for argv in (['git', 'init', '-q'], ['git', 'add', 'README.md', 'signal_exit.py'],
                     ['git', '-c', 'user.name=Release Export', '-c', 'user.email=release-export',
                      '-c', 'core.hooksPath=/dev/null', 'commit', '-qm',
                      'Create fixture\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>']):
            subprocess.run(argv, cwd=self.project, env=self.env, capture_output=True, check=True)
        self.job = {'stage': 'verify', 'route': {'model': 'scripted-verifier'},
                    'source_hash': 'a' * 64, 'required_check_ids': ['one']}

    def run_product(self, command, expected=0, suffix='run'):
        items = self.directory / 'requirements.json'
        items.write_text(json.dumps([{'id': 'one', 'text': 'Exercise the declared command.'}]))
        plan = self.directory / 'plan.json'
        commands = command if isinstance(command, list) else [command]
        plan.write_text(json.dumps([
            {'recipe': 'not applicable: CLI'},
            *[{'requirement_id': 'one', 'command': item, 'expected_exit_code': expected} for item in commands],
            {'requirement_id': 'one', 'status': 'verified', 'observed': 'Actual command output.', 'reason': ''},
            {'done': True, 'not_checked': ['none']}]))
        self.out = self.directory / suffix
        argv = ['python3', '-m', 'readtrial', 'verify', str(self.project), '--requirements', str(items),
                '--scripted-plan', str(plan), '--model', 'scripted-verifier', '--builder-model', 'sample-builder',
                '--source-hash', self.job['source_hash'], '--trusted-local', '--independent-agent',
                # This budget includes Git preparation, not just the command.
                '--command-seconds', '10', '--out', str(self.out)]
        result = subprocess.run(argv, cwd=ROOT, env=self.env, capture_output=True, text=True)
        self.path = self.out / 'verify.json'
        self.report = json.loads(self.path.read_text())
        self.assertFalse(self.report['tool_problems'], result.stderr)
        return result.returncode

    def save(self):
        self.path.write_text(json.dumps(self.report))

    def test_venv_activation_has_command_proof_and_missing_activation_fails(self):
        commands = ['python3 -m venv .venv',
                    '. .venv/bin/activate && python -c "import sys; print(sys.prefix); assert sys.prefix != sys.base_prefix"']
        self.assertEqual(self.run_product(commands), 0)
        validate_result(self.path, self.job)
        self.assertEqual(self.run_product('. .venv/bin/activate', suffix='missing-venv'), 1)
        self.assertNotEqual(self.report['checks'][0]['step_results'][0]['exit_code'], 0)

    def test_security_document_fixture_is_writable(self):
        self.assertEqual(self.run_product("python3 -c \"from pathlib import Path; p=Path('SECURITY.md'); p.write_text('# Security\\n'); print(p.read_text())\""), 0)
        validate_result(self.path, self.job)

    def test_delta_finding_1_escaped_arguments_match_documented_shell(self):
        command = r'''python3 -c 'import sys; sys.exit(0 if sys.argv[1].startswith(chr(92)) else 7)' "\$value"'''
        direct = subprocess.run(['sh', '-c', command], env=self.env, capture_output=True)
        self.assertEqual(direct.returncode, 7)
        self.assertEqual(self.run_product(command), 1)
        self.assertEqual(self.report['checks'][0]['step_results'][0]['exit_code'], 7)
        validate_result(self.path, self.job)
        for index, argument in enumerate((r'"\$value"', r'"\`value\`"', r'"\"value\""', r'"prefix\value"')):
            with self.subTest(argument=argument):
                command = "python3 -c 'import sys; print(repr(sys.argv[1]))' " + argument
                direct = subprocess.run(['sh', '-c', command], env=self.env, capture_output=True, text=True)
                self.assertEqual(self.run_product(command, suffix='escape-' + str(index)), 0)
                proof = json.loads(Path(self.report['checks'][0]['artifact']).read_text())
                self.assertEqual(proof['commands'][0]['output'], direct.stdout)
                validate_result(self.path, self.job)

    def test_delta_findings_with_empty_home_minimal_path_and_slow_git(self):
        home, programs = self.directory / 'home', self.directory / 'bin'
        home.mkdir()
        programs.mkdir()
        (programs / 'python3').symlink_to(sys.executable)
        git = shutil.which('git')
        self.assertIsNotNone(git)
        wrapper = programs / 'git'
        wrapper.write_text(
            '#!' + sys.executable + '\n'
            'import os, sys, time\n'
            # Real Git operations still run. Reproduce preparation taking longer
            # than the old one-second fixture budget on a cold/busy machine.
            'if "clone" in sys.argv[1:]: time.sleep(1.1)\n'
            f'os.execv({git!r}, [{git!r}, *sys.argv[1:]])\n')
        wrapper.chmod(0o755)
        self.env = {'HOME': str(home), 'PATH': str(programs) + ':/usr/bin:/bin',
                    'TMPDIR': str(self.directory), 'LANG': 'C.UTF-8',
                    'PYTHONDONTWRITEBYTECODE': '1'}
        cases = [("python3 -c 'import sys; print(repr(sys.argv[1]))' " + r'"\$value"', "'$value'\n"),
                 (' printf " 42 " ', ' 42 ')]
        for index, (command, output) in enumerate(cases):
            with self.subTest(command=command):
                self.assertEqual(list(home.iterdir()), [])
                direct = subprocess.run(['sh', '-c', command], env=self.env, capture_output=True, text=True)
                self.assertEqual(direct.returncode, 0)
                self.assertEqual(direct.stdout, output)
                self.assertEqual(self.run_product(command, suffix='minimal-' + str(index)), 0)
                self.assertEqual(self.report['checks'][0]['steps'], [command])
                proof = json.loads(Path(self.report['checks'][0]['artifact']).read_text())
                self.assertEqual(proof['commands'][0]['output'], output)
                self.assertFalse(proof['commands'][0]['timed_out'])
                self.assertEqual(proof['commands'][0]['steps'][0]['exit_code'], 0)
                validate_result(self.path, self.job)

    def test_delta_finding_2_surrounding_whitespace_keeps_valid_evidence(self):
        for index, command in enumerate((' python3 -c "print(42)" ', '\tpython3 -c "print(42)"\t',
                                         ' exit 2 ', ' printf " 42 " ',
                                         ' printf "<%s>" value\\ ; printf ok ',
                                         " python3 -c 'import sys; print(repr(sys.argv[1]))' value\\  \t",
                                         " python3 -c 'import sys; print(repr(sys.argv[1]))' value\r",
                                         " python3 -c 'import sys; print(repr(sys.argv[1]))' value\u00a0")):
            with self.subTest(command=command):
                direct = subprocess.run(['sh', '-c', command], env=self.env, capture_output=True, text=True)
                self.assertEqual(self.run_product(command, direct.returncode, 'whitespace-' + str(index)), 0)
                self.assertEqual(self.report['checks'][0]['steps'], [command])
                proof = json.loads(Path(self.report['checks'][0]['artifact']).read_text())
                self.assertEqual(proof['commands'][0]['output'], direct.stdout)
                validate_result(self.path, self.job)

    def test_delta_finding_3_two_failed_runs_cannot_form_a_passing_record(self):
        from readtrial.process import Result
        reports = []
        job = {**self.job, 'required_check_ids': ['one', 'two']}
        items = [{'id': key, 'text': 'Exercise ' + key} for key in job['required_check_ids']]

        def measured_execute(*args, **kwargs):
            result = execute(*args, **kwargs)
            # Reproduce the audit's equal rounded durations deterministically;
            # product commands and all proof bytes remain real and unchanged.
            result.seconds = 0.001
            return result

        for successful in ('one', 'two'):
            plan = [{'recipe': 'not applicable: CLI'}]
            for item in items:
                key = item['id']
                plan.extend([{'requirement_id': key, 'command': 'printf ok', 'expected_exit_code': 0},
                             {'requirement_id': key, 'status': 'verified' if key == successful else 'unverified',
                              'observed': 'Observed ok.', 'reason': '' if key == successful else 'Not established.'}])
            plan.append({'done': True, 'not_checked': ['none']})
            agent = Mock()
            agent.codex = agent.claude = False
            agent.ask.side_effect = [(value, Result(0, json.dumps(value), 0)) for value in plan]
            out = self.directory / ('failed-' + successful)
            with patch('readtrial.verify.execute', side_effect=measured_execute):
                report, code = verify(self.project, items, out, model=job['route']['model'],
                                      builder_model='sample-builder', source_hash=job['source_hash'],
                                      agent_command='scripted-verifier', trusted_local=True, independent_agent=True,
                                      agent_factory=lambda *args, **kwargs: agent)
            self.assertEqual(code, 1)
            self.assertEqual(validate_result(out / 'verify.json', job), 2)
            reports.append(report)
        bindings = [json.loads(Path(report['checks'][0]['artifact']).read_text())['binding'] for report in reports]
        self.assertEqual(bindings[0]['command_set'], bindings[1]['command_set'])
        hybrid = copy.deepcopy(reports[0])
        hybrid['checks'] = [copy.deepcopy(reports[0]['checks'][0]), copy.deepcopy(reports[1]['checks'][1])]
        hybrid['not_checked'] = ['none']
        out = self.directory / 'hybrid'
        out.mkdir()
        for index, check in enumerate(hybrid['checks']):
            artifact = out / f'proof-{index}.json'
            artifact.write_bytes(Path(check['artifact']).read_bytes())
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), check['artifact_sha256'])
            check['artifact'] = str(artifact)
        path = out / 'verify.json'
        path.write_text(json.dumps(hybrid))
        with self.assertRaisesRegex(ValueError, 'different runs|proof binding differs: run_id'):
            validate_result(path, job)
        self.assertNotEqual(reports[0]['run_id'], reports[1]['run_id'])

    def test_finding_1_removing_detail_cannot_relabel_failure(self):
        self.assertEqual(self.run_product('python3 -c "import sys; sys.exit(3)"'), 1)
        check = self.report['checks'][0]
        check.pop('step_results')
        check.pop('proof_steps')
        check.update(status='verified', exit_code=0)
        check.pop('reason')
        self.save()
        with self.assertRaises(ValueError):
            validate_result(self.path, self.job)

    def test_finding_2_unchanged_proof_cannot_be_rebound_to_another_source(self):
        self.assertEqual(self.run_product('python3 -c "import sys; sys.exit(3)"', 3), 0)
        validate_result(self.path, self.job)
        self.report['source_hash'] = 'b' * 64
        self.save()
        with self.assertRaises(ValueError):
            validate_result(self.path, {**self.job, 'source_hash': 'b' * 64})

    def test_finding_3_missing_executable_is_not_expected_product_refusal(self):
        self.assertEqual(self.run_product('readtrial_missing_integrity_fixture', 127), 1)
        self.assertEqual(self.report['checks'][0]['status'], 'unverified')
        self.assertEqual(self.report['checks'][0]['step_results'][0]['execution_mode'], 'argv')
        self.assertEqual(self.report['checks'][0]['step_results'][0]['execution_error'], 'process spawn failed')
        validate_result(self.path, self.job)

    def test_finding_4_instrumentation_preserves_user_variables_and_failure(self):
        command = 'rt_code=7; python3 -c \'import sys; sys.exit(int(sys.argv[1]))\' "$rt_code"'
        direct = subprocess.run(['sh', '-c', command], env=self.env, capture_output=True)
        self.assertEqual(direct.returncode, 7)
        self.assertEqual(self.run_product(command), 1)
        self.assertEqual(self.report['checks'][0]['step_results'][0]['exit_code'], 7)

    def test_every_required_field_and_unknown_fields_fail_closed(self):
        self.assertEqual(self.run_product('true; printf ok'), 0)
        original = copy.deepcopy(self.report)
        proof_path = Path(original['checks'][0]['artifact'])
        original_proof = json.loads(proof_path.read_text())
        targets = [(), ('binding',), ('binding', 'requirements', 0), ('binding', 'command_set', 0),
                   ('requirement',), ('commands', 0), ('commands', 0, 'steps', 0),
                   ('observation',), ('lifecycle',), ('lifecycle', 'launch')]
        for target in targets:
            node = original_proof
            for key in target:
                node = node[key]
            for key in [*node, 'unknown_field']:
                with self.subTest(target=target, key=key):
                    proof = copy.deepcopy(original_proof)
                    node = proof
                    for part in target:
                        node = node[part]
                    if key == 'unknown_field':
                        node[key] = True
                    else:
                        node.pop(key)
                    proof_path.write_text(json.dumps(proof))
                    self.report = copy.deepcopy(original)
                    self.report['checks'][0]['artifact_sha256'] = hashlib.sha256(proof_path.read_bytes()).hexdigest()
                    self.save()
                    with self.assertRaises(ValueError):
                        validate_result(self.path, self.job)
        proof_path.write_text(json.dumps(original_proof))
        original['checks'][0]['artifact_sha256'] = hashlib.sha256(proof_path.read_bytes()).hexdigest()
        for target in [(), ('checks', 0), ('checks', 0, 'step_results', 0)]:
            node = original
            for key in target:
                node = node[key]
            for key in [*node, 'unknown_field']:
                with self.subTest(report_target=target, key=key):
                    self.report = copy.deepcopy(original)
                    node = self.report
                    for part in target:
                        node = node[part]
                    if key == 'unknown_field':
                        node[key] = True
                    else:
                        node.pop(key)
                    self.save()
                    with self.assertRaises(ValueError):
                        validate_result(self.path, self.job)

    def test_proof_cannot_be_empty_truncated_reordered_or_edited(self):
        self.assertEqual(self.run_product('true; printf ok'), 0)
        original = copy.deepcopy(self.report)
        path = Path(original['checks'][0]['artifact'])
        proof = json.loads(path.read_text())
        mutations = [lambda p: p.clear(), lambda p: p['commands'].clear(),
                     lambda p: p['commands'][0]['steps'].pop(),
                     lambda p: p['commands'][0]['steps'].reverse(),
                     lambda p: p['commands'][0]['steps'].append(p['commands'][0]['steps'][0]),
                     lambda p: p['commands'][0]['steps'][0].update(exit_code=True),
                     lambda p: p['binding']['command_set'].clear(),
                     lambda p: p['binding']['requirements'].clear(),
                     lambda p: p['binding']['command_set'][0]['commands_sha256'].append(True),
                     lambda p: p.update(proof_steps=[True]),
                     lambda p: p['observation'].update(proof_steps=[True]),
                     lambda p: p['commands'][0].update(execution_error=''),
                     lambda p: p['commands'][0].update(seconds=float('nan'))]
        for mutate in mutations:
            changed = copy.deepcopy(proof)
            mutate(changed)
            if changed == proof:
                continue
            path.write_text(json.dumps(changed))
            self.report = copy.deepcopy(original)
            self.report['checks'][0]['artifact_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.save()
            with self.assertRaises(ValueError):
                validate_result(self.path, self.job)
        # An edit without a refreshed digest must also fail.
        self.report = original
        self.save()
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            validate_result(self.path, self.job)

    def test_spawn_error_is_distinct_from_an_expected_127(self):
        with patch('readtrial.process.subprocess.Popen', side_effect=FileNotFoundError('fixture')):
            result = execute(['missing'], tempdir=str(self.directory))
        self.assertEqual(result.exit_code, 127)
        self.assertTrue(result.execution_error)

    def test_real_expected_nonzero_exits_including_reserved_codes_verify(self):
        for expected in (1, 3, 124, 125, 126, 127, 137, 143, 255):
            with self.subTest(expected=expected):
                command = 'python3 -c "import sys; sys.exit(' + str(expected) + ')"'
                self.assertEqual(self.run_product(command, expected, 'exit-' + str(expected)), 0)
                self.assertEqual(self.report['checks'][0]['step_results'][0]['exit_code'], expected)
                validate_result(self.path, self.job)

    def test_command_set_cannot_be_truncated_reordered_or_replaced(self):
        self.assertEqual(self.run_product(['printf first', 'printf second']), 0)
        original = copy.deepcopy(self.report)
        proof_path = Path(original['checks'][0]['artifact'])
        original_proof = json.loads(proof_path.read_text())
        for mode in ('truncate', 'reorder', 'edit'):
            with self.subTest(mode=mode):
                self.report = copy.deepcopy(original)
                proof = copy.deepcopy(original_proof)
                if mode == 'truncate':
                    proof['commands'].pop()
                elif mode == 'reorder':
                    proof['commands'].reverse()
                else:
                    proof['commands'][0].update(command='printf edited')
                    proof['commands'][0]['steps'][0].update(command='printf edited')
                from readtrial.verify import step_summary
                for index, row in enumerate(proof['commands'], 1):
                    row['step'] = index
                selected = list(range(1, len(proof['commands']) + 1))
                proof['proof_steps'] = proof['observation']['proof_steps'] = selected
                check = self.report['checks'][0]
                check.update(step_results=[step_summary(row) for row in proof['commands']],
                             steps=[row['command'] for row in proof['commands']], proof_steps=selected)
                proof_path.write_text(json.dumps(proof))
                check['artifact_sha256'] = hashlib.sha256(proof_path.read_bytes()).hexdigest()
                self.save()
                with self.assertRaisesRegex(ValueError, 'bound command'):
                    validate_result(self.path, self.job)

    def test_model_commit_and_run_labels_cannot_be_rebound(self):
        self.assertEqual(self.run_product('true'), 0)
        original = copy.deepcopy(self.report)
        for key, value in (('model', 'other-verifier'), ('source_commit', 'd' * 40), ('run_id', 'e' * 32)):
            with self.subTest(key=key):
                self.report = {**original, key: value}
                self.save()
                job = {**self.job, 'route': {'model': self.report['model']}}
                with self.assertRaisesRegex(ValueError, 'proof binding differs'):
                    validate_result(self.path, job)

    def test_killed_missing_nonexecutable_and_opaque_commands_do_not_verify(self):
        cases = [('python3 signal_exit.py SIGTERM', 143), ('python3 signal_exit.py SIGKILL', 137),
                 ('./README.md', 126), ('true || printf skipped', 0),
                 ('true; if readtrial_missing_integrity_fixture; then true; else true; fi', 0),
                 ('(readtrial_missing_integrity_fixture; true)', 0),
                 ("sh -c 'readtrial_missing_integrity_fixture'", 127),
                 ('env readtrial_missing_integrity_fixture', 127),
                 ("eval 'readtrial_missing_integrity_fixture; true'", 0),
                 ('runner=sh; "$runner" -c "readtrial_missing_integrity_fixture; true"', 0),
                 ('cat < readtrial_missing_integrity_file', 2),
                 ('timeout 0.05 python3 -c "import time; time.sleep(2)"', 124),
                 ('printf "%s" "$(readtrial_missing_integrity_fixture)"', 0)]
        for index, (command, expected) in enumerate(cases):
            with self.subTest(command=command):
                self.assertEqual(self.run_product(command, expected, 'case-' + str(index)), 1)
                self.assertTrue(self.report['checks'][0]['step_results'][0]['execution_error'])
                validate_result(self.path, self.job)

    def test_observer_preserves_variables_environment_and_shell_status(self):
        commands = ['rt_code=7; printf "%s\\n" "$rt_code"',
                    'readonly rt_code=7; printf "%s\\n" "$rt_code"',
                    'set -- one two; printf "%s %s\\n" "$1" "$2"',
                    'false; printf "%s\\n" "$?"',
                    'set -a; rt_code=7; python3 -c \'import os; print(os.environ["rt_code"])\'',
                    'false || printf recovered; printf "%s\\n" "$?"',
                    'trap \'printf exiting\' EXIT; printf done',
                    'printf "%s\\n" "$0"',
                    'set -e; false; printf unreachable']
        for command in commands:
            with self.subTest(command=command):
                status = self.directory / 'statuses'
                direct = subprocess.run(['sh', '-c', command], env=self.env, capture_output=True)
                observed = subprocess.run(['sh', '-c', posix_script(command, str(status))],
                                          env=self.env, capture_output=True)
                self.assertEqual((observed.returncode, observed.stdout, observed.stderr),
                                 (direct.returncode, direct.stdout, direct.stderr))
                status.unlink(missing_ok=True)

    def test_duplicate_json_keys_and_status_records_are_errors(self):
        self.assertEqual(self.run_product('true'), 0)
        self.path.write_text(self.path.read_text().replace('"checks":', '"model": "shadow", "checks":'))
        with self.assertRaisesRegex(ValueError, 'duplicate JSON'):
            validate_result(self.path, self.job)
        with self.assertRaisesRegex(ValueError, 'duplicate step-status'):
            statuses('1:0\n1:0\n', 'true')


if __name__ == '__main__':
    unittest.main()
