"""Narrow work-folder installation routes; text checks are not OS confinement."""
from __future__ import annotations

import importlib
import re
import shlex

shell = importlib.import_module(".shell", __package__) if __package__ else importlib.import_module("shell")

UV_SH = 'https://astral.sh/uv/install.sh'
UV_PS = 'https://astral.sh/uv/install.ps1'
DOWNLOAD_DOMAINS = {'astral.sh', 'releases.astral.sh', 'github.com', 'release-assets.githubusercontent.com',
                    'objects.githubusercontent.com', 'downloads.python.org'}


def quote_ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def environment(workdir: str, windows: bool = False) -> dict[str, str]:
    separator = '\\' if windows else '/'
    home = workdir + separator + 'home'
    return {'HOME': home, 'TMPDIR': workdir + separator + 'tmp',
            'TMP': workdir + separator + 'tmp', 'TEMP': workdir + separator + 'tmp',
            'XDG_CONFIG_HOME': home + separator + 'config',
            'XDG_DATA_HOME': home + separator + 'data',
            'XDG_CACHE_HOME': home + separator + 'cache',
            'XDG_STATE_HOME': home + separator + 'state',
            'UV_INSTALL_DIR': workdir + separator + 'bin', 'UV_NO_MODIFY_PATH': '1',
            'UV_PYTHON_INSTALL_DIR': workdir + separator + 'python',
            'UV_CACHE_DIR': workdir + separator + 'cache',
            'UV_TOOL_DIR': workdir + separator + 'tools',
            'UV_TOOL_BIN_DIR': workdir + separator + 'bin',
            'UV_PYTHON_BIN_DIR': workdir + separator + 'bin',
            'UV_PYTHON_INSTALL_BIN': '0', 'UV_NO_CONFIG': '1',
            'PIP_CONFIG_FILE': 'NUL' if windows else '/dev/null',
            'PIP_REQUIRE_VIRTUALENV': 'true', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}


def prepare_segment(command: str, workdir: str, windows: bool = False, repository: str = "") -> tuple[str, str, str]:
    """Return execution text, policy text, refusal. Never accept arbitrary pipelines."""
    text = command.strip()
    if not text or len(text) > 8192 or '\x00' in text:
        return command, command, 'empty, invalid, or oversized command'
    # Existing commands that merely inspect these names remain harmless. Changes
    # to runner-owned installation variables or PATH are refused outside the two
    # fully parsed installer forms below.
    special = 'astral.sh/uv/install.' in text
    if special:
        if windows:
            match = re.fullmatch(r'(?is)(?:powershell(?:\.exe)?\s+-ExecutionPolicy\s+ByPass\s+-(?:c|Command)\s+"(.*)"|(.*))', text)
            body = (match.group(1) or match.group(2)).strip() if match else ''
            prefix = r"(?:\$env:UV_INSTALL_DIR\s*=\s*'([^']+)'\s*;\s*\$env:UV_NO_MODIFY_PATH\s*=\s*'1'\s*;\s*)?"
            valid = re.fullmatch(prefix + r'irm\s+' + re.escape(UV_PS) + r'\s*\|\s*iex', body, re.IGNORECASE)
            expected = environment(workdir, True)['UV_INSTALL_DIR']
            if not valid or UV_PS not in body or (valid.group(1) and valid.group(1) != expected):
                return command, command, 'only the exact official uv installer with work-folder settings is allowed'
            run = "$env:UV_INSTALL_DIR=" + quote_ps(expected) + "; $env:UV_NO_MODIFY_PATH='1'; irm " + UV_PS + ' | iex'
        else:
            prefix = r'''(?:env\s+UV_INSTALL_DIR=('([^']*)'|"([^"]*)"|(\S+))\s+UV_NO_MODIFY_PATH=1\s+)?'''
            valid = re.fullmatch(r'curl\s+-LsSf\s+' + re.escape(UV_SH) + r'\s*\|\s*' + prefix + 'sh', text)
            expected = environment(workdir)['UV_INSTALL_DIR']
            if not valid or (valid.group(1) and valid.group(1).strip("'\"") != expected):
                return command, command, 'only the exact official uv installer with work-folder settings is allowed'
            run = 'curl -LsSf ' + UV_SH + ' | env UV_INSTALL_DIR=' + shlex.quote(expected) + ' UV_NO_MODIFY_PATH=1 sh'
        return run, 'true', ''
    # The installer-generated activation line would edit child PATH; the runner
    # already supplies it and instead verifies the owned binary exists.
    if not windows and text in {'. "$HOME/.local/bin/env"', 'source "$HOME/.local/bin/env"'}:
        return 'test -x ' + shlex.quote(workdir + '/bin/uv'), 'true', ''
    if not windows and re.fullmatch(r'(?:\.|source)\s+\.?/?\.venv/bin/activate', text):
        return 'test -x ./.venv/bin/python', 'true', ''
    if not re.match(r'(?i)^(?:echo|printf|Write-Output)\s', text) and re.search(r'(?i)(?:\b(?:UV_\w+|PIP_\w+|XDG_\w+|HOME|USERPROFILE|APPDATA|LOCALAPPDATA|TMPDIR|TMP|TEMP|VIRTUAL_ENV|PATH)\s*\+?=|\$env:(?:UV_\w+|PIP_\w+|XDG_\w+|HOME|USERPROFILE|APPDATA|LOCALAPPDATA|TMPDIR|TMP|TEMP|VIRTUAL_ENV|PATH)\s*\+?=|\bset(?:x)?\s+(?:PATH|HOME|XDG_\w+|TMPDIR|TMP|TEMP|UV_\w+|PIP_\w+))', text):
        return command, command, 'runner installation environment or PATH override'
    if re.search(r'(?i)\b(?:unset|env\s+-u|Set-Item|Remove-Item|SetEnvironmentVariable)\b[^;\n]*(?:UV_\w+|PIP_\w+|XDG_\w+|HOME|TMPDIR|TMP|TEMP|VIRTUAL_ENV|PATH)', text):
        return command, command, 'runner installation environment removal or override'
    if re.search(r'(?i)\b(?:curl|wget|irm|iwr|Invoke-WebRequest|Invoke-RestMethod)\b', text) and re.search(r'[|;\n]|&&|\$\(|`', text):
        return command, command, 'arbitrary downloaded script execution'
    if re.search(r'(?i)(?:\.bashrc|\.zshrc|\.profile|\$PROFILE|\bsetx\b)', text):
        return command, command, 'shell profile or persistent PATH change'
    # uv cannot be directed at the host interpreter, alternate registries,
    # configuration, global installs or outside installation destinations.
    if re.search(r'(?i)\b(?:uv|uvx|pip|pip3)\b|\s-m\s+pip\b', text) and re.search(r'(?i)--(?:system|user|prefix|target|directory|project|config-file|python-install-dir|cache-dir|isolated|managed-python-dir|tool-dir|index-url|extra-index-url|index|default-index|find-links|root|no-config|no-managed-python)(?:[=\s]|$)|(?:^|\s)-[it]|\buv\s+(?:self|tool)\b', text):
        return command, command, 'installation destination, configuration, registry or system override'
    if re.search(r'(?i)\buv\s+pip\b', text) and re.search(r'(?i)(?:--python(?:[=\s]|$)|\s-p(?:[=\s]|$))', text):
        return command, command, 'uv pip interpreter override'
    if re.search(r'(?i)\bgit\s+clone\b', text):
        match = re.fullmatch(r'git\s+clone\s+(https://[^\s]+)(?:\s+([A-Za-z0-9_.-]+))?', text)
        if not match or not repository or match.group(1).removesuffix('.git').rstrip('/') != repository.removesuffix('.git').rstrip('/') or match.group(2) in {'..', '.'}:
            return command, command, 'clone must use the repository under test and a relative child folder'
    # A bare pip install always targets the conventional work-folder venv. This
    # also preserves README activation semantics across stateless shell calls.
    pip = re.match(r'(?i)^(?:python(?:3(?:\.\d+)?)?\s+-m\s+pip|pip3?)\s+install\b', text)
    if pip:
        python = '.\\.venv\\Scripts\\python.exe' if windows else './.venv/bin/python'
        return python + ' -m pip' + text[pip.end() - len('install') - 1:], text, ''
    return command, command, ''


def prepare(command: str, workdir: str, windows: bool = False, repository: str = "") -> tuple[str, str, str]:
    # Preserve support for the complete official Windows invocation, including
    # its explicitly owned environment prefix. Otherwise validate pipeline groups
    # within a compound rather than granting an allowance to the whole command.
    if 'astral.sh/uv/install.' in command:
        whole = prepare_segment(command, workdir, windows, repository)
        if not whole[2]:
            return whole
    try:
        segments, operators = shell.split(command, windows)
    except ValueError as exc:
        return command, command, str(exc)
    runs, inspected, joins = [], [], []
    index = 0
    while index < len(segments):
        first = index
        while index < len(operators) and operators[index] == '|':
            index += 1
        segment = ' | '.join(segments[first:index + 1])
        # Downloads in a list can feed a later shell even without a pipe.
        if len(segments) > 1 and 'astral.sh/uv/install.' not in segment and re.search(r'(?i)\b(?:curl|wget|irm|iwr|Invoke-WebRequest|Invoke-RestMethod)\b', segment):
            return command, command, shell.denied(first + 1, segment, 'arbitrary downloaded script execution')
        run, policy, refusal = prepare_segment(segment, workdir, windows, repository)
        if refusal:
            return command, command, shell.denied(first + 1, segment, refusal)
        runs.append(run)
        inspected.append(policy)
        if index < len(operators):
            joins.append(operators[index])
        index += 1
    def join(parts: list[str]) -> str:
        return ' '.join(part + (' ' + joins[i] if i < len(joins) else '') for i, part in enumerate(parts))
    return join(runs), join(inspected), ''
