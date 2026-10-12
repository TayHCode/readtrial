"""Validate documented invocations against the real parser without executing them."""
import contextlib
import io
import re
import shlex
import unittest
from pathlib import Path
from unittest.mock import patch

from readtrial.cli import parser
from readtrial import setup


def documented_argv(text):
    """Yield line numbers and argv from executable README shell fences."""
    shell = False
    pending = ''
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith('```'):
            shell = not shell and line[3:].strip() in {
                'sh', 'bash', 'shell', 'console', 'zsh', 'powershell', 'ps1'}
            continue
        if not shell or not line.strip() or line.lstrip().startswith('#'):
            continue
        if not pending:
            start = number
        pending += line.rstrip().removesuffix('\\') + ' '
        if line.rstrip().endswith('\\'):
            continue
        words = shlex.split(pending, comments=True)
        pending = ''
        for index, word in enumerate(words):
            if word == 'readtrial' and index and words[index - 1] == '-m':
                yield start, words[index + 1:]
                break
            if index == 0 and re.fullmatch(r'(?:[^\s]*/)?readtrial(?:\.exe)?', word):
                yield start, words[index + 1:]
                break


def validate(argv):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            return parser().parse_args(argv)
        except SystemExit as error:
            if error.code:
                raise ValueError('Invalid documented CLI arguments: ' + repr(argv)) from error


class ReadmeCLITests(unittest.TestCase):
    def test_doctor_mac_setup_blocks_match_readme_and_request_public_key_input(self):
        from readtrial.account import provisioning_steps
        reference = provisioning_steps('trialuser')
        readme = (Path(__file__).parents[1] / 'README.md').read_text()
        section = readme.split('## Dedicated macOS test account\n', 1)[1]
        blocks = re.findall(r'```sh\n(.*?)```', reference, re.S)
        self.assertGreaterEqual(len(blocks), 5)
        for block in blocks:
            self.assertIn(block, section)
        self.assertIn('read -r TRIAL_PUBLIC_KEY', reference)
        self.assertIn('TRIAL_HOME=', reference)

    def test_every_fenced_readtrial_command_uses_real_subcommands_and_flags(self):
        readme = Path(__file__).parents[1] / 'README.md'
        commands = list(documented_argv(readme.read_text()))
        self.assertGreaterEqual(len(commands), 20)
        modes = set()
        for line, argv in commands:
            with self.subTest(line=line, argv=argv):
                args = validate(argv)
                if args is not None:
                    modes.add(args.mode)
        self.assertTrue({'demo', 'doctor', 'setup-linux', 'run', 'rerun', 'leftovers'} <= modes)

    def test_contract_rejects_unknown_flags_modes_and_missing_values(self):
        for argv in (['invented'], ['doctor', '--invented'], ['demo', '--out'],
                     ['run', '.', '--target', 'local-container', '--agent-command', 'fake']):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                validate(argv)

    def test_shell_fences_keep_quoted_agent_and_check_arguments_intact(self):
        text = '''```sh
uv run --no-project --python 3.12 python -m readtrial run . \\
  --target lxc:ubuntu:24.04 --agent-command 'fake --model sample' \\
  --check 'python3 -m readtrial --help' --out trial-results/example
```
```json
{"command": "readtrial made-up"}
```
'''
        commands = list(documented_argv(text))
        self.assertEqual(len(commands), 1)
        args = validate(commands[0][1])
        self.assertEqual(args.agent_command, 'fake --model sample')
        self.assertEqual(args.check, ['python3 -m readtrial --help'])

    def test_windows_doctor_never_claims_sandbox_support_or_queries_features(self):
        with patch('readtrial.setup.execute') as execute:
            report = setup.doctor('Windows')
        execute.assert_not_called()
        self.assertFalse(report['ready'])
        wording = ' '.join(row['check'] + ' ' + row['fix'] for row in report['checks'])
        self.assertIn('Windows Sandbox is not supported', wording)
        self.assertIn('Linux VM', wording)
        self.assertIn('--trusted-target', wording)
        self.assertNotIn('Enable-WindowsOptionalFeature', wording)

