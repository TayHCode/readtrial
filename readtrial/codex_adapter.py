"""Restricted native Codex entry point: text proposals, no native host tools."""
from __future__ import annotations

import json
import os
import re

from .process import execute
from .protocol import normalize

DISABLED = ("shell_tool", "unified_exec", "shell_snapshot", "apply_patch_freeform",
            "multi_agent", "multi_agent_v2", "apps", "plugins", "hooks",
            "skill_search", "skill_mcp_dependency_install", "browser_use", "computer_use",
            "in_app_browser", "image_generation", "view_image", "code_mode",
            "code_mode_host", "goals", "sleep_tool", "tool_suggest")


def argv(executable: str, model: str) -> list[str]:
    result = [executable, "--no-daemon", "exec", "-m", model, "--ignore-user-config",
              "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
              "--json", "--color", "never", "-c", "approval_policy=\"never\"",
              "-c", "web_search=\"disabled\"", "-c", "project_doc_max_bytes=0",
              "-c", "skills.include_instructions=false", "-c", "features.skip_host_skill_discovery=true",
              "-c", "suppress_unstable_features_warning=true", "-c", "features.code_mode_only=false",
              "-c", "tools.update_plan.enabled=false",
              "-c", "tools.experimental_request_user_input.enabled=false",
              "-c", "agents.enabled=false",
              "-c", "include_environment_context=false", "-c", "check_for_update_on_startup=false"]
    for name in DISABLED:
        result += ["-c", "features." + name + "=false"]
    return result


def response(output: str, *, notes: list[str] | None = None) -> dict:
    final = None
    for line in output.splitlines():
        if not line.strip().startswith("{"):
            if re.search(r'\bERROR codex_core::tools::router: error=failed to parse function arguments:', line):
                # Argument decoding rejected this call before a handler ran.
                # It is not an action. A usable final JSON action is still needed.
                continue
            if re.search(r'\b(?:ERROR|FATAL)\b', line):
                raise ValueError('Codex configuration diagnostic: ' + line[:1000])
            continue  # bounded CLI diagnostics, never treated as an action
        event = json.loads(line)
        if not isinstance(event, dict) or event.get("type") not in {
                "thread.started", "turn.started", "turn.completed", "turn.failed", "error",
                "item.started", "item.updated", "item.completed"}:
            raise ValueError("Codex returned an unknown event")
        item = event.get("item", {})
        if not isinstance(item, dict):
            raise TypeError("Codex returned a malformed item")
        if item.get("type") == "error":
            message = item.get("message", "")
            if not isinstance(message, str):
                raise TypeError('Codex returned a malformed diagnostic')
            if message.strip() == "Code Mode is unavailable because code-mode host is disabled":
                continue  # explicit fail-closed feature diagnostic, not execution
            raise ValueError("Codex configuration diagnostic: " + message[:1000])
        if item and item.get("type") not in {"agent_message", "reasoning"}:
            raise ValueError("Codex produced a non-text event (" + str(item.get("type")) + "); adapter refuses the trial")
        if event.get("type") in {"error", "turn.failed"}:
            raise ValueError("Codex turn failed")
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            final = item["text"]
    if not isinstance(final, str):
        raise TypeError("Codex returned no JSON action")
    action = json.loads(final)
    if not isinstance(action, dict):
        raise TypeError("Codex action must be a JSON object")
    return normalize(action, notes=notes)


def environment() -> dict[str, str]:
    # Drop orchestration/MCP injection and shell environment customizations.
    # Authentication stays in the CLI on the controller, never in a target.
    allowed = {"PATH", "HOME", "CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL",
               "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    return {name: value for name, value in os.environ.items() if name in allowed}


def catalog(executable: str, model: str, directory, timeout: float):
    """Retain the requested model; override its bundled code-only tool routing.

    Codex 0.162 model metadata tool_mode=code_mode_only overrides the feature
    switch. A private model_catalog_json copy selects direct routing with all
    feature tools disabled. No host/runtime or authentication config is changed.
    """
    result = execute([executable, '--no-daemon', 'debug', 'models', '--bundled'],
                     timeout=timeout, cwd=str(directory),
                     env={**environment(), "CODEX_HOME": str(directory), "HOME": str(directory)}, cap=4 * 1024 * 1024)
    if result.exit_code or result.timed_out or result.truncated:
        raise ValueError('cannot read bundled Codex model catalog: ' + result.output[:1000])
    payload = next((line for line in result.output.splitlines() if line.startswith("{")), "")
    value = json.loads(payload)
    items = value.get('models') if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise TypeError('invalid bundled Codex model catalog')
    selected = next((item for item in items if isinstance(item, dict) and item.get('slug') == model), None)
    if selected is None:
        raise ValueError('requested model is absent from the bundled Codex catalog; update CLI compatibility')
    selected['tool_mode'] = 'direct'
    selected['experimental_supported_tools'] = []
    # The catalog independently advertises patch tools too; disabling only the
    # freeform feature can leave the alternate patch route available.
    selected['apply_patch_tool_type'] = None
    selected['supports_search_tool'] = False
    path = directory / 'model-catalog.json'
    path.write_text(json.dumps({'models': [selected]}), encoding='utf-8')
    return path
