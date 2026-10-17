"""Product journeys and an independently described Factory evidence contract."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from readtrial import codex_adapter
from readtrial.cli import main
from readtrial.process import Result, execute
from readtrial.protocol import claude_response
from readtrial.targets import Limits
from readtrial.trial import Agent, AgentError
from readtrial.verify import action, requirements, resolve_agent_command, verify

ROOT = Path(__file__).parents[1]
HASH = 'a' * 64
MODEL = 'scripted-verifier'
BUILDER = 'sample-builder'
RECIPE = json.loads((ROOT / 'examples/verify-plan.json').read_text())[0]['recipe']
ITEMS = json.loads((ROOT / 'examples/verify-requirements.json').read_text())
PLAN = json.loads((ROOT / 'examples/verify-plan.json').read_text())
READY_THEN_EXIT_1 = (
    "python3 -c \"import subprocess, sys, time; "
    "[time.sleep(.1) for _ in iter(lambda: subprocess.run([sys.executable, 'examples/verify_product/product.py', 'ready'], "
    "capture_output=True).returncode, 0)]; sys.exit(1)\"")


def assert_contract(case, report, directory, ids, model=MODEL, source_hash=HASH):
    """Wire-format assertions; no Factory implementation is imported or copied.

    The caller also checks its job route and current source fingerprint. Proof
    records are one per requested ID and stay inside the evidence boundary.
    """
    case.assertEqual(report['model'], model)
    case.assertEqual(report['source_hash'], source_hash)
    recipe = report['recipe']
    if isinstance(recipe, str):
        case.assertTrue(recipe.startswith('not applicable: '))
        case.assertTrue(recipe[len('not applicable: '):].strip())
    else:
        case.assertIsInstance(recipe, dict)
        for key in ('launch', 'ready_check', 'cleanup'):
            from readtrial.verify import command_value
            case.assertTrue(command_value(recipe[key])[0])
    case.assertIsInstance(report['not_checked'], list)
    case.assertTrue(report['not_checked'])
    for item in report['not_checked']:
        case.assertIsInstance(item, str)
        case.assertTrue(item.strip())
    case.assertIsInstance(report['tool_problems'], list)
    found = []
    for check in report['checks']:
        case.assertTrue({'requirement_id', 'steps', 'observed', 'exit_code', 'artifact',
                         'artifact_sha256', 'status'} <= check.keys())
        found.append(check['requirement_id'])
        case.assertIsInstance(check['steps'], list)
        case.assertTrue(check['steps'])
        case.assertTrue(all(isinstance(step, str) and step.strip() for step in check['steps']))
        case.assertIsInstance(check['observed'], str)
        case.assertTrue(check['observed'].strip())
        case.assertIn(check['status'], {'verified', 'unverified'})
        code = check['exit_code']
        case.assertTrue(code is None or type(code) is int)
        if check['status'] == 'unverified':
            case.assertTrue(isinstance(check['reason'], str) and check['reason'].strip())
        if 'blocked_by' in check:
            case.assertEqual(check['status'], 'unverified')
            case.assertIn(check['blocked_by'], ('external', 'sandbox'))
            case.assertTrue(check['reason'].strip())
        artifact = check['artifact']
        if artifact is None:
            case.assertIsNone(check['artifact_sha256'])
        else:
            path = Path(artifact)
            if not path.is_absolute():
                path = directory / path
            case.assertIn(directory.resolve(), path.resolve().parents)
            case.assertTrue(path.is_file())
            case.assertRegex(check['artifact_sha256'], r'^[a-fA-F0-9]{64}$')
            case.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), check['artifact_sha256'].lower())
    case.assertEqual(len(found), len(set(found)))
    case.assertEqual(set(found), set(ids))


class ScriptedAgent:
    def __init__(self, plan):
        self.plan = iter(plan)
        self.contexts = []

    def ask(self, context, timeout, directory):
        self.contexts.append(dict(context))
        value = next(self.plan)
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value, Result(0, json.dumps(value), 0)


@unittest.skipUnless(os.name == 'posix', 'trusted local verification uses POSIX process groups')
class VerifyTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'original'
        self.project.mkdir()
        shutil.copytree(ROOT / 'examples/verify_product', self.project / 'examples/verify_product')
        (self.project / 'README.md').write_text('Use the counter CLI. Do not edit its source.\n')
        self.out = self.root / 'evidence'
        self.agent = ScriptedAgent(PLAN)
        self.agent_kwargs = None

        def agent_factory(template, limits, **kwargs):
            self.agent_kwargs = kwargs
            return self.agent

        self.agent_factory = agent_factory
        self.addCleanup(patch.stopall)
        patch('readtrial.verify.snapshot', return_value=(self.root / 'unused.bundle', 'b' * 40)).start()

        def runner(argv, **kwargs):
            if argv[0] == 'git' and 'clone' in argv:
                shutil.copytree(self.project, Path(argv[-1]))
                return Result(0, 'fresh copy', 0)
            return execute(argv, **kwargs)

        patch('readtrial.verify.execute', side_effect=runner).start()

    def run_verify(self, plan=None, **kwargs):
        if plan is not None:
            self.agent = ScriptedAgent(plan)
        options = dict(model=MODEL, builder_model=BUILDER, source_hash=HASH,
                       agent_command='fake', trusted_local=True, independent_agent=True,
                       keep_running=True, agent_factory=self.agent_factory,
                       limits=Limits(command_seconds=2, total_seconds=15, max_steps=20))
        options.update(kwargs)
        items = options.pop('requirement_list', ITEMS)
        report, code = verify(self.project, items, self.out, **options)
        assert_contract(self, report, self.out, [item['id'] for item in items], options['model'], options['source_hash'])
        return report, code

    def proof(self, index=1):
        return json.loads((self.out / f'proof-{index:03}.json').read_text())

    def assert_stopped(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # A killed descendant may await the container init's reaper; it cannot run.
        state = Path('/proc') / str(pid) / 'stat'
        self.assertTrue(state.is_file())
        self.assertEqual(state.read_text().split(') ')[1].split()[0], 'Z')

    def test_declared_exit_lists_match_exactly_and_default_rejects_nonzero(self):
        from readtrial.verify_result import validate_result
        for index, (expected, actual, verdict) in enumerate((([1], 1, 0), ([2], 2, 0), ([1, 2], 2, 0),
                                                            ([1], 0, 1), ([1], 2, 1), (None, 1, 1))):
            with self.subTest(expected=expected, actual=actual):
                self.out = self.root / ('exits-' + str(index))
                command = {**PLAN[1], 'command': 'exit ' + str(actual)}
                if expected is not None:
                    command['expect_exit'] = expected
                report, code = self.run_verify([PLAN[0], command, *PLAN[2:]])
                self.assertEqual(code, verdict)
                row = report['checks'][0]['step_results'][0]
                self.assertEqual(row['expect_exit'], expected or [0])
                self.assertEqual(row['expectation_matched'], actual in (expected or [0]))
                job = {'stage': 'verify', 'route': {'model': MODEL}, 'source_hash': HASH,
                       'required_check_ids': [item['id'] for item in ITEMS]}
                self.assertEqual(validate_result(self.out / 'verify.json', job), 2)

    def test_requirement_expectation_is_inherited_and_cannot_be_overridden(self):
        items = [{**ITEMS[0], 'expect_exit': [1, 2]}, ITEMS[1]]
        report, code = self.run_verify([PLAN[0], {**PLAN[1], 'command': 'exit 2'}, *PLAN[2:]], requirement_list=items)
        self.assertEqual(code, 0)
        self.assertEqual(report['checks'][0]['step_results'][0]['expect_exit'], [1, 2])
        self.out = self.root / 'override'
        report, code = self.run_verify([PLAN[0], {**PLAN[1], 'command': 'exit 0', 'expect_exit': [0]}, *PLAN[2:]],
                                       requirement_list=items)
        self.assertEqual(code, 2)
        self.assertIn('differs from requirement', report['tool_problems'][0]['problem'])
        self.assertFalse(self.proof()['commands'])

    def test_recipe_readiness_and_cleanup_accept_declared_nonzero(self):
        from readtrial.verify_result import validate_result
        recipe = {**RECIPE, 'ready_check': {'command': READY_THEN_EXIT_1, 'expect_exit': [1]},
                  'cleanup': {'command': 'exit 2', 'expect_exit': [2]}}
        report, code = self.run_verify([{'recipe': recipe}, *PLAN[1:]])
        self.assertEqual(code, 0)
        self.assertEqual(self.proof()['lifecycle']['cleanup']['exit_code'], 2)
        job = {'stage': 'verify', 'route': {'model': MODEL}, 'source_hash': HASH,
               'required_check_ids': [item['id'] for item in ITEMS]}
        self.assertEqual(validate_result(self.out / 'verify.json', job), 2)
        self.out = self.root / 'reused'
        report, code = self.run_verify(PLAN[1:], recipe={'recipe': recipe, 'source_hash': HASH})
        self.assertEqual(code, 0)
        self.assertTrue(self.proof()['lifecycle']['recipe_reused'])

    def test_declared_sandbox_requirements_have_reasons_and_no_commands(self):
        from readtrial.verify_result import validate_result
        items = [{**item, 'not_verifiable_in_sandbox': reason} for item, reason in zip(ITEMS, (
            'Original reference is unavailable in the sandbox.', 'Private project records remain outside the sandbox.'))]
        report, code = self.run_verify([PLAN[0], PLAN[-1]], requirement_list=items)
        self.assertEqual(code, 1)
        self.assertFalse(report['tool_problems'])
        for check, item in zip(report['checks'], items):
            self.assertEqual(check['blocked_by'], 'sandbox')
            self.assertEqual(check['reason'], item['not_verifiable_in_sandbox'])
            self.assertEqual(check['step_results'], [])
            self.assertIsNone(check['exit_code'])
        job = {'stage': 'verify', 'route': {'model': MODEL}, 'source_hash': HASH,
               'required_check_ids': [item['id'] for item in ITEMS]}
        self.assertEqual(validate_result(self.out / 'verify.json', job), 2)

    def test_sandbox_limitations_stay_valid_when_the_runner_fails(self):
        from readtrial.verify_result import validate_result
        items = [{**item, 'not_verifiable_in_sandbox': 'Owner must inspect external records.'} for item in ITEMS]
        (self.project / 'README.md').write_text('x' * 65537)
        report, code = self.run_verify([PLAN[0], PLAN[-1]], requirement_list=items)
        self.assertEqual(code, 2)
        self.assertTrue(report['tool_problems'])
        for check in report['checks']:
            self.assertEqual(check['blocked_by'], 'sandbox')
            self.assertEqual(check['reason'], 'Owner must inspect external records.')
        job = {'stage': 'verify', 'route': {'model': MODEL}, 'source_hash': HASH,
               'required_check_ids': [item['id'] for item in items]}
        self.assertEqual(validate_result(self.out / 'verify.json', job), 2)

    def test_completed_launch_respects_expected_exit_and_keep_running(self):
        for index, (expected, keep, verdict) in enumerate((([1], False, 0), ([0], False, 1), ([1], True, 1))):
            with self.subTest(expected=expected, keep_running=keep):
                self.out = self.root / ('launch-' + str(index))
                recipe = {'launch': {'command': 'exit 1', 'expect_exit': expected},
                          'ready_check': 'true', 'cleanup': 'true'}
                plan = [{'recipe': recipe}, {**PLAN[1], 'command': 'true'}, PLAN[2],
                        {**PLAN[3], 'command': 'true'}, *PLAN[4:]]
                report, code = self.run_verify(plan, keep_running=keep)
                self.assertEqual(code, verdict)
                self.assertFalse(report['tool_problems'])

    def test_two_user_journeys_share_live_instance_and_state(self):
        report, code = self.run_verify()
        self.assertEqual(code, 0)
        self.assertEqual(report['not_checked'], PLAN[-1]['not_checked'])
        first = json.loads(self.proof(1)['commands'][0]['output'])
        second = json.loads(self.proof(2)['commands'][0]['output'])
        self.assertEqual(first['pid'], second['pid'])
        self.assertEqual((first['value'], second['value']), (3, 3))
        self.assert_stopped(first['pid'])
        self.assertFalse((self.out / '.state').exists())
        self.assertEqual((self.project / 'README.md').read_text(), 'Use the counter CLI. Do not edit its source.\n')
        self.assertFalse((self.project / '.git').exists())
        self.assertFalse(self.agent_kwargs['require_command'])

    def test_readtrial_sized_readme_fits_verifier_context(self):
        readme = (ROOT / 'README.md').read_text()
        self.assertGreater(len(readme.encode()), 32768)
        (self.project / 'README.md').write_text(readme)
        report, code = self.run_verify()
        self.assertEqual(code, 0)
        self.assertEqual(self.agent.contexts[0]['README'], readme)

    def test_oversized_readme_fails_honestly_and_cleans_state(self):
        (self.project / 'README.md').write_text('x' * 65537)
        report, code = self.run_verify()
        self.assertEqual(code, 2)
        self.assertIn('README exceeds', report['tool_problems'][0]['problem'])
        self.assertFalse((self.out / '.state').exists())

    def test_saved_factory_recipe_is_reused_and_readiness_rechecked_on_source_change(self):
        saved = {'recipe': RECIPE, 'source_hash': 'c' * 64, 'at': 'previous run'}
        report, code = self.run_verify(PLAN[1:], recipe=saved)
        self.assertEqual(code, 0)
        lifecycle = self.proof()['lifecycle']
        self.assertTrue(lifecycle['recipe_reused'])
        self.assertTrue(lifecycle['source_changed'])
        self.assertTrue(lifecycle['ready_attempts'])
        self.assertEqual(json.loads((self.out / 'recipe.json').read_text()), {'recipe': RECIPE, 'source_hash': HASH})
        self.assertEqual(self.agent.contexts[0]['recipe'], RECIPE)
        self.assertEqual(report['recipe'], RECIPE)

    def test_unverified_external_requirement_is_reported_without_fabricated_commands(self):
        plan = [PLAN[0], {'requirement_id': 'increment', 'status': 'unverified', 'observed': 'No external account.',
                         'reason': 'Real account access is unavailable.', 'blocked_by': 'external'}, *PLAN[3:]]
        report, code = self.run_verify(plan)
        self.assertEqual(code, 1)
        self.assertEqual(report['checks'][0]['blocked_by'], 'external')
        self.assertIsNone(report['checks'][0]['exit_code'])
        self.assertTrue(any('increment' in item for item in report['not_checked']))
        self.assertEqual(report['tool_problems'], [])

    def test_verified_claim_without_proof_is_downgraded(self):
        report, code = self.run_verify([PLAN[0], PLAN[2], *PLAN[3:]])
        self.assertEqual(code, 1)
        self.assertEqual(report['checks'][0]['status'], 'unverified')
        self.assertIn('proof is missing', report['checks'][0]['reason'])

    def test_expected_exits_and_honest_supersession_in_both_directions(self):
        from readtrial.verify_result import validate_result
        refusal = 'cat ~/.ssh/id_rsa'
        cases = [
            ('expected-nonzero', [('exit 2', 2)], [], True),
            ('unexpected-nonzero', [('exit 2', 0)], [], False),
            ('unexpected-zero', [('true', 2)], [], False),
            ('retry', [('exit 1', 0), ('true', 0)], [1], True),
            ('unmarked-retry', [('exit 1', 0), ('true', 0)], [], False),
            ('no-replacement', [('exit 1', 0)], [1], False),
            ('failed-replacement', [('exit 1', 0), ('exit 2', 0)], [1], False),
            ('final-failure', [('true', 0), ('exit 2', 0)], [2], False),
            ('refused-alone', [(refusal, 0)], [], False),
            ('expected-refusal', [(refusal, 126)], [], False),
            ('refused-retry', [(refusal, 0), ('true', 0)], [1], True),
            ('product-exit-126', [('exit 126', 126)], [], True),
            ('matching-but-unverified', [('exit 2', 2)], [], False),
        ]
        for name, commands, replaced, passes in cases:
            with self.subTest(name=name):
                self.out = self.root / name
                proposals = [{**PLAN[1], 'command': command, 'expected_exit_code': expected}
                             for command, expected in commands]
                observation = {**PLAN[2], 'superseded': [{'step': step, 'reason': 'Corrected attempt.'}
                                                         for step in replaced]}
                if name == 'matching-but-unverified':
                    observation.update(status='unverified', reason='Output did not show the requirement.')
                report, code = self.run_verify([PLAN[0], *proposals, observation, *PLAN[3:]])
                self.assertEqual(code, 0 if passes else 1)
                check = report['checks'][0]
                self.assertEqual(check['status'], 'verified' if passes else 'unverified')
                self.assertEqual(check['exit_code'], 0 if passes else 126 if commands[-1][0] == refusal else
                                 int(commands[-1][0].split()[1]) if commands[-1][0].startswith('exit ') else 0)
                self.assertEqual(len(check['steps']), len(commands))
                self.assertEqual(check['proof_steps'], [len(commands)] if passes else [])
                for step, row in enumerate(check['step_results'], 1):
                    self.assertEqual(row['expected_exit_code'], commands[step - 1][1])
                    self.assertEqual(row['expectation_matched'], row['exit_code'] == row['expected_exit_code'])
                    self.assertEqual(row['refused_by_policy'], commands[step - 1][0] == refusal)
                    self.assertEqual(row['superseded'], step in replaced)
                    self.assertEqual(row['superseded_reason'], 'Corrected attempt.' if step in replaced else '')
                self.assertEqual(self.proof()['proof_steps'], check['proof_steps'])
                job = {'stage': 'verify', 'route': {'model': MODEL}, 'source_hash': HASH,
                       'required_check_ids': [item['id'] for item in ITEMS]}
                self.assertEqual(validate_result(self.out / 'verify.json', job), len(ITEMS))

    def test_old_plan_defaults_to_zero_and_explicit_proof_selection(self):
        observation = {**PLAN[2], 'proof_steps': [2]}
        report, code = self.run_verify([PLAN[0], {**PLAN[1], 'command': 'true'}, PLAN[1], observation, *PLAN[3:]])
        self.assertEqual(code, 0)
        self.assertEqual(report['checks'][0]['proof_steps'], [2])
        self.assertTrue(all(row['expected_exit_code'] == 0 for row in report['checks'][0]['step_results']))

    def test_expectation_cannot_be_rewritten_in_observation(self):
        report, code = self.run_verify([PLAN[0], {**PLAN[1], 'command': 'exit 2'},
                                       {**PLAN[2], 'expected_exit_code': 2}])
        self.assertEqual(code, 2)
        row = self.proof()['commands'][0]
        self.assertEqual((row['expected_exit_code'], row['exit_code'], row['expectation_matched']), (0, 2, False))
        self.assertEqual(report['checks'][0]['status'], 'unverified')

    def test_invalid_proof_and_replacement_indices_are_protocol_faults(self):
        for detail in ({'proof_steps': [2]}, {'superseded': [{'step': 2, 'reason': 'Absent step.'}]}):
            self.out = self.root / ('proof-index' if 'proof_steps' in detail else 'superseded-index')
            report, code = self.run_verify([PLAN[0], PLAN[1], {**PLAN[2], **detail}])
            self.assertEqual(code, 2)
            self.assertIn('does not exist', report['tool_problems'][0]['problem'])

    def test_expected_timeout_and_output_cap_still_do_not_prove_behavior(self):
        for command, expected in [('python3 -c "import time; time.sleep(3)"', 124),
                                  ('python3 -c "print(10000 * chr(120))"', 125)]:
            self.out = self.root / str(expected)
            report, code = self.run_verify([PLAN[0], {**PLAN[1], 'command': command, 'expected_exit_code': expected},
                                           *PLAN[2:]], limits=Limits(command_seconds=1, output_bytes=1024, max_steps=20))
            self.assertEqual(code, 1)
            self.assertTrue(report['checks'][0]['step_results'][0]['expectation_matched'])
            self.assertEqual(report['checks'][0]['proof_steps'], [])

    def test_failed_compound_command_cannot_be_hidden_by_successful_echo_or_agent_claim(self):
        plan = [PLAN[0], {'requirement_id': 'increment', 'command': 'false; echo misleading-success'}, *PLAN[2:]]
        report, code = self.run_verify(plan)
        self.assertEqual(code, 1)
        self.assertEqual(report['checks'][0]['exit_code'], 0)
        self.assertEqual(self.proof()['commands'][0]['steps'][0]['exit_code'], 1)
        self.assertEqual(report['checks'][0]['status'], 'unverified')
        self.assertEqual(report['tool_problems'], [])

    def test_agent_failure_after_launch_stops_app_and_preserves_proof(self):
        failure = AgentError('fake provider unavailable', Result(2, 'failure', 0))
        report, code = self.run_verify([*PLAN[:2], failure])
        self.assertEqual(code, 2)
        self.assertTrue(report['tool_problems'])
        self.assertTrue(all(row['status'] == 'unverified' for row in report['checks']))
        self.assert_stopped(json.loads(self.proof()['commands'][0]['output'])['pid'])
        self.assertFalse((self.out / '.state').exists())

    def test_sigterm_runs_cleanup_writes_proof_and_restores_handlers(self):
        previous = signal.getsignal(signal.SIGTERM)
        report, code = self.run_verify([*PLAN[:2], lambda: os.kill(os.getpid(), signal.SIGTERM)])
        self.assertEqual(code, 2)
        self.assertIn('interrupted by signal', report['checks'][0]['reason'])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assert_stopped(json.loads(self.proof()['commands'][0]['output'])['pid'])
        self.assertFalse((self.out / '.state').exists())

    def test_total_time_budget_exhaustion_stops_app(self):
        plan = [PLAN[0], {**PLAN[1], 'command': 'python3 -c "import time; time.sleep(3)"'}, *PLAN[2:]]
        report, code = self.run_verify(plan, limits=Limits(total_seconds=1, command_seconds=2, max_steps=20))
        self.assertEqual(code, 2)
        self.assertIn('total time budget', report['checks'][0]['reason'])
        self.assertTrue(self.proof()['lifecycle']['launch']['stopped'])
        self.assertFalse((self.out / '.state').exists())

    def test_launch_output_cap_stops_app_and_reports_runner_problem(self):
        bad = {**RECIPE, 'launch': 'python3 -c "import time; print(chr(120) * 10000, flush=True); time.sleep(5)"',
               'ready_check': 'python3 --version'}
        report, code = self.run_verify([{'recipe': bad}, *PLAN[1:]],
                                       limits=Limits(command_seconds=2, output_bytes=1024, max_steps=20))
        self.assertEqual(code, 2)
        self.assertIn('launch output exceeded cap', report['checks'][0]['reason'])
        self.assertTrue(self.proof()['lifecycle']['launch']['truncated'])
        self.assertFalse((self.out / '.state').exists())

    def test_agent_finishes_early_records_all_missing_checks(self):
        report, code = self.run_verify([PLAN[0], PLAN[-1]])
        self.assertEqual(code, 2)
        self.assertEqual(len(report['checks']), 2)
        self.assertTrue(all(check['artifact'] for check in report['checks']))

    def test_duplicate_or_unknown_requirement_action_is_a_protocol_fault(self):
        for identifier in ('unknown', 'increment'):
            with self.subTest(identifier=identifier):
                self.out = self.root / identifier
                report, code = self.run_verify([*PLAN[:3], {**PLAN[1], 'requirement_id': identifier}])
                self.assertEqual(code, 2)
                self.assertIn('unfinished requirement', report['tool_problems'][0]['problem'])

    def test_command_timeout_cannot_verify_and_app_is_stopped(self):
        plan = [PLAN[0], {'requirement_id': 'increment', 'command': 'python3 -c "import time; time.sleep(3)"'}, *PLAN[2:]]
        report, code = self.run_verify(plan, limits=Limits(command_seconds=1, total_seconds=10, max_steps=20))
        self.assertEqual(code, 1)
        self.assertEqual(report['checks'][0]['exit_code'], 124)
        self.assertTrue(self.proof()['commands'][0]['timed_out'])
        self.assertTrue(self.proof()['lifecycle']['launch']['stopped'])

    def test_command_output_cap_is_recorded_and_prevents_verified_status(self):
        plan = [PLAN[0], {'requirement_id': 'increment', 'command': 'python3 -c "print(10000 * chr(120))"'}, *PLAN[2:]]
        report, code = self.run_verify(plan, limits=Limits(command_seconds=2, output_bytes=1024, max_steps=20))
        self.assertEqual(code, 1)
        self.assertEqual(report['checks'][0]['exit_code'], 125)
        self.assertTrue(self.proof()['commands'][0]['truncated'])

    def test_launch_failure_is_product_problem_and_cleanup_runs(self):
        bad = {**RECIPE, 'launch': 'false'}
        report, code = self.run_verify([{'recipe': bad}], limits=Limits(command_seconds=1))
        self.assertEqual(code, 1)
        self.assertEqual(report['tool_problems'], [])
        self.assertEqual(self.proof()['lifecycle']['cleanup']['exit_code'], 0)
        self.assertFalse((self.out / '.state').exists())

    def test_readiness_failure_stops_our_live_app(self):
        bad = {**RECIPE, 'ready_check': 'false'}
        report, code = self.run_verify([{'recipe': bad}], limits=Limits(command_seconds=1))
        self.assertEqual(code, 1)
        self.assertIn('readiness failed', report['checks'][0]['reason'])
        self.assertTrue(self.proof()['lifecycle']['launch']['stopped'])
        self.assertEqual(self.proof()['lifecycle']['cleanup']['exit_code'], 0)

    def test_cleanup_failure_invalidates_observations_and_keeps_proof(self):
        bad = {**RECIPE, 'cleanup': 'false'}
        report, code = self.run_verify([{'recipe': bad}, *PLAN[1:]])
        self.assertEqual(code, 2)
        self.assertTrue(all(check['status'] == 'unverified' for check in report['checks']))
        self.assertEqual(report['tool_problems'][0]['tool'], 'readtrial verify cleanup')
        self.assertTrue(self.proof()['lifecycle']['launch']['stopped'])

    def test_step_budget_exhaustion_cleans_up(self):
        report, code = self.run_verify(limits=Limits(command_seconds=2, max_steps=1))
        self.assertEqual(code, 2)
        self.assertIn('step budget', report['checks'][0]['reason'])
        self.assertTrue(self.proof()['lifecycle']['launch']['stopped'])

    def test_short_lived_cli_not_applicable_recipe_and_no_persistent_process(self):
        plan = [{'recipe': 'not applicable: short-lived CLI'},
                {**PLAN[1], 'command': 'python3 --version'}, PLAN[2],
                {**PLAN[3], 'command': 'python3 --version'}, PLAN[4], PLAN[5]]
        report, code = self.run_verify(plan, keep_running=False)
        self.assertEqual(code, 0)
        self.assertEqual(report['recipe'], 'not applicable: short-lived CLI')
        self.assertIsNone(self.proof()['lifecycle']['launch']['exit_code'])

    def test_isolated_home_tmp_and_no_inherited_secret(self):
        command = 'python3 -c "import os; print(os.environ.get(\'VERIFY_SENTINEL_KEY\', \'absent\')); print(os.environ[\'HOME\']); print(os.environ[\'TMPDIR\'])"'
        with patch.dict(os.environ, {'VERIFY_SENTINEL_KEY': 'private-test-sentinel'}):
            report, code = self.run_verify([PLAN[0], {**PLAN[1], 'command': command}, *PLAN[2:]])
        self.assertEqual(code, 0)
        text = self.proof()['commands'][0]['output']
        self.assertNotIn('private-test-sentinel', text)
        self.assertIn(str(self.out / '.state/home'), text)
        self.assertIn(str(self.out / '.state/tmp'), text)

    def test_visible_credential_command_is_refused(self):
        report, code = self.run_verify([PLAN[0], {**PLAN[1], 'command': 'cat ~/.ssh/id_rsa'}, *PLAN[2:]])
        self.assertEqual(code, 1)
        self.assertEqual(report['checks'][0]['exit_code'], 126)
        self.assertTrue(self.proof()['commands'][0]['denied'])

    def test_identity_attestations_and_exact_ids_are_checked_before_creating_output(self):
        for options in ({'model': BUILDER}, {'trusted_local': False}, {'independent_agent': False},
                        {'source_hash': 'invalid'}, {'required_check_ids': ['increment']},
                        {'required_check_ids': ['increment', 'missing']}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_verify(**options)
            self.assertFalse(self.out.exists())

    def test_evidence_is_private_and_existing_output_is_preserved(self):
        self.run_verify()
        for path in self.out.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        before = (self.out / 'verify.json').read_bytes()
        with self.assertRaises(ValueError):
            self.run_verify()
        self.assertEqual((self.out / 'verify.json').read_bytes(), before)

    def test_scripted_agent_subprocess_uses_same_protocol(self):
        patch.stopall()
        # Only source setup is faked; agent and product subprocesses are real.
        patch('readtrial.verify.snapshot', return_value=(self.root / 'unused.bundle', 'b' * 40)).start()
        def runner(argv, **kwargs):
            if argv[0] == 'git':
                shutil.copytree(self.project, Path(argv[-1]))
                return Result(0, '', 0)
            return execute(argv, **kwargs)
        patch('readtrial.verify.execute', side_effect=runner).start()
        import shlex
        command = shlex.join([sys.executable, str(ROOT / 'examples/verify_agent.py'), str(ROOT / 'examples/verify-plan.json')])
        report, code = self.run_verify(agent_command=command, agent_factory=Agent)
        self.assertEqual(code, 0)
        self.assertEqual(report['tool_problems'], [])


    def test_custom_agent_phase_correction_runs_only_after_recipe(self):
        report, code = self.run_verify([PLAN[1], *PLAN])
        self.assertEqual(code, 0)
        self.assertIn('commands cannot run before recipe selection',
                      self.agent.contexts[1]['protocol_correction'])
        self.assertEqual(len(self.proof()['commands']), 1)

    def test_recipe_after_selection_is_corrected(self):
        report, code = self.run_verify([PLAN[0], PLAN[0], *PLAN[1:]])
        self.assertEqual(code, 0)
        self.assertIn('recipe already selected', self.agent.contexts[2]['protocol_correction'])

    def test_next_action_guides_commands_observations_then_completion(self):
        report, code = self.run_verify()
        self.assertEqual(code, 0)
        self.assertIn('Propose a product command', self.agent.contexts[1]['next_action'])
        self.assertIn('Report status, observed and reason', self.agent.contexts[2]['next_action'])
        self.assertIn('Every requirement has an observation', self.agent.contexts[-1]['next_action'])

    def test_executed_command_retains_quotes_backslashes_and_compound_segments(self):
        command = 'python3 -c \'print("quoted value")\'; printf "hello\\n"; printf \\"literal\\"'
        plan = [PLAN[0], {**PLAN[1], 'command': command}, *PLAN[2:]]
        report, code = self.run_verify(plan)
        self.assertEqual(code, 0)
        row = self.proof()['commands'][0]
        self.assertEqual(row['command'], command)
        self.assertEqual(row['output'], 'quoted value\nhello\n"literal"')
        self.assertEqual(row['steps'][1]['command'], 'printf "hello\\n"')

    def test_failed_saved_recipe_is_not_revalidated_or_rebased(self):
        saved = {'recipe': {**PLAN[0]['recipe'], 'ready_check': 'false'}, 'source_hash': 'c' * 64}
        report, code = self.run_verify([], recipe=saved, limits=Limits(command_seconds=.2))
        self.assertEqual(code, 1)
        failed = json.loads((self.out / 'recipe.json').read_text())
        self.assertFalse(failed['validated'])
        self.assertEqual(failed['source_hash'], saved['source_hash'])
        self.assertIn('readiness failed', failed['failure'])
        self.out = self.root / 'next'
        report, code = self.run_verify(PLAN, recipe=failed)
        self.assertEqual(code, 0)
        rejected = json.loads((self.out / 'unusable-recipe.json').read_text())
        self.assertFalse(rejected['validated'])
        self.assertEqual(rejected['source_hash'], saved['source_hash'])
        self.assertIn('previously marked invalid', rejected['failure'])
        self.assertIsNone(self.agent.contexts[0]['recipe'])

    def assert_saved_recipe_fallback(self, recipe, reason):
        saved = {'recipe': recipe, 'source_hash': 'c' * 64}
        with patch('readtrial.verify.LocalProduct.launch', autospec=True) as launch:
            # No rejected launch may run; the replacement here is a CLI recipe.
            plan = [{'recipe': 'not applicable: fresh CLI recipe'},
                    {**PLAN[1], 'command': 'python3 --version'}, PLAN[2],
                    {**PLAN[3], 'command': 'python3 --version'}, *PLAN[4:]]
            report, code = self.run_verify(plan, recipe=saved, keep_running=False)
            launch.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(report['tool_problems'], [])
        self.assertIsNone(self.proof()['lifecycle']['cleanup'])
        rejection = self.proof()['lifecycle']['saved_recipe_rejection']
        self.assertEqual(rejection['recipe'], recipe)
        self.assertFalse(rejection['validated'])
        self.assertIn(reason, rejection['failure'])
        self.assertFalse(self.proof()['lifecycle']['recipe_reused'])
        self.assertIsNone(self.agent.contexts[0]['recipe'])
        self.assertIn(reason, self.agent.contexts[0]['history'][0]['saved_recipe_unusable'])
        self.assertEqual(json.loads((self.out / 'unusable-recipe.json').read_text()), rejection)
        self.assertEqual((self.out / 'unusable-recipe.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.out / 'recipe.json').read_text())['recipe'], plan[0]['recipe'])
        self.assertFalse((self.out / '.state').exists())
        self.out = self.root / (self.out.name + '-replacement')
        report, code = self.run_verify(PLAN, recipe=saved)
        self.assertEqual(code, 0)
        self.assertEqual(report['tool_problems'], [])
        self.assertEqual(json.loads((self.out / 'recipe.json').read_text()), {'recipe': RECIPE, 'source_hash': HASH})

    def test_saved_prose_recipe_gets_fresh_recipe(self):
        self.assert_saved_recipe_fallback('Start the product, wait until ready, then stop it.', 'not-applicable reason')

    def test_saved_null_recipe_wrapper_gets_fresh_recipe(self):
        self.assert_saved_recipe_fallback(None, 'needs launch, ready_check and cleanup')

    def test_saved_incomplete_recipe_gets_fresh_recipe(self):
        self.assert_saved_recipe_fallback({'launch': 'false'}, 'needs launch, ready_check and cleanup')

    def test_saved_prose_after_assignment_gets_fresh_recipe(self):
        self.assert_saved_recipe_fallback({**RECIPE, 'cleanup': 'MODE=trial Stop the product.'},
                                          'command unavailable: Stop')

    def test_saved_prose_command_recipe_gets_fresh_recipe(self):
        self.assert_saved_recipe_fallback({'launch': 'Start the product.', 'ready_check': 'Wait until ready.',
                                           'cleanup': 'Stop the product.'}, 'command unavailable: Start')

    def test_saved_prose_readiness_or_cleanup_gets_fresh_recipe_before_launch(self):
        for phase, text in (('ready_check', 'Wait until ready.'), ('cleanup', 'Stop the product.')):
            with self.subTest(phase=phase):
                self.out = self.root / (phase + '-prose')
                self.assert_saved_recipe_fallback({**RECIPE, phase: text}, 'command unavailable')

    def test_saved_refused_readiness_or_cleanup_gets_fresh_recipe_before_launch(self):
        for phase in ('launch', 'ready_check', 'cleanup'):
            with self.subTest(phase=phase):
                self.out = self.root / (phase + '-out')
                self.assert_saved_recipe_fallback({**RECIPE, phase: 'cat /outside-workspace'}, 'refused')

    def test_saved_shell_syntax_error_gets_fresh_recipe_before_launch(self):
        for phase in ('launch', 'ready_check', 'cleanup'):
            with self.subTest(phase=phase):
                self.out = self.root / (phase + '-syntax')
                self.assert_saved_recipe_fallback({**RECIPE, phase: 'if then'}, 'shell syntax invalid')

    def test_saved_unbalanced_shell_gets_fresh_recipe_before_launch(self):
        for phase in ('launch', 'ready_check', 'cleanup'):
            with self.subTest(phase=phase):
                self.out = self.root / (phase + '-unbalanced')
                self.assert_saved_recipe_fallback({**RECIPE, phase: "printf 'unterminated"}, 'refused')

    def test_saved_recipe_cleanup_never_runs_before_source_copy_succeeds(self):
        with patch('readtrial.verify.execute', return_value=Result(2, 'copy failed', 0)), \
                patch('readtrial.verify.LocalProduct.command', autospec=True) as command:
            report, code = self.run_verify(recipe={'recipe': RECIPE, 'source_hash': 'c' * 64})
            command.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn('local source copy failed', report['tool_problems'][0]['problem'])
        self.assertIsNone(self.proof()['lifecycle']['cleanup'])

    def test_actual_cli_error_is_retained_in_saved_verify_result(self):
        # Product setup is local; only the failing agent CLI is a stand-in.
        raw = json.dumps({'is_error': True, 'result': 'API Error: 400 schema rejected'})
        with patch('readtrial.trial.execute', return_value=Result(1, raw, 0)):
            report, code = self.run_verify(agent_factory=Agent, agent_command='offline-agent')
        self.assertEqual(code, 2)
        stored = (self.out / 'verify.json').read_text()
        self.assertIn('API Error: 400 schema rejected', stored)
        self.assertNotIn('[response omitted]', stored)


class VerifyProtocolTests(unittest.TestCase):
    def test_exit_lists_reject_ambiguous_or_invalid_declarations(self):
        for value in ([], [True], [-1], [256], [1, 1], '1', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                action({**PLAN[1], 'expect_exit': value})
            with self.subTest(requirement=value), self.assertRaises(ValueError):
                requirements([{'id': 'one', 'text': 'Command.', 'expect_exit': value}])
        with self.assertRaises(ValueError):
            action({**PLAN[1], 'expect_exit': [1], 'expected_exit_code': 1})
        with self.assertRaises(ValueError):
            action({**PLAN[2], 'expect_exit': [1]})
        with self.assertRaises(ValueError):
            requirements([{'id': 'one', 'text': 'Command.', 'not_verifiable_in_sandbox': ''}])

    def test_vault_product_names_are_allowed_and_credential_access_is_refused(self):
        from readtrial.trusted import inspect
        self.assertFalse(inspect("python3 vault_kit.py --username '' --transcripts examples/transcripts --output ./QuickstartVault", set()))
        self.assertFalse(inspect('python3 vault_kit.py search hello --vault ./QuickstartVault', set()))
        self.assertTrue(inspect('vault kv get secret/private', set()))
        self.assertTrue(inspect('cat .vault-token', set()))

    def test_new_action_fields_are_strict_and_expectations_are_command_only(self):
        for expected in (True, None, '2', -1, 256):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                action({**PLAN[1], 'expected_exit_code': expected})
        for detail in ({'expected_exit_code': 2}, {'proof_steps': [True]}, {'proof_steps': [1, 1]},
                       {'proof_steps': [0]}, {'superseded': {}}, {'superseded': [{'step': 1, 'reason': ''}]},
                       {'superseded': [{'step': True, 'reason': 'Reason.'}]},
                       {'superseded': [{'step': 1, 'reason': 'Reason.', 'expected_exit_code': 2}]},
                       {'superseded': [{'step': 1, 'reason': 'One.'}, {'step': 1, 'reason': 'Two.'}]}):
            with self.subTest(detail=detail), self.assertRaises(ValueError):
                action({**PLAN[2], **detail})

    def test_relative_agent_program_and_python_script_resolve_within_trusted_roots(self):
        import shlex
        script = 'examples/standin_agent.py'
        resolved = resolve_agent_command('python3 ' + script, ROOT)
        self.assertEqual(shlex.split(resolved), ['python3', str((ROOT / script).resolve())])
        resolved = resolve_agent_command('./' + script + ' "data argument"', ROOT)
        self.assertEqual(shlex.split(resolved), [str((ROOT / script).resolve()), 'data argument'])
        self.assertEqual(shlex.split(resolve_agent_command('python3 -m module', ROOT)), ['python3', '-m', 'module'])

    def test_relative_agent_paths_cannot_escape_trusted_roots_through_parent_or_symlink(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            project = root / 'project'
            project.mkdir()
            outside = root / 'outside.py'
            outside.write_text('raise RuntimeError("must never run")')
            (project / 'link.py').symlink_to(outside)
            previous = Path.cwd()
            try:
                os.chdir(project)
                for path in ('../outside.py', 'link.py'):
                    with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'outside trusted'):
                        resolve_agent_command('python3 ' + path, project)
            finally:
                os.chdir(previous)

    def test_starting_directory_agent_path_preserves_spaces_and_literal_arguments(self):
        import shlex
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            script = root / 'agent script.py'
            script.write_text('pass')
            previous = Path.cwd()
            try:
                os.chdir(root)
                command = shlex.join(['python3', script.name, 'literal $value', 'two words'])
                self.assertEqual(shlex.split(resolve_agent_command(command, ROOT)),
                                 ['python3', str(script), 'literal $value', 'two words'])
                with self.assertRaisesRegex(ValueError, 'not found'):
                    resolve_agent_command('python3 missing.py', root)
            finally:
                os.chdir(previous)

    def test_provider_envelopes_parse_verify_actions(self):
        value = PLAN[0]
        claude = json.dumps({'structured_output': value})
        codex = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(value)}})
        self.assertEqual(claude_response(claude, normalizer=action), value)
        self.assertEqual(codex_adapter.response(codex, normalizer=action), value)

    def test_actions_reject_extra_fields_invalid_external_blocks_and_empty_explanations(self):
        for value in ({'recipe': 'not applicable: '}, {**PLAN[1], 'native_tool': True},
                      {**PLAN[2], 'blocked_by': 'external'}, {**PLAN[2], 'status': 'unverified'},
                      {'done': True, 'not_checked': []}, {**PLAN[1], 'command': ''}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                action(value)

    def test_requirement_ids_and_text_are_validated(self):
        for value in ([], {}, [{'id': 'x', 'text': ''}], [ITEMS[0], ITEMS[0]]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                requirements(value)

    def test_claude_configuration_uses_verify_brief_and_schema_without_tools(self):
        from readtrial.verify import BRIEF, SCHEMA
        agent = Agent('claude -p --model independent', Limits(), brief=BRIEF, schema=SCHEMA,
                      normalizer=action, require_command=False)
        self.assertEqual(agent.argv[agent.argv.index('--system-prompt') + 1], BRIEF)
        wire_schema = json.loads(agent.argv[agent.argv.index('--json-schema') + 1])
        self.assertEqual(wire_schema['type'], 'object')
        self.assertEqual(wire_schema['required'], ['action'])
        self.assertEqual(wire_schema['properties']['action'], SCHEMA)
        self.assertEqual(agent.argv[agent.argv.index('--tools') + 1], '')
        self.assertIn('--no-session-persistence', agent.argv)

    def test_native_model_mismatch_is_refused_before_output(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            for command in ('codex exec -m other-model', 'claude -p --model other-model'):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    verify(ROOT, ITEMS, Path(folder) / 'out', model=MODEL, builder_model=BUILDER,
                           source_hash=HASH, agent_command=command, trusted_local=True, independent_agent=True)
                self.assertFalse((Path(folder) / 'out').exists())

    def test_job_overrides_and_output_boundary_are_validated_by_cli(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            job = root / 'job.json'
            job.write_text(json.dumps({'stage': 'verify', 'route': {'model': MODEL}, 'source_hash': HASH,
                                       'evidence_dir': str(root / 'private')}))
            args = ['verify', str(ROOT), '--requirements', str(ROOT / 'examples/verify-requirements.json'),
                    '--job', str(job), '--builder-model', BUILDER, '--agent-command', 'fake',
                    '--trusted-local', '--independent-agent', '--out', str(root / 'outside')]
            with self.assertRaises(SystemExit) as error:
                main(args)
            self.assertEqual(error.exception.code, 2)
            with self.assertRaises(SystemExit) as error:
                main([*args, '--model', 'wrong'])
            self.assertEqual(error.exception.code, 2)

    def test_cli_preserves_job_model_hash_and_adds_regression_smoke(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            job = root / 'job.json'
            job.write_text(json.dumps({'stage': 'verify', 'route': {'model': MODEL}, 'source_hash': HASH,
                                       'required_check_ids': ['increment', 'retain', 'regression-smoke']}))
            with patch('readtrial.verify.verify', return_value=({'checks': []}, 0)) as call:
                self.assertEqual(main(['verify', str(ROOT), '--requirements', str(ROOT / 'examples/verify-requirements.json'),
                                       '--job', str(job), '--builder-model', BUILDER, '--agent-command', 'fake',
                                       '--out', str(root / 'out')]), 0)
            self.assertEqual(call.call_args.kwargs['model'], MODEL)
            self.assertEqual(call.call_args.kwargs['source_hash'], HASH)
            self.assertEqual(call.call_args.args[1][-1]['id'], 'regression-smoke')

    def test_contract_rejects_tampered_proof_hash(self):
        scratch = ROOT / '.scratch'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            proof = root / 'proof.json'
            proof.write_text('{}')
            report = {'model': MODEL, 'source_hash': HASH, 'recipe': 'not applicable: CLI',
                      'not_checked': ['none'], 'tool_problems': [],
                      'checks': [{'requirement_id': 'one', 'steps': ['invoke product'], 'observed': 'ok',
                                  'exit_code': 0, 'artifact': str(proof), 'artifact_sha256': '0' * 64,
                                  'status': 'verified'}]}
            with self.assertRaises(AssertionError):
                assert_contract(self, report, root, ['one'])
