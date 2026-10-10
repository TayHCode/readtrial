from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import readtrial.process
from readtrial.targets import Limits, Target, TargetError
from readtrial.trial import Agent, AgentError, snapshot


def adapter():
    from readtrial import codex_adapter
    return codex_adapter


class AdapterTests(unittest.TestCase):
    def test_cannot_override_native_policy(self):
        for command in ("codex exec -m gpt-6-luna --search", "codex exec -m gpt-6-luna -c features.shell_tool=true",
                        "codex exec --dangerously-bypass-approvals-and-sandbox"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                Agent(command, Limits())

    def test_native_execution_events_refused(self):
        for kind in ("command_execution", "mcp_tool_call", "file_change", "web_search"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                adapter().response(json.dumps({"type": "item.completed", "item": {"type": kind}}))

    def test_codex_protocol_requires_object(self):
        for output in ("", "not json", json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "null"}})):
            with self.subTest(output=output), self.assertRaises((ValueError, TypeError)):
                adapter().response(output)

    def test_native_configuration_errors_fail_closed(self):
        with self.assertRaises(ValueError):
            adapter().response(json.dumps({"type": "item.completed", "item": {"type": "error", "message": "unknown security control"}}))

    def test_native_malformed_items_and_unknown_events(self):
        for event in ({"type": "item.completed", "item": None},
                      {"type": "item.completed", "item": []}, {"type": "tool.started"}):
            with self.subTest(event=event), self.assertRaises((ValueError, TypeError)):
                adapter().response(json.dumps(event))

    def test_native_response_is_text_only(self):
        action = {"command": "python3 --version"}
        self.assertEqual(adapter().response(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(action)}})), action)

    def test_native_progress_precedes_final_action(self):
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "I will follow the README."}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": '{"command":"python3 --version"}'}}]
        self.assertEqual(adapter().response("\n".join(map(json.dumps, events))), {"command": "python3 --version"})

    def test_native_tool_after_progress_is_still_refused(self):
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "Checking."}},
                  {"type": "item.completed", "item": {"type": "command_execution"}}]
        with self.assertRaises(ValueError):
            adapter().response("\n".join(map(json.dumps, events)))

    def test_native_command_restrictions(self):
        args = adapter().argv("codex", "gpt-6-luna")
        for word in ("--no-daemon", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                     "features.shell_tool=false", "features.unified_exec=false", "features.apps=false",
                     "features.plugins=false", "features.hooks=false", 'web_search="disabled"'):
            self.assertIn(word, args)
        self.assertIn('tools.experimental_request_user_input.enabled=false', args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)

    def test_failed_wrapper_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("readtrial.codex_adapter.catalog", return_value=Path(directory) / "catalog.json"), patch("readtrial.trial.execute", return_value=readtrial.process.Result(2, "tool event denied", 0)), self.assertRaises(AgentError) as result:
                Agent("codex exec -m gpt-6-luna", Limits()).ask({}, 1, Path(directory))
            self.assertEqual(result.exception.result.exit_code, 2)


class PlatformTests(unittest.TestCase):
    def test_mac_refused_before_mutation(self):
        target = Target("ssh:example", Limits(), lambda *a: None)
        with patch("readtrial.targets.execute", return_value=readtrial.process.Result(0, "Darwin\n", 0)), self.assertRaisesRegex(TargetError, "address-space"):
            target.prepare(Path("unused"))
        self.assertFalse(target.created)

    def test_windows_powershell_probe_is_fixed(self):
        target = Target("ssh:example", Limits(), lambda *a: None)
        with patch("readtrial.targets.execute", side_effect=[readtrial.process.Result(1, "unknown uname", 0), readtrial.process.Result(0, "Win32NT", 0)]) as runner, self.assertRaisesRegex(TargetError, "requires --trusted-target"):
            target.prepare(Path("unused"))
        self.assertIn("powershell -NoProfile", runner.call_args.args[0][-1])
        self.assertFalse(target.created)

    def test_initial_ref_validated_before_git(self):
        with patch("readtrial.trial.execute") as runner, self.assertRaises(ValueError):
            snapshot("https://github.com/example/project", Path("unused"), Limits(), "--bad")
        runner.assert_not_called()

    def test_ssh_connection_failure_not_reported_as_windows(self):
        target = Target("ssh:example", Limits(), lambda *a: None)
        with patch("readtrial.targets.execute", return_value=readtrial.process.Result(255, "connection refused", 0)), self.assertRaisesRegex(TargetError, "SSH platform probe failed"):
            target.prepare(Path("unused"))
        self.assertFalse(target.created)

    def test_privilege_denied_in_disposable_target(self):
        from test_trial import PolicyTests
        PolicyTests.setUpClass()
        for command in ("sudo -n true", "su root", "doas true", "touch /tmp/outside"):
            with self.subTest(command=command):
                self.assertTrue(PolicyTests.worker.policy(command, True))


if __name__ == "__main__":
    unittest.main()
