# Changelog

## 0.1.0 — 2026-10-09

- Initial standalone release with a JSON-only weak-agent CLI protocol.
- Fresh LXC, confined SSH and optional local-container target lifecycles.
- Bounded command evidence and script-only success checks.
- Advisory findings, Markdown reports and rerun finding diffs.
- Standard-library runtime; unittest suite with fake targets and agents.

## Unreleased — hardening

- Unwrap one serialized command action, log corrective retries separately, and
  recover from one malformed response before stopping a trial.
- Ignore an invented cd around an exact README command with an AGENT deviation
  note; distinguish other agent failures from documented README failures.
- Refuse absent checks before the agent loop, abbreviate temporary/account paths
  in evidence, keep Linux SSH notices consistent, and leave unevidenced model
  opinions out of finding comparisons.

- Evidence-derived stable failure IDs across reruns, migration of earlier model
  IDs, and unassessed comparisons for incomplete trials.
- FAIL stdout names failed steps, commands and real segment/check exits; protocol
  errors omit raw CLI envelopes. Quickstart coverage falls back to the section's
  own platform-compatible block, and Debian/Ubuntu venv prerequisites precede use.
- Full versus partial quickstart coverage on stdout and in reports, shell-specific
  subdirectory guidance, physical in-clone parent-path checks, and TOOL-only runner
  refusals that stop with INCOMPLETE.
- Raw Windows native-output capture, real rewrite exits, successful-command
  deduplication, explicit success timings, and evidence-backed rerun resolution.
- Missing SSH acknowledgement refusal before output creation and an explicitly
  labelled expected Windows POSIX-probe failure.

- Canonical command aliases, strict first-command schemas, one corrective retry,
  explicit scrubbed protocol errors, and Luna catalog routing compatibility.
- Out-of-band compound step exits, named failing commands, separate declared-check
  and attempted-README-step outcomes, and UTF-8 PowerShell evidence.
- Explicit Linux SSH acknowledgement, honest unconfined target labels, configurable
  LXC instance prefixes, root rerun diffs, friendly errors, dry runs and offline demo.

- Restricted Codex text adapter, fixed SSH platform probes, stronger privilege and
  temporary-path denials, explicit LXC cleanup verification.
- Full initial source pin with rerun against latest HEAD and preserved checks.
- Expanded CLI quickstarts, verified/unsupported matrix, and live Linux loop evidence.
- Explicit trusted-target macOS/Windows SSH execution, fresh temp work folders,
  command inspection, time/output bounds, process-tree termination, and cleanup.
- Prominent trust notice and report labels; documented absence of OS confinement,
  networking isolation, memory/disk/CPU quotas, and detached-process guarantees.
- Fake platform/refusal regressions and real old/current README learning loops.

- Work-folder official uv installers are rewritten and validated, managed Python
  and cache/tool destinations are forced local, and pip installs require a work
  venv. Child PATH includes owned bin and venv executables; browser ports 9222–9226
  remain refused. Stranger trials stop at the first offline README goal.
