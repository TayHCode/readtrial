# Changelog

## 0.3.0 — 2026-10-14

Compared with public 0.2.0:

- Add `verify`/`validate-verify`, reusable launch/readiness/cleanup recipes, hashed
  source-bound command proof, declared nonzero exits and explicit sandbox gaps.
- Fix Claude schema adaptation, quote preservation, stale-recipe handling and
  bounded diagnostics. Build/install offline with a standard-library backend
  instead of requiring setuptools.
- Record proof for the exact `. .venv/bin/activate` shell segment, retaining
  failure checks and refusing arbitrary source/execution wrappers.
- Permit SECURITY.md fixture text while still refusing the macOS `security`
  executable. Remove obsolete IP approval entries from the scan manifest.
- No public commands, files or user features are removed. The setuptools build
  dependency is removed; the existing README trial interface remains available.

Further changes included in this release:

- Declare accepted `expect_exit` lists in requirements, command proposals and
  recipe phases; bind declarations and exact matches to private proof. Keep
  legacy singleton proposals compatible and reject overrides and undeclared failures.
- Record explicit sandbox evidence limitations without fabricated command failures;
  document owner-side checks for original-skill comparison and private project records.
- Allow vault-kit product commands while retaining credential CLI/token refusals.
- Explain simple-command proof restrictions to the verifier and provide corrective
  feedback when an attempted command cannot prove behavior.

- Add independent product verification with private command proof and reusable
  launch, readiness and cleanup recipes.
- Wrap Claude action unions in an object schema; require a recipe before product
  commands and correct premature proposals once. Keep offline agents compatible.
- Preserve decoded shell quotes and backslashes, and report bounded redacted CLI
  errors in terminal output and saved evidence.
- Mark failed startup recipes invalid instead of rebasing them to new source.
- Explain how Factory records establish tool use and distinguish fallback.

## 0.2.0 — 2026-10-12

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
  venv. Child PATH includes owned bin and venv executables; browser/credential/device access
  remains refused. Stranger trials stop at the first offline README goal.

- Linux isolation preflight, fresh HOME/XDG roots, preservation manifests,
  durable ownership journals, exact LXC recovery and explicit quota-pool setup.
- Dedicated macOS standard-account checks, administrator sudo-denial attestation,
  home guards and controller-journaled process identity with bounded cleanup.
  This remains trusted mode: no network confinement or aggregate resource quotas.
- OS isolation table and disposable Linux VM instructions for Windows; Windows
  Sandbox is unsupported because it has no target backend. Doctor reports the
  supported route without querying or enabling Windows features.
- README shell-fence commands validated against the real CLI parser. Native
  macOS/Windows controller trials and account provisioning remain unverified.

## 0.1.0 — 2026-10-09

- Initial standalone release with a JSON-only weak-agent CLI protocol.
- Fresh LXC, trusted unconfined SSH and optional local-container lifecycles.
- Bounded command evidence and script-only success checks.
- Advisory findings, Markdown reports and rerun finding diffs.
- Standard-library runtime; unittest suite with fake targets and agents.
