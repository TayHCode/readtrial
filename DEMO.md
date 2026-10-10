# Demo and live verification

## Offline FAIL → PASS sample

From the checkout, run:

```sh
python3 -m readtrial demo --out trial-results/demo
```

After installing the entry point, `readtrial demo` uses `trial-results/demo` by
default. Choose a new output folder for each run. This is an offline fake agent
and fake target: no model, subprocess, Git fetch, SSH, LXC or repository command
runs. It finishes in under 20 seconds. The emitted reports and diff are real files.

Actual sample output from the 2026-10-10 verification:

```text
OFFLINE DEMO: fake agent and fake target; no model, SSH, LXC or repository execution.
Before: FAIL (README step exit 3; declared check failed)
After: PASS (README step exit 0; declared check passed)
diff.json: 1 resolved finding; evidence in before/ and after/.
```

The generated `before/report.md` names the failure:

```text
Script verdict: **FAIL: README step 1, segment 1 (exit 3): example --help [failed: example --help]; declared check 1 (exit 3): example --help**
Declared success checks passed: **False**
Every attempted README step succeeded: **False**
Failed README command (exit 3): `example --help`
- Failed segment 1 (exit 3): `example --help`
```

`after/report.md` reports PASS with both booleans true. `diff.json` records the
resolved finding under its controller-assigned `failure-` identity. This demonstrates the evidence layout;
it is separate from the real model and target results below.

`run --dry-run` and `rerun --dry-run` print their configuration and planned actions
without fetching source, calling a model, contacting a target or creating output.

## Real old/current README loops

On 2026-10-10 the fixed adapters ran these loops sequentially from a Linux
controller. Immutable local Git copies from the earlier public fetch were reused;
no new fetch or hosted issue was needed. Every current run used `rerun` with the
same agent, target, limits and independent checks as its old run.

| Target / agent / repository | Old script verdict | Current script verdict | Independent check |
| --- | --- | --- | --- |
| Stock Ubuntu 24.04 LXC / Codex `gpt-6-luna` / cdpidletabs | PASS | PASS | `python3 close_idle_tabs.py --help` |
| Owned macOS trusted SSH / Codex `gpt-6-luna` / release-check | FAIL | PASS | `uv run --no-project --python 3.12 python release_check.py --help` |
| Owned Debian trusted SSH / Claude `claude-haiku-5-5` / release-check | FAIL | PASS | `python3 release_check.py --help` |
| Owned Windows trusted SSH / Claude `claude-haiku-5-5` / cdpidletabs | PASS | PASS | `py -3 close_idle_tabs.py --help` |

Exact repository identities:

- [release-check](https://github.com/TayHCode/release-check): old
  `44da6f236e9ab535697712fcce7ad7c86cc9d092`; current
  `22bb027a7517fd36bdbc529d95a1605a4099a3ea`.
- [cdpidletabs](https://github.com/TayHCode/cdpidletabs): old
  `8e6df07ad072c34e9c59ac871c158d354f521d30`; current
  `52b41a74859348002d8ea28a9c17c04bd17d0466`.

Luna proposed and executed real README help commands on both LXC pins. These
PASS results cover the attempted offline/help route; they do not establish that
an unattempted venv or browser workflow would succeed. The current Mac run used
the README's official uv installer, rewritten into the owned work folder, then
its managed Python 3.12 help route. Both the command and the identical independent
uv check passed. The old Mac pip/direct-help steps failed on stock Python 3.9.6;
its uv check also failed because uv had not been installed in that fresh folder.
The report names those failed steps and the failed check separately.

Haiku's old Debian venv step failed because ensurepip was missing. Direct help and
its independent check passed, but the script still returned FAIL because a README
step failed. The current Debian version/help commands and check all passed.
Windows Haiku used the Python launcher and PowerShell syntax; documented help
commands and their independent checks passed on both repository pins.
Weak-model advisory findings did not determine any of these verdicts. Models
previously changed finding IDs and sometimes invent interpretations. The controller
now assigns deterministic command/evidence identities; resolved IDs alone do not
establish every advisory interpretation. Current reruns write the diff at the output
root and per target.

Codex CLI 0.162.0's Luna catalog used `tool_mode=code_mode_only`, overriding the
adapter's disabled feature switch. The fixed adapter uses a private direct-routing
catalog for the same model, removes patch/search/experimental capabilities,
disables native agent/input tools, and reinforces the JSON action protocol.
Claude Code 2.1.295 and Codex both use strict output schemas and one corrective
retry. Transcript regressions include the exact disabled-Code-Mode diagnostic,
legacy command aliases, serialized JSON commands, and premature command findings.
Unknown errors are reported explicitly; a diagnostic never substitutes for a command.

## Real controlled failure and text probes

A scripted adapter, separate from the model trials, deliberately submitted failing
commands on the owned Windows target. Exits 7, 9 and 5 remained failures despite
trailing output, a succeeding pipeline stage and an `if ($?)` guard. The unchanged
`py -3 --version` independent check passed. An actual report excerpt is:

```text
Script verdict: **FAIL**
Declared success checks passed: **True**
Every attempted README step succeeded: **False**
Failed README command (exit 7): `py -3 -c "raise SystemExit(7)"; Write-Output done`
- Failed segment 1 (exit 7): `py -3 -c "raise SystemExit(7)"`
```

These intentional failures test readtrial's evidence and verdicts; they are not
repository defects. A UTF-8 file containing an en dash and curly apostrophe was
written and read through the real Windows runner without replacement characters.
Unit regressions also exercise UTF-16/OEM decoding, POSIX `&&`, `||`, semicolon and
pipeline status capture, permitted venv/installer compounds, and offending-segment
refusals. PowerShell status logging preserves the previous `if ($?)` decision.

## Boundaries and retained evidence

LXC is the live verified confined target: fresh unprivileged instances, no inherited
profiles or NIC, private writable workspace, resource controls and an exact-domain
proxy. Every SSH target uses user permissions without filesystem/network
confinement. Linux SSH requires its explicit acknowledgement and prints the
unconfined notice; macOS/Windows require `--trusted-target` and print their notice.

Owned work folders, tracked process groups/trees and exact LXC instance names were
removed, with cleanup confirmations in the logs. Installer/Python/cache files stayed
in owned work folders. No owner uv installation was removed. No Chrome, browser
endpoint/profile, service, timer or scheduled task was used. Forced cancellation
left an owned LXC instance in earlier attempts; its identity was recovered from
its command evidence, and only that exact instance was removed and absence checked.

Earlier failed/incomplete adapter-development runs and the stranger reports remain
in private evidence outside this repository. They exposed tool defects, model
mistakes and tester mistakes separately: a Windows-only LXC check and a Mac check
using the unsupported system Python were invalid checks for their intended routes.
Historical Linux SSH namespace results do not describe today's unconfined backend.

These results verify attempted offline/help paths, failure reporting and cleanup.
They do not verify browser behavior, complete audit workflows, malicious-code
containment on SSH, native macOS/Windows controller runs or OCI. Docker/Podman
were unavailable for OCI verification. See [README.md](README.md) and
[SECURITY.md](SECURITY.md) for the target boundaries and limitations.

## Round-four controlled rerun verification

On 2026-10-10, fresh owned greeting fixtures rechecked Luna on Ubuntu LXC and
trusted macOS SSH, and Haiku on acknowledged Debian SSH and trusted Windows SSH.
Luna's in-clone parent-path command passed on both targets. A POSIX quickstart
followed by only a Windows subsection retained full coverage on Linux and macOS.

Haiku executed an intentionally unsupported version flag on Debian and Windows.
Each unchanged rerun retained the controller-assigned failure ID; Windows had two
unchanged reruns. Differences in advisory wording appeared as changed, without
phantom added/no_longer_raised entries for the recorded failure. Adding the version
flag to each fixture then produced PASS and one evidence-backed resolved failure.
An unrelated model opinion omitted on Debian stayed no_longer_raised, not resolved.
All these runs confirmed owned-resource cleanup.

A separate scripted Windows probe reproduced PowerShell 5.1's rejection of a
README `&&` command. Its `;` alternative succeeded but retained partial exact
README coverage. One command row and its spacing-only rewrite row describe one
execution with identical output and segment evidence. This distinction is now
explicit in the README. Fake transcript tests independently reproduce malformed
protocol responses and verify that incomplete reruns leave findings unassessed.
