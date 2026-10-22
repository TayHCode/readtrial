from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from readtrial.cli import main, parser
from readtrial.process import Result, execute
from readtrial.targets import Limits, Target, TargetError
from readtrial.trial import (
    FIELDS,
    Agent,
    checks,
    diff,
    findings,
    regex_match,
    run_target,
    verdict,
)
from platforms import posix_only


# Runtime-only fake data, never a real key or credential.
def finding(identity="venv", actual="ensurepip unavailable"):
    return dict(zip(sorted(FIELDS), [""] * len(FIELDS))) | {
        "id": identity, "severity": "BLOCKER", "type": "PLATFORM", "machine": "fake",
        "command": "python3 -m venv .venv", "expected": "environment created",
        "actual": actual, "doc_line": "README.md:9"}


class FakeTarget:
    commands: ClassVar[dict] = {}
    metadata = '{}'
    prepare_error = close_error = ""
    closed = 0
    seen: ClassVar[list] = []
    network = "denied (fake)"

    def __init__(self, spec, limits, log, **kwargs):
        self.log = log

    def prepare(self, bundle):
        if self.prepare_error:
            raise TargetError(self.prepare_error)

    def command(self, command, **kwargs):
        type(self).seen.append(command)
        if "README.md" in command:
            value = Result(0, "1 Fake README\n2 python3 -m venv .venv\n", 0)
        elif "readme-trial.json" in command:
            value = Result(0, self.metadata, 0)
        else:
            value = self.commands.get(command, Result(0, "OK\n", 0))
        self.log("command", command, value)
        return value

    def close(self):
        type(self).closed += 1
        if self.close_error:
            raise TargetError(self.close_error)


class FakeAgent:
    actions: ClassVar[list] = []
    calls: ClassVar[list] = []

    def __init__(self, template, limits):
        self.index = 0

    def ask(self, context, timeout, directory):
        type(self).calls.append(json.loads(json.dumps(context)))
        action = self.actions[self.index]
        self.index += 1
        if isinstance(action, Exception):
            raise action
        return action, Result(0, json.dumps(action), 0)


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        FakeTarget.commands = {}
        FakeTarget.metadata = '{}'
        FakeTarget.prepare_error = FakeTarget.close_error = ""
        FakeTarget.closed = 0
        FakeTarget.seen = []
        FakeAgent.actions = [{"done": True, "findings": []}]
        FakeAgent.calls = []
        self.config = {"repo": "https://github.com/example/project", "targets": ["ssh:example"],
                       "agent_command": "fake-agent", "checks": [{"command": "verify", "match": "OK"}],
                       "limits": asdict(Limits())}

    def tearDown(self):
        self.temp.cleanup()

    def trial(self, **kwargs):
        return run_target(self.config, "ssh:example", self.root / "result", self.root / "source.bundle",
                          "abc123", target_factory=FakeTarget, agent_factory=FakeAgent, **kwargs)

    @posix_only
    def test_pass_from_checks(self):
        self.assertEqual(self.trial()["verdict"], "PASS (partial)")
        self.assertEqual(FakeTarget.closed, 1)

    @posix_only
    def test_blocker_opinion_cannot_fail(self):
        FakeAgent.actions = [{"done": True, "findings": [finding()]}]
        self.assertEqual(self.trial()["verdict"], "PASS (partial)")

    def test_empty_opinion_cannot_pass_failed_check(self):
        FakeTarget.commands["verify"] = Result(1, "bad", 0)
        self.assertEqual(self.trial()["verdict"], "FAIL")

    @posix_only
    def test_regex_mismatch(self):
        FakeTarget.commands["verify"] = Result(0, "different", 0)
        self.assertEqual(self.trial()["verdict"], "FAIL")

    def test_no_checks_incomplete(self):
        self.config["checks"] = []
        self.assertEqual(self.trial()["verdict"], "INCOMPLETE")

    @posix_only
    def test_repo_declarations(self):
        self.config["checks"] = []
        FakeTarget.metadata = '{"checks":[{"command":"verify","match":"OK"}]}'
        self.assertEqual(self.trial()["verdict"], "PASS (partial)")

    @posix_only
    def test_cli_overrides_repo_declarations(self):
        FakeTarget.metadata = 'not json'
        self.assertEqual(self.trial()["verdict"], "PASS (partial)")
        self.assertFalse(any("readme-trial.json" in c for c in FakeTarget.seen))

    def test_malformed_repo_checks(self):
        self.config["checks"] = []
        FakeTarget.metadata = '{"checks":"echo OK"}'
        self.assertEqual(self.trial()["verdict"], "INCOMPLETE")

    @posix_only
    def test_hidden_checks(self):
        self.trial()
        self.assertNotIn("verify", json.dumps(FakeAgent.calls))

    @posix_only
    def test_trial_failure_cannot_be_overruled(self):
        FakeAgent.actions = [{"command": "attempt"}, {"done": True, "findings": []}]
        FakeTarget.commands["attempt"] = Result(1, "bad", 0)
        self.assertEqual(self.trial()["verdict"], "FAIL")

    def test_trial_denial(self):
        FakeAgent.actions = [{"command": "attempt"}, {"done": True, "findings": []}]
        FakeTarget.commands["attempt"] = Result(126, "", 0, denied="unsafe")
        self.assertEqual(self.trial()["verdict"], "INCOMPLETE")

    @posix_only
    def test_trial_output_cap(self):
        FakeAgent.actions = [{"command": "attempt"}, {"done": True, "findings": []}]
        FakeTarget.commands["attempt"] = Result(0, "OK", 0, truncated=True)
        self.assertEqual(self.trial()["verdict"], "FAIL")

    def test_check_timeout(self):
        FakeTarget.commands["verify"] = Result(124, "", 0, timed_out=True)
        self.assertEqual(self.trial()["verdict"], "FAIL")

    def test_agent_invalid_protocol(self):
        FakeAgent.actions = [{"command": "verify", "done": True}] * 2
        self.assertEqual(self.trial()["verdict"], "INCOMPLETE")
        self.assertEqual(FakeTarget.closed, 1)

    def test_agent_failure_cleanup(self):
        FakeAgent.actions = [TargetError("fake provider unavailable")]
        self.assertEqual(self.trial()["verdict"], "INCOMPLETE")
        self.assertEqual(FakeTarget.closed, 1)

    def test_prepare_failure_cleanup(self):
        FakeTarget.prepare_error = "quota unavailable"
        self.assertEqual(self.trial()["verdict"], "INCOMPLETE")
        self.assertEqual(FakeTarget.closed, 1)

    @posix_only
    def test_cleanup_failure_blocks_pass(self):
        FakeTarget.close_error = "cleanup failure"
        self.assertEqual(self.trial()["verdict"], "INCOMPLETE")

    def test_step_cap(self):
        self.config["limits"]["max_steps"] = 1
        FakeAgent.actions = [{"command": "attempt"}]
        self.assertEqual(self.trial()["verdict"], "INCOMPLETE")

    def test_rerun_preserves_checks(self):
        self.config["checks"] = [{"command": "different"}]
        result = self.trial(fixed_checks=[{"command": "verify"}], previous=[finding()])
        self.assertEqual(result["checks"][0]["command"], "verify")
        changes = json.loads((self.root / "result" / "diff.json").read_text())
        self.assertTrue(changes["unassessed"][0]["id"].startswith("advisory-"))
        self.assertEqual(changes["unassessed"][0]["command"], finding()["command"])

    @posix_only
    def test_reports_and_command_evidence(self):
        self.trial()
        report = self.root / "result"
        self.assertEqual({p.name for p in report.iterdir()}, {"commands.jsonl", "findings.json", "result.json", "report.md"})
        records = [json.loads(line) for line in (report / "commands.jsonl").read_text().splitlines()]
        self.assertTrue(all({"exit_code", "seconds", "output", "command"} <= record.keys() for record in records))

    def test_duplicate_finding_ids_incomplete(self):
        FakeAgent.actions = [{"done": True, "findings": [finding(), finding()]}] * 2
        self.assertEqual(self.trial()["verdict"], "INCOMPLETE")

    def test_regex_timeout_incomplete(self):
        with patch("readtrial.trial.regex_match", side_effect=ValueError("regex timeout")):
            self.assertEqual(self.trial()["verdict"], "INCOMPLETE")


class PureTests(unittest.TestCase):
    def test_diff_added_resolved_changed(self):
        result = diff([finding("fixed"), finding("changed", "before")],
                      [finding("new"), finding("changed", "after")])
        self.assertEqual(result["added"][0]["id"], "new")
        self.assertEqual(result["no_longer_raised"][0]["id"], "fixed")
        self.assertEqual(result["changed"][0]["after"]["actual"], "after")

    def test_diff_unchanged(self):
        self.assertEqual(diff([finding()], [finding()])["unchanged"], ["venv"])

    def test_invalid_severity(self):
        value = finding() | {"severity": "PASS"}
        with self.assertRaises(ValueError):
            findings([value], "fake")

    def test_machine_assigned_by_script(self):
        self.assertEqual(findings([finding()], "target")[0]["machine"], "target")

    def test_missing_finding_field(self):
        value = finding()
        value.pop("actual")
        with self.assertRaises(ValueError):
            findings([value], "target")

    def test_invalid_check_regex(self):
        with self.assertRaises(ValueError):
            checks([{"command": "run", "match": "["}])

    def test_invalid_check_fields(self):
        with self.assertRaises(ValueError):
            checks([{"command": "run", "pass": True}])

    def test_claude_tool_policy(self):
        value = Agent("claude -p --model claude-haiku-5-5", Limits())
        self.assertIn("--tools", value.argv)
        self.assertEqual(value.argv[value.argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", value.argv)

    def test_claude_cannot_override_tools(self):
        with self.assertRaises(ValueError):
            Agent("claude -p --tools Bash", Limits())

    def test_codex_wrapped(self):
        value = Agent("codex exec -m gpt-6-luna", Limits())
        self.assertIn("--ignore-user-config", value.argv)
        self.assertIn("features.shell_tool=false", value.argv)

    def test_arbitrary_agent_argv_never_shell(self):
        self.assertEqual(Agent("fake-agent '$(echo bad)'", Limits()).argv[-1], "$(echo bad)")

    def test_host_target_rejected(self):
        with self.assertRaises(TargetError):
            Target("local", Limits(), lambda *a: None)

    def test_ssh_argument_injection_rejected(self):
        with self.assertRaises(TargetError):
            Target("ssh:host;echo", Limits(), lambda *a: None)

    def test_ssh_tokens_quoted(self):
        target = Target("ssh:example", Limits(), lambda *a: None)
        value = target.outer(["echo", "$(echo bad)"])
        self.assertIn("'$(echo bad)'", value[-1])

    def test_ssh_no_forwarded_auth_or_hostkey_autotrust(self):
        value = Target("ssh:example", Limits(), lambda *a: None).prefix
        self.assertIn("ForwardAgent=no", value)
        self.assertIn("StrictHostKeyChecking=yes", value)

    @posix_only
    def test_regex(self):
        self.assertTrue(regex_match("O+K", "OOK", 1))
        self.assertFalse(regex_match("missing", "OOK", 1))

    def test_cli_bad_match_count(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as result:
            main(["run", ".", "--target", "ssh:example", "--agent-command", "fake", "--check", "x", "--match", "x", "--match", "y", "--out", "unused"])
        self.assertEqual(result.exception.code, 2)

    def test_positive_limits(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            parser().parse_args(["run", ".", "--target", "ssh:example", "--agent-command", "fake", "--command-seconds", "0", "--out", "unused"])

    def test_verdict_without_checks(self):
        self.assertEqual(verdict([], []), "INCOMPLETE")


class ProcessTests(unittest.TestCase):
    @posix_only
    def test_output_cap(self):
        result = execute([sys.executable, "-c", "print('x'*100000)"], cap=100)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.output), 100)

    @posix_only
    def test_timeout(self):
        result = execute([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.15)
        self.assertTrue(result.timed_out)
        self.assertLess(result.seconds, 2)

    @posix_only
    def test_exit_and_stdin(self):
        result = execute([sys.executable, "-c", "import sys;print(sys.stdin.read());sys.exit(3)"], data=b"fake input")
        self.assertEqual(result.exit_code, 3)
        self.assertIn("fake input", result.output)

    def test_missing_command(self):
        self.assertEqual(execute(["/nonexistent/readtrial-test"]).exit_code, 127)

    @posix_only
    def test_orphan_holding_pipe_killed(self):
        script = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])"
        result = execute([sys.executable, "-c", script], timeout=0.2)
        self.assertTrue(result.timed_out)
        self.assertLess(result.seconds, 2)


class PolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # worker.py is copied standalone to a target and imports its sibling.
        import importlib.util

        import readtrial.installers
        import readtrial.isolation
        import readtrial.process
        spec = importlib.util.spec_from_file_location("target_worker", Path(__file__).parents[1] / "readtrial" / "worker.py")
        cls.worker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"process": readtrial.process, "installers": readtrial.installers,
                                     'isolation': readtrial.isolation}):
            spec.loader.exec_module(cls.worker)

    def test_fake_secret_path_denied(self):
        import secrets
        fake_name = ".ssh/" + secrets.token_hex(8)
        self.assertTrue(self.worker.policy("cat " + fake_name))

    def test_outside_workspace_denied(self):
        for command in ["cat /etc/passwd", "cd ../other", "touch ~/x"]:
            with self.subTest(command=command):
                self.assertTrue(self.worker.policy(command))

    def test_system_change_on_ssh_denied(self):
        self.assertTrue(self.worker.policy("apt-get install git"))

    def test_service_change_on_container_denied(self):
        self.assertTrue(self.worker.policy("systemctl restart app", True))

    def test_browser_debugging_denied(self):
        self.assertTrue(self.worker.policy("chromium --remote-debugging-port=45678"))

    def test_normal_command_allowed(self):
        self.assertEqual(self.worker.policy("python3 -m venv .venv"), "")

    def test_sandbox_tmpfs_no_home_mount(self):
        value = self.worker.sandbox("/tmp/control", "/tmp/bundle", 128)
        self.assertIn("--tmpfs", value)
        self.assertIn("--size", value)
        self.assertIn("--unshare-all", value)
        self.assertIn("--remount-ro", value)
        self.assertNotIn("/home", value)
        self.assertNotIn("--share-net", value)


class ProxyTests(unittest.TestCase):
    def test_domain_denied_before_dns(self):
        from readtrial.proxy import approved
        with patch("socket.getaddrinfo") as dns, self.assertRaises(ValueError):
            approved("evil.invalid", 443, {"pypi.org"})
        dns.assert_not_called()

    def test_private_resolution_denied(self):
        import socket

        from readtrial.proxy import approved
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=address), self.assertRaises(ValueError):
            approved("pypi.org", 443, {"pypi.org"})

    def test_non_web_ports_denied(self):
        from readtrial.proxy import approved
        with self.assertRaises(ValueError):
            approved("pypi.org", 22, {"pypi.org"})


class AdditionalFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        PolicyTests.setUpClass()

    def test_agent_malformed_output_keeps_evidence(self):
        from readtrial.trial import AgentError
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent("fake-agent", Limits())
            with patch("readtrial.trial.execute", return_value=Result(0, "not JSON", 0)), self.assertRaises(AgentError) as caught:
                agent.ask({}, 1, Path(directory))
        self.assertIn('agent-protocol-error:', caught.exception.result.output)
        self.assertIn('[response omitted]', caught.exception.result.output)

    def test_agent_exit_failure_keeps_evidence(self):
        from readtrial.trial import AgentError
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent("fake-agent", Limits())
            with patch("readtrial.trial.execute", return_value=Result(7, "provider unavailable", 0)), self.assertRaises(AgentError) as caught:
                agent.ask({}, 1, Path(directory))
        self.assertEqual(caught.exception.result.exit_code, 7)

    def test_claude_scalar_json_is_rejected(self):
        from readtrial.trial import AgentError
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent("claude -p", Limits())
            with patch("readtrial.trial.execute", return_value=Result(0, "null", 0)), self.assertRaises(AgentError):
                agent.ask({}, 1, Path(directory))

    def test_claude_customizations_and_browser_disabled(self):
        value = Agent("claude -p --model claude-haiku-5-5", Limits()).argv
        self.assertIn("--safe-mode", value)
        self.assertIn("--no-chrome", value)
        self.assertIn("--system-prompt", value)

    def test_source_credential_url_rejected_before_git(self):
        import secrets

        from readtrial.trial import snapshot
        with tempfile.TemporaryDirectory() as directory, patch("readtrial.trial.execute") as runner:
            url = "https://example:" + secrets.token_hex(8) + "@" + "github.com/example/project"
            with self.assertRaises(ValueError):
                snapshot(url, Path(directory), Limits())
        runner.assert_not_called()

    def test_source_remote_helper_rejected_before_git(self):
        from readtrial.trial import snapshot
        with tempfile.TemporaryDirectory() as directory, patch("readtrial.trial.execute") as runner, self.assertRaises(ValueError):
            snapshot("ext::arbitrary-command", Path(directory), Limits())
        runner.assert_not_called()

    def test_control_protection_fails_closed(self):
        with patch.object(PolicyTests.worker.ctypes, "CDLL") as library:
            library.return_value.prctl.return_value = -1
            with self.assertRaises(OSError):
                PolicyTests.worker.protect_control()

    def test_control_protection_syscall(self):
        with patch.object(PolicyTests.worker.ctypes, "CDLL") as library:
            library.return_value.prctl.return_value = 0
            PolicyTests.worker.protect_control()
            library.return_value.prctl.assert_called_once_with(4, 0, 0, 0, 0)

    @posix_only
    def test_stdout_close_does_not_kill_successful_child(self):
        script = "import os,time;os.close(1);os.close(2);time.sleep(.05)"
        result = execute([sys.executable, "-c", script], timeout=1)
        self.assertEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
