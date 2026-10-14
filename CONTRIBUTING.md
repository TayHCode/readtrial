# Contributing

Keep changes small and focused. Runtime code uses Python 3.11+ and only the
standard library. Do not add a model SDK or copy code from another project without
reviewing its license. MIT is the project license.

Run `python -m unittest discover -s tests -q`. Unit tests use fake targets and
agents, never real models, SSH or LXC. Generate any fake secret data at runtime.
Optional development checks are Ruff and mypy, run through uv when available.
Keep real trial evidence outside the repository and scrub anything shared.

For target changes, explain the filesystem, credential, network and resource
boundaries, plus the failure/cleanup behavior. Unsupported container boundaries
must fail INCOMPLETE. Every SSH mode must report its weaker boundaries and require
the documented explicit opt-in. Agent opinions must never change a script verdict.
Cover meaningful failure cases with fake runners before doing a separately authorized real trial.

The release-check manifest declares the exact README install and first-run
commands. Keep its line references synchronized when editing the README. Review
privacy/license findings and any public attribution metadata before release;
passing automated checks alone does not authorize publication.

After committing packaging or quickstart changes, run
`python3 scripts/check_offline_install.py`. It checks committed HEAD in a fresh
local clone, HOME and venv with pip indexes and caches disabled, runs every
declared quickstart/check, and verifies the installed import and entry point
outside the checkout. Evidence and temporary files stay under `.scratch/`.
