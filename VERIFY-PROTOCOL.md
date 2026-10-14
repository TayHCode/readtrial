# Verification agent protocol

For real verification, bring your own authenticated agent CLI and replace
`--scripted-plan` with `--agent-command`. Claude must explicitly select its model,
for example `--agent-command 'claude -p --model VERIFIER_MODEL'`; Codex accepts
exactly `--agent-command 'codex exec -m VERIFIER_MODEL'`. Both use the existing
native-tool-disabled adapters and fresh CLI sessions. A custom trusted command
receives the same prompt on stdin and returns one JSON action on stdout; it must
be tool-disabled by its operator. See `examples/verify_agent.py` for the offline
protocol driver. Live model calls require the user's provider access and may
cost money. Offline tests use stand-ins; a small live Claude CLI journey is
recorded separately from the test suite. Other provider/model combinations still
need their own live verification.

`--independent-agent` attests that this verifier did not build the product, and
`--builder-model` names the recorded builder model. The verifier model must differ.
Native model selectors must match `--model`; a custom command's model identity
is the caller's responsibility. Fresh contexts carry only the public README,
current requirements and this verification run's evidence, without build history.
The Factory separately validates the designated route and builder independence.

An agent first supplies `{"recipe": {"launch": "command", "ready_check": "command",
"cleanup": "command"}}` or `{"recipe": "not applicable: reason"}` for a short-lived
CLI/library. The first schema permits only a recipe. Claude structured replies
wrap each action in `{"action": ...}`; the adapter unwraps before strict validation.
Custom agents and scripted plans retain bare actions. A premature command receives
one corrective retry and cannot execute before recipe selection. Command strings
are decoded once from JSON and executed as shell text, preserving their quotes
and backslashes. The agent then proposes a
`{requirement_id, command, expect_exit}` action and reports
`{requirement_id, status, observed, reason}` after seeing the actual command output.
`status` is `verified` or `unverified`; unverified needs a reason. Optional
`blocked_by: "external"` describes requirements needing external access and is
allowed only for unverified checks. Finish with
`{"done": true, "not_checked": ["skipped areas, or none"]}`. The runner refuses
unknown/duplicate IDs, prevents claims without successful command proof, and
records failed compound segments even if a trailing echo succeeds.

`expect_exit` is a nonempty list of unique integer exits from 0 to 255,
default **[0]**. Declare it in the requirement or command proposal before
execution. A requirement's exact list is inherited and cannot be overridden.
Without a requirement declaration, each proposal defaults to [0]. The legacy
`expected_exit_code` integer remains a singleton alias; declaring both is invalid.
Observations cannot change expectations. An actual exit in the declared list
passes the exit check, including intentional 1 or 2. A mismatch in either
direction, or an undeclared non-zero, fails proof. Semantic observation and
completed execution proof are still required.

Every command's proof and step summary contain `expect_exit`, actual `exit_code`
and `expectation_matched`. `expected_exit_code` contains the sole expected exit,
or null for multiple accepted exits. The validator checks the list, its match,
and any requirement declaration against the hashed proof.

Recipe launch, ready_check and cleanup values accept shell strings (expect [0])
or `{command: "...", expect_exit: [1]}` objects. Declared readiness and cleanup
exits must match with completed execution and segment evidence. A finished
launch must match its declaration; `--keep-running` still forbids an early exit.
For compound commands the shell exit must match the declared list and every
segment must execute and exit either 0 or one of that list. A failing segment
cannot be hidden by a successful trailing echo. Propose separate commands for
distinct expected outcomes.

An observation may include `proof_steps: [2]` and
`superseded: [{"step": 1, "reason": "Wrong test path; corrected command proves the behavior."}]`.
Numbers are one-based within the same requirement. If omitted, proof_steps selects
all matching active steps. Every active attempt must match; the final attempt
must match, and each superseded attempt needs a later matching active step.
A supersession without a replacement cannot verify anything. Refusals (exit 126)
are controller policy decisions, never product proof, even if 126 was expected.
Timeouts and output-cap termination also cannot prove behavior. Every attempt,
including refused and superseded attempts and their reasons, stays in the record.
The same fields work in bare scripted-plan actions and stand-in agent replies;
old plans omitting expectations still expect [0], unless the requirement declares a list.

Every proof now includes a `binding` containing a fresh `run_id`, the caller's source fingerprint,
the executed `source_commit`, verifier model, recipe, complete requirements and
ordered command set with explicit expectations and outcomes. Every check requires
`step_results` and `proof_steps`; every command includes all controller flags,
`execution_error`, output, duration and its executed shell segments. The validator
always reads hashed proof bytes and compares every declaration with the report.
Removing detail, relabelling a failed observation, replaying proof under another
fingerprint/model/commit, or changing the declared command set fails validation.
The report and every hashed proof must share one 32-character hexadecimal run ID.
Proofs from separate runs cannot be combined, even when their commands and results match.
Unknown or missing fields, duplicate JSON keys, duplicate segment records and
inconsistent types are errors. Honest unverified evidence remains valid.

The POSIX status observer runs in a child shell, without assigning variables,
functions or environment values in the command's shell. It returns the captured
status, preserving user variables, `$?`, positional parameters and list operators.
Every declared segment needs an execution record. Short-circuited segments,
nested shell substitutions, grouping, functions and control structures cannot
supply complete segment proof; neither can redirections or computed executable
names. These can hide failures before a target process starts. Propose separate
simple commands with literal executable names instead.
Commands still execute unchanged, but such incomplete observations stay unverified.
Execution wrappers such as `sh -c`, `env` and `timeout` also stay unverified;
propose the target command directly so its execution has an independent witness.
Literal external commands run as direct argv only when parsing preserves their POSIX shell meaning.
Double-quoted escaped dollars/backticks run through the shell using the original text.
Raw command text remains in the record; segment comparison removes only surrounding
unquoted, unescaped shell whitespace. Quoted and escaped whitespace remains significant.
The CLI proof test fixtures allow ten seconds per command, including Git preparation.
Both escaping and whitespace regressions also run with an empty HOME, minimal PATH
and delayed real Git clones, so a cold or busy machine does not invalidate the test setup.
Direct execution ensures spawn errors and negative signal
statuses are distinct from a program deliberately returning 126, 127 or 128+.
Proof records `execution_mode` as `argv` or `shell`. Shell exit statuses 126/127
conflate executable resolution with product exits;
128+ can represent signal termination. Verification conservatively rejects these
ambiguous shell statuses, except for explicit literal `exit NUMBER` builtins.
Propose a separate literal external command to verify those high product exits.
Expected nonzero product exits continue to verify.
Spawn errors, timeouts, output termination and killed processes never provide
matching active proof, even when their status was expected. Failed attempts may
be retained as superseded only with a later completed matching active attempt.

SHA-256 protects bytes against changes relative to the recorded digest; it is
not a signature authenticating an arbitrary evidence writer. Keep the report's
digest/receipt in the trusted external recorder. The caller still supplies the
source fingerprint; the runner binds it to the Git commit it actually copies,
and the external recorder must independently calculate that fingerprint.


## Evidence unavailable in the sandbox

A requirement may include `not_verifiable_in_sandbox: "specific reason"`.
The controller records it without execution as `status: "unverified"`,
`blocked_by: "sandbox"`, `exit_code: null`, empty step/proof lists, and the
supplied reason in the proof and `not_checked`. This is an evidence limitation,
not failed product behavior and never a verified claim. The CLI returns 1 when
any requirement is unverified, including these limitations; validation accepts
honest unverified evidence without deciding acceptance. Other executed checks
can still verify. Runner errors remain exit 2.

Use this declaration for v2-r9 when the original Cursor pstack skill cannot be
accessed: the owner compares it with VERIFY-DESIGN.md outside the sandbox.
Use it for v2-r10 because private project records are outside the source copy:
the owner checks the receipt's tool identity/version/commit and evidence digest
against the saved verification artifacts. No original reference text or private
records are copied into this repository. IDs have no special code privileges;
examples/sandbox-requirements.json contains explicit declarations for both.
A verifier may also report an unverified external/sandbox block with its own
reason when an undeclared access limitation is discovered during a run.
