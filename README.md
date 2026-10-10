# readtrial

Test whether a repository works the way its README says. A weak AI agent acts as
an impatient stranger: it sees the README, proposes commands, and records where
it gets stuck. A restricted runner executes those commands in a fresh disposable
target. An independent script decides PASS, PASS (partial), FAIL, or INCOMPLETE.

The result is command evidence, advisory findings, and a learning loop:
**try the README → fix the project or docs → rerun → compare findings**.
No model SDK or runtime packages. Python 3.11+ and Git are required on the
controller. This checkout is prepared locally; its future public repository has
not been published. Obtain the checkout from the person who supplied this release.

## Five-minute CLI quickstart

Start in the checkout directory. These commands show the CLI and an offline fake FAIL → PASS loop without installing
anything, contacting a model, creating a real target, or executing repository code.
The demo finishes in under 20 seconds and writes real sample reports and a diff;
see [DEMO.md](DEMO.md). Unit tests use fakes and run offline.
A first real trial also requires a supported target and an authenticated agent CLI.

### Linux

```sh
python3 --version
python3 -m readtrial --help
python3 -m readtrial demo --out trial-results/demo
python3 -m unittest discover -s tests -q
```

Use Python 3.11 or newer. If stock Python is older, use uv ([installation guide](https://docs.astral.sh/uv/getting-started/installation/)):

```sh
uv run --no-project --python 3.12 python -m readtrial --help
```

### macOS

Stock macOS Python may be too old. If `python3 --version` is 3.11 or newer:

```sh
python3 -m readtrial --help
```

Otherwise, use uv to obtain an isolated interpreter:

```sh
uv run --no-project --python 3.12 python -m readtrial --help
```

If neither is available, install Python 3.11+ from [python.org](https://www.python.org/downloads/)
or follow [uv's installation guide](https://docs.astral.sh/uv/getting-started/installation/).
Do not replace system Python. macOS SSH trials are supported from a Linux
controller with `--trusted-target`; the target runner needs no Python upgrade.
Full macOS-controller trials are unverified.

### Windows PowerShell

With Python 3.11+ installed and the Python launcher available:

```powershell
py -3 -m readtrial --help
```

With uv installed instead:

```powershell
uv run --no-project --python 3.12 python -m readtrial --help
```

Run actual Windows SSH trials from a Linux controller with `--trusted-target`.
The SSH login shell may be cmd; trial commands use PowerShell with profiles off.
The unit suite's controller process tests require POSIX. Windows-controller
trial execution remains unverified.

### Optional installed command

The direct module command is the standard-library path. An installed entry point
is also available; use your own fresh virtual environment:

On Debian/Ubuntu, the following venv command needs the distribution's
`python3-venv` package. If it is unavailable, use the module command or
[install uv](https://docs.astral.sh/uv/getting-started/installation/) and use its
managed interpreter instead. readtrial never installs system prerequisites on SSH.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
readtrial --help
```

## First real trial

Use a Linux controller. For a **trusted repository on your own Linux SSH machine**,
the target needs Git and Bash. You must acknowledge that Linux SSH is not
confined. It enforces a fresh folder, time/output limits, visible-command denials
and tracked-process cleanup; it does **not** enforce filesystem or network isolation.

```sh
python3 -m readtrial run ./your-repo --target ssh:your-host --i-understand-ssh-linux-is-not-confined --agent-command 'claude -p --model claude-haiku-5-5' --check 'python3 -m unittest discover -s tests -q' --out trial-results/first
python3 -m readtrial rerun trial-results/first --out trial-results/second
```

Replace the example success check with the project's promised outcome.
The check must succeed independently of the model's opinion. Targets execute
sequentially; repeat `--target` to compare machines. Output directories must be new.
Never use SSH for an untrusted repository. Use an isolated container or a VM.

For LXC, an existing LXD-compatible `lxc` CLI, stock Debian/Ubuntu image, and a
storage pool that enforces root disk quotas are required. Nothing is added to an
existing profile, instance, service, or network:

```sh
python3 -m readtrial run https://github.com/example/project --target lxc:ubuntu:24.04 --agent-command 'claude -p --model claude-haiku-5-5' --check 'python3 -m unittest discover -s tests -q' --out trial-results/container
```

Use `--lxc-pool POOL` when multiple pools exist. Use
`--instance-prefix owner-trial-` to recognize your instances; only exact owned
names are deleted. `--dry-run` on run/rerun prints the configuration and planned
actions without fetching, calling an agent, creating output, or contacting a target. A fresh unprivileged container has
no inherited profiles, NIC, home mounts, credentials, or container sockets.
Bootstrap installs Git, curl, Python, bubblewrap, certificates, and util-linux, without
adding Python venv support. This preserves stock prerequisite failures.
An exact-domain loopback proxy supplies package/repository access; no NIC provides
a direct network route. Unsupported isolation or quota setup fails INCOMPLETE.

## Trusted macOS and Windows SSH targets

`--trusted-target` is required on these targets and prints this notice prominently:

> less isolated: this target runs the repository's commands with your user's permissions inside a fresh work folder; use only on machines you own and repositories you trust; untrusted repositories are supported only on the Linux container target

readtrial runs on the Linux controller. macOS needs its stock POSIX shell/tools
and Git, not Python 3.11; the repository's commands use the target's actual tools.
Windows needs Git, PowerShell, and `taskkill`; cmd as the SSH shell is supported.
The runner creates a new work folder under the user's temp directory (`mktemp -d`
on macOS; a unique PowerShell directory under `%TEMP%` on Windows). It supplies
fresh HOME/temp paths and disables Git credential helpers/hooks during cloning.
No owner software or system prerequisite is installed by setup.

```sh
python3 -m readtrial run ./your-repo --target ssh:your-mac --trusted-target --agent-command 'claude -p --model claude-haiku-5-5' --check 'python3 script.py --help' --out trial-results/mac
python3 -m readtrial run ./your-repo --target ssh:your-windows --trusted-target --agent-command 'codex exec -m gpt-6-luna' --check 'py -3 script.py --help' --out trial-results/windows
```

The flag, exact agent/checks, and limits persist through `rerun`. Every report
records trusted mode and the actual isolation/network level. The agent still has
no host tools: all proposed commands pass through this runner and are logged.

| Target | Isolation level | Filesystem/cwd | Network | Resource/cleanup controls |
| --- | --- | --- | --- | --- |
| Linux LXC | Confined, unprivileged container plus namespace | Private writable workspace; read-only system mounts | Exact repository/registry proxy, no NIC | CPU/RAM/process/disk ceilings; time/output bounds; verified deletion |
| Linux SSH | Explicit acknowledgement; user permissions; **not confined** | Fresh clone and HOME/temp; visible escapes refused | URL inspection only; **no network isolation** | Time/output bounds; tracked process-group cleanup and owned-folder removal; no RAM/disk/CPU quota |
| macOS SSH | Trusted mode only; user permissions | Commands start in clone; visible escapes refused; **no OS confinement** | Explicit URL/host inspection only; **no network isolation** | Time/output bounds, process-group kill; verified owned-folder/process cleanup; **no memory/disk/CPU quota** |
| Windows SSH | Trusted mode only; user permissions | Commands start in clone; visible escapes refused; **no OS confinement** | Explicit URL/host inspection only; **no network isolation** | Time/output bounds, `taskkill /T`; verified owned-folder/process cleanup; **no memory/disk/CPU quota or Job Object isolation** |
| OCI | Unsupported unless Docker/Podman and a suitable image exist | Intended confined container; not live verified here | Intended denied | Missing engine returns INCOMPLETE |

Trusted-mode command inspection refuses visible credential/SSH paths, browser
profiles and ports, sudo/admin elevation (`Start-Process -Verb RunAs`), system
package managers, global Git changes, outside paths, and unrelated explicit URLs.
The conservative filters can reject legitimate commands too; a denial is evidence
of a runner limitation, not proof of a repository defect. Each command starts in
the clone and obvious cwd escapes are refused. This is
**not cwd or filesystem confinement**: arbitrary scripts, computed/encoded paths,
symlinks, shell syntax, subprocesses, and installers can bypass text checks and
read/write as your user. Repository subprocesses can contact arbitrary hosts.
Only literal HTTP(S) URLs in proposed commands are checked against repository and
listed registry/distribution domains; it is not a firewall, DNS policy, or redirect boundary.
Time/output caps terminate ordinary process trees. Detached/reparented processes
can evade process-group/taskkill tracking; cleanup verifies tracked descendants,
not every possible malicious process. Output storage is capped when collected;
temporary output files can grow between polls. Forced controller/transport loss
can prevent cleanup. These limitations are the reason explicit trust is required.

## Work-folder installation allowance

README-directed user-space installs are accepted on trusted targets and Linux
SSH/LXC. The runner recognizes only these complete official installer forms:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

It rewrites the POSIX line to `curl -LsSf https://astral.sh/uv/install.sh | env
UV_INSTALL_DIR=<WORKDIR>/bin UV_NO_MODIFY_PATH=1 sh`. On Windows it runs the exact
`irm https://astral.sh/uv/install.ps1 | iex` body with
`$env:UV_INSTALL_DIR='<WORKDIR>\bin'; $env:UV_NO_MODIFY_PATH='1'` first. Explicit
settings are accepted only when they match these owned destinations and disable
PATH/profile modification. Extra installer flags, suffixes, other URLs, HTTP, alternate
shells and outside destinations are refused. Other compound commands are split
at top-level `&&`, `||`, `;` and pipes, inspected segment by segment, and refused
with the offending segment number and reason. Venv activation and pip rewrites
work inside compounds. Arbitrary download-and-execute chains remain refused. An official POSIX
`. "$HOME/.local/bin/env"` follow-up is replaced by an owned uv binary check;
the runner already puts `<WORKDIR>/bin` first on the child PATH. No owner PATH or
shell profile is edited. Original proposed commands remain in the evidence log.
Rewrite rows contain the executed rewrite's real exit code and segment evidence;
they are not extra successes for the unexecuted original text.

Every child gets forced `UV_PYTHON_INSTALL_DIR`, `UV_CACHE_DIR`, `UV_TOOL_DIR`,
`UV_TOOL_BIN_DIR` and `UV_PYTHON_BIN_DIR` below its work folder, plus
`UV_NO_MODIFY_PATH=1`, `UV_NO_CONFIG=1` and `UV_PYTHON_INSTALL_BIN=0`.
`uv python install`, `uv venv`, `uv pip install`, `uv run` and `uvx` are allowed.
Plain `python -m venv` uses a relative folder; bare `pip install` or
`python -m pip install` is redirected to the clone's `.venv` interpreter.
The child PATH includes that venv's bin/Scripts directory. pip requires a venv
and ignores owner configuration. Missing venvs still fail; none is silently made.
PyPI package installs and an HTTPS `git clone` of the exact repository under test
into a relative child folder are accepted. Installation environment overrides,
system/user installs, alternate indexes, global uv tools/self-update, shell profile
changes, and outside paths remain refused. The existing privilege, service,
credential, browser and system-package deny list remains in force.

An allowance does not change the target boundary. All SSH targets use user
permissions and have unrestricted subprocess networking; only explicit URLs are inspected.
LXC's no-NIC proxy adds exact uv/Python distribution hosts (`astral.sh`,
`github.com`, `release-assets.githubusercontent.com`, `objects.githubusercontent.com`,
`downloads.python.org`) alongside its existing repository/registry domains.
HTTPS CONNECT checks host/port/public address, not encrypted URL paths. The exact
installer URL restriction is command inspection. Trusted targets inspect explicit
URLs but cannot confine installer child code, redirects or symlink destinations;
use only trusted repositories and installers. LXC namespaces retain their
filesystem/resource boundary. These are documented controls, not a claim that
text filtering confines arbitrary code on macOS or Windows.

## Agent adapters

The agent is a trusted program on the controller, separate from repository code.
Its authentication never enters the target. The runner is the only way the agent
executes target commands. Both supported templates are argv, never shell text:

```text
claude -p --model claude-haiku-5-5
codex exec -m gpt-6-luna
```

Both adapters require a first response with exactly one `command` field. Its value
is shell text. The runner also accepts one accidentally serialized JSON object
containing a valid command action; deeper nesting and unknown fields are refused. Later responses use the fixed
command/completion schema; aliases are accepted only when normalizing transcripts.
The Claude adapter forces a JSON output schema, tools off, safe mode, no Chrome, no skills, no settings
discovery, empty strict MCP configuration, and no session persistence.
The Codex adapter accepts exactly `codex exec -m MODEL`; it supplies the restrictive
flags itself. User config, rules, project instructions, hooks, plugins, apps,
shell/unified execution, code mode, browser tools, image tools, and subagents are
disabled; web search is disabled and the CLI runs without a daemon in an empty
working directory. Codex 0.162.0 marks Luna as `tool_mode=code_mode_only`; that
model metadata overrides `features.code_mode_only=false`. Disabling only the
host left unusable Code Mode calls and confused the agent. readtrial reads the
installed bundled catalog without networking and creates a private per-invocation
`model_catalog_json` copy for the **same requested model**, selecting direct routing
and removing experimental, patch and search capabilities. It then applies the tool-disabling features and
`--output-schema`. No owner CLI configuration or model is replaced. A model absent
from the installed catalog fails explicitly. Non-text tool events and unknown configuration diagnostics
make the trial INCOMPLETE. The CLI still authenticates on the controller.
Tool flags vary by CLI version: a refusal is a compatibility finding, never an
excuse to enable tools. These controls have live smoke and denial evidence;
they are not a proof against vulnerabilities in a CLI or its provider.

Other adapters must be trusted JSON-only programs with all host tools disabled.
They are not sandboxed by readtrial. Do not pass an arbitrary unreviewed executable.
`{prompt}` substitutes the prompt as one argv token; otherwise stdin supplies it.

The JSON command action remains the runner even with native CLI tools off. The
agent must try a usable documented command and select the documented platform
prerequisite route after observing an unsupported interpreter version.

The agent sees numbered README text and bounded command history. It follows
only those docs, stops blocked steps, records surprises, and never repairs source.
A trial stops after the first README offline/help goal; test browser launches or
full audit workflows separately.
That can leave declared quickstart steps untested and produces PASS (partial).
An exact successful command proposed again is logged as `duplicate-command`
with `exit_code:null` and is not executed again. The agent receives a reminder;
repeated proposals still consume its step budget.
The schema uses **command** as its only command field; native CLI responses
after the first turn also include `done` and `findings` (`command:null` on completion). The adapter
normalizes documented legacy aliases `proposed_command` and `cmd`, refuses
conflicting/unknown fields, and retries one malformed response with a corrective
message, logging each response as its own agent row. A single serialized command
action is unwrapped before execution; another malformed reply receives one
corrective retry rather than ending the trial immediately. Completion without any README command is a protocol error. Every
adapter failure is reported as `agent-protocol-error:` with a bounded scrubbed
diagnosis; raw CLI envelopes and session/usage metadata are omitted from errors
and stored agent evidence. Target temporary paths are abbreviated as `<WORKDIR>`
in commands.jsonl, including Windows account directories. Plain CLI progress is ignored. Codex router argument-decoding rejections
(`failed to parse function arguments:`) are harmless diagnostics: the handler did
not run, and a usable final JSON action is still required. Other router errors
and unlisted configuration diagnostics fail explicitly. The exact legacy
“Code Mode is unavailable because code-mode host is disabled” event is an
acknowledged fail-closed diagnostic, never a command or proof of success; the
fixed live adapter no longer emits it. Native execution events remain fatal.

Each normalized invocation returns one action:

```json
{"command": "python3 -m venv .venv"}
```

or finishes:

```json
{"done": true, "findings": [{"id": "venv", "severity": "BLOCKER", "type": "PLATFORM", "machine": "target", "command": "python3 -m venv .venv", "expected": "A virtual environment", "actual": "ensurepip unavailable", "doc_line": "README.md:30"}]}
```

Every command starts in the clone. Files persist; shell state does not. If an
agent adds only a simple `cd` prefix to an otherwise exact declared README
command, the runner ignores that prefix, executes the declared command from the
clone root, and records an `agent-deviation` note with the original proposal.
Other unmatched failures are labelled agent commands rather than README steps. Use
`cd subdir && ...` in one POSIX command. The brief names the target OS and shell.
On Windows PowerShell 5.1, `&&`/`||` are unsupported: use `py -3`, `;` and
`if ($?)`. README reads and console text use explicit UTF-8; OEM and UTF-16
transport decoding preserve text rather than replacing bytes with U+FFFD.
The controller assigns stable IDs rather than using model-chosen finding IDs.
Their identity is derived from target transport/OS class, normalized full command,
real failing exit code and failed segment index, independent of model-chosen IDs
or wording. Normalization trims outer/separator spacing while preserving arguments,
quotes and case. Reruns also canonicalize older findings using their saved evidence.
Different exit codes or segments identify different failures. Unevidenced advisory
commands use an unknown exit and segment zero, with a distinct `advisory-` prefix.
They are excluded from added/changed/no-longer-raised finding comparisons; earlier
unevidenced opinions remain unassessed. They cannot establish fixes.
Ambiguous segment-only matches remain advisory rather than merging failures from
different working directories. Interpretations of the same command/failure key
are grouped; wording changes appear as changed, never as new model-generated IDs.
For subdirectories in PowerShell use `cd subdir; if ($?) { command }`.
Visible relative paths are resolved against the command's in-clone directory;
`cd examples` followed by `../hello.py` is allowed when its real path stays in
the clone. Existing symlink escapes are refused. SSH path resolution uses the
target's stock `python3` or Windows `py -3`; a missing resolver is a runner
limitation. Text inspection remains unable to confine computed paths or child code.
Windows captures native stdout and stderr as bytes through separate process
streams, then combines stdout followed by stderr within the output cap. Native
stderr is not formatted as PowerShell `NativeCommandError` records. Stream
interleaving is not retained; real exit and segment codes still decide checks.
The controller assigns machine identity and validates every finding field.

## Checks and the learning loop

Declare checks with `--check`, optionally one `--match` regex for each check.
CLI checks override the repository's optional `readme-trial.json`. Missing checks
are rejected before the agent loop. For a local repository, absent committed
checks are detected before output or target creation. A remote declaration is
read during target setup, before any agent invocation:

```json
{"checks": [{"command": "python3 -m unittest discover -s tests -q", "match": "OK"}]}
```

The checks stay hidden from the agent and execute independently after its attempt.
A script returns full PASS (exit 0) only if every declared README step ran and
succeeded, every attempted command succeeded, and independent checks passed.
Stdout and report show the PASS label followed by
`all N declared README steps ran and succeeded`.
Successful checks with incomplete coverage return **PASS (partial)** (exit 0),
with `PASS (partial): agent ran k of N README steps` on stdout and in the report.
This is a successful partial trial, not a complete quickstart verification.

N is derived without a model: select the first compatible fenced shell block in
each heading containing “Quickstart” or “Quick start”. If that section has Linux,
macOS or Windows subsections, select the matching platform subsection first.
If no matching subsection exists, use the section's own compatible block before
any platform subsections; never borrow another platform's block.
Only its first compatible block counts; alternate prerequisite routes and optional
installation blocks are not additional required steps. POSIX blocks use `sh`,
`bash`, `shell`, `console` or `zsh`; Windows blocks use `powershell` or `ps1`.
Without a quickstart heading, use the README's first compatible shell block.
Each nonblank, noncomment logical command line is one step; joined continuation
lines and a compound command count as one. `$ ` and `PS> ` display prompts are
stripped. Exact commands or exact executed compound segments establish coverage;
independent checks and agent opinions do not count. An agent's substitution of
`;` for a documented Windows `&&` is not exact coverage
of that declaration. The substituted command can succeed while the original
PowerShell 5.1 command remains unsupported. The k count is matched declarations,
so a partial result can include successful attempts at alternative commands.
`readme_coverage` records N,
k and per-step line/evidence status. If no count can be derived, stdout explicitly
says coverage is unavailable and the result remains PASS (partial).

Failed, timed-out, or truncated trial/check commands mean FAIL (exit 1).
Runner refusals mean INCOMPLETE (exit 2), stop the trial after the denial, and are
filed as TOOL limitations. They never establish a DOCS defect. Missing SSH opt-in
is rejected before creating output when neither acknowledgement flag is present;
a fixed read-only platform probe names the flag required by the chosen target;
platform-specific validation still occurs on the target when one flag is present.
Missing checks, infrastructure/agent/protocol errors, exhausted budgets, and
cleanup failures mean INCOMPLETE (exit 2). Model findings cannot change a verdict.
Reports separately show **declared success checks passed** and **every attempted
README step succeeded**, with failed commands/segments and their exit codes near
the verdict. Stdout FAIL details also name the failing README step or agent command,
the full command, real failing segment exit and first two lines of output; failed independent checks are
named. Top-level compound segment statuses are captured out of band before trailing
echoes can replace them; a failed segment makes the command FAIL even
if a later segment succeeds. Full PASS covers the derived quickstart declaration;
it does not prove that optional commands elsewhere in the README were attempted.
Timing fields are `minutes_to_first_successful_command` and
`minutes_to_first_declared_check_success`; neither implies full README coverage.

To start at an old public commit, add `--ref` with its full 40-character identity.
`rerun` uses latest HEAD, retaining the exact agent, targets, limits, and resolved
checks. The initial pin remains recorded, but does not pin the rerun. Remote input
must be credential-free HTTPS; local input uses committed HEAD. Uncommitted work
is excluded. The controller disables Git helpers, hooks, and SSH transport while
creating a bounded immutable source bundle. The target clones that bundle.

Each target directory contains `findings.json`, `report.md`, `commands.jsonl`, and
`result.json`; the parent has `run.json`. Logs record setup, agent responses,
commands, denials, time/output limits, independent checks, and cleanup confirmation.
An `installation-rewrite` row describes the same execution as the following
original `command` row, including spacing-only rewrites; it is metadata, not a
second attempt, and is excluded from coverage and verdict counting.
Reruns add `diff.json` both at the output root (keyed by target folder) and inside
each target folder, with added, resolved, no_longer_raised, changed, and unchanged
finding IDs. A resolved finding requires its exact command to have failed in the
previous trial and now run successfully without another failure. An omitted
evidence-backed failure is only no_longer_raised. Unevidenced opinions, positive
notes and earlier refusals remain unassessed and never enter the change lists.
An INCOMPLETE rerun has comparison status `incomplete`, puts previous findings in
`unassessed`, and leaves all change/progress lists empty. Protocol, infrastructure,
budget or cleanup failures cannot establish resolution or absence of a finding.
Findings are filed locally, never posted as hosted issues. Review them with their
command evidence. Reports label advisory interpretations unverified and link
matching command evidence. A U+FFFD/mojibake claim contradicted by the independent
README read is marked contradicted; other corruption claims remain unverified.
A resolved command failure does not independently verify every advisory interpretation.
Rerun executes saved trusted commands: never rerun someone else's results folder.

## What has been verified

This table describes live release-preparation runs, separate from fake unit tests.
See [DEMO.md](DEMO.md) for outcomes and limits.

| Component | Live evidence | Status |
| --- | --- | --- |
| Linux SSH target | Fresh Debian workspace, explicit acknowledgement, limits, command inspection, cleanup | Unconfined; trusted code only |
| LXC target | Fresh stock Ubuntu, no profiles/NIC, proxy, resource limits, cleanup | Verified |
| Claude Haiku 5.5 | Schema-enforced command proposals on Debian and Windows SSH | Live; scripted verdicts |
| Codex Luna | Direct-routing restricted adapter; README commands on Ubuntu LXC and macOS SSH | Live; scripted verdicts |
| Learning loop | Old/current public README trials, exact checks, rerun diff | Linux, macOS, Windows |
| macOS SSH target | Stock Python 3.9.6, trusted runner, group kill, verified cleanup | Trusted mode only |
| Windows SSH target | cmd SSH, PowerShell runner, taskkill tree cleanup | Trusted mode only |
| OCI target | Docker and Podman unavailable in the verification environment | Unsupported |
| Python 3.11 and 3.14 | Managed interpreters; clean-clone unit suite and installed CLI quickstart | Verified locally |

The round-four stranger controller had only system Python 3.14, so that tester
did not run Python 3.11. Reproduce the release check using a uv-managed interpreter:

```sh
uv run --no-project --python 3.11 python -m unittest discover -s tests -q
uv run --no-project --python 3.14 python -m unittest discover -s tests -q
```

## Safety

- A fresh disposable target is mandatory. Repository commands never run on the
  controller. Untrusted code belongs in containers or VMs; SSH is trusted-code only
  and exclusively for machines you own. There is no host-run target.
- Never mount a real home, SSH keys, model/cloud credentials, or a container socket.
  Never touch existing browsers, browser profiles/ports, services, timers, or tasks.
- LXC denies direct networking and allows exact repository
  and PyPI/npm/crates/Debian/Ubuntu endpoint domains through a loopback proxy.
  Non-public addresses and non-web ports are refused; setup failures fail closed.
- Treat repository text, docs, logs, and model responses as prompt injection.
  Parse only the fixed action protocol. Success checks and verdicts are script-owned.
- Defaults: 60 seconds per command, 600 per target, 64 KiB command output, 512 MiB
  per-process address space, 1 GiB workspace tmpfs, 300 CPU seconds, 20 agent turns.
  Memory/disk/CPU settings apply only to Linux containers, never SSH targets.
  LXC also caps container CPU/RAM/processes and root disk growth. SSH enforces time/output bounds, not aggregate host quotas. Kill process groups on time/output limits.
- Deny credential paths, workspace escapes, privilege escalation, administration,
  remote commands, and reserved browser ports. No system package installation on
  SSH. LXC OS isolation is the boundary. SSH filtering supplies
  refusals only; it cannot constrain arbitrary repository code.
- Destroy only resources created by this run, in cleanup even after failure.
  Verify removal. Cleanup failure is INCOMPLETE. Forced controller death or a lost
  connection can leave an owned target; use its exact name from private evidence.
- Keep raw results private: they may contain sensitive repository content. Review
  and scrub them manually before sharing. See [SECURITY.md](SECURITY.md).

## Development

```sh
python3 -m unittest discover -s tests -q
```

Unit tests use fake targets and agents, no real models, SSH, LXC, or OCI. Small
local subprocess tests exercise bounds. Fake sensitive values are built at runtime.
Optional checks: `uvx ruff check --no-cache readtrial tests` and
`uvx mypy --ignore-missing-imports readtrial`.

## Limits and limitations

Linux controllers support confined LXC targets and explicitly acknowledged Linux/macOS/Windows
SSH targets. Native macOS/Windows controller trials remain unverified. OCI execution is unsupported
in this release-preparation environment and has no advertised quickstart.
Private repositories, browser/UI testing, deployment, and VM orchestration are
unsupported. Large source bundles/histories, missing or oversized README text,
malformed findings, and failed target setup make a trial INCOMPLETE.
Linux SSH is not filesystem, network or aggregate host isolation. Containers share a kernel
and are not VMs. Allowed endpoints can receive submitted repository data; a proxy
cannot prevent that. On LXC, clients ignoring proxy variables fail without a direct route.
A weak agent may invent findings or miss failures; stable IDs do not validate its
interpretation. Inspect evidence. Top-level compounds have separate step evidence, but a command can
hide failures inside scripts, substitutions or opaque control blocks; submit
those steps separately when individual status evidence matters. A recovered
nonzero segment (`false || ...`) still counts as a failed attempted step.
The live loops in DEMO.md report actual scripted outcomes. They establish the
declared offline/help checks and attempted commands, not browser behavior or
complete audit workflows. Earlier failed attempts remain part of the evidence.

## License and ideas

MIT, copyright TayHCode. Concepts only from
[quickstarted](https://github.com/snehankekre/quickstarted) (MIT), for doc-following
and independent success scripts, and [Repo2Run](https://github.com/bytedance/Repo2Run)
(Apache-2.0), for isolated build attempts. No code was copied; no AGPL code is included.
