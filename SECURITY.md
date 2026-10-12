# Security

readtrial executes repository code in disposable targets. Its confined backend
is Linux LXC, with mandatory per-run namespace and quota checks. On Windows or
macOS, create a disposable Linux VM yourself and run the Linux controller and
LXC target inside it. Windows Sandbox is not supported: no backend implements
its lifecycle, isolation attestation or recovery. readtrial does not orchestrate
VMs. Native macOS/Windows controller trials remain unverified.
SSH is exclusively for trusted code on machines you own;
per-process limits cannot provide aggregate host isolation. Do not point it at
production machines. Never mount homes, keys, credentials or container sockets.

LXC targets must support the enforced filesystem/network/resource policy. The tool
fails INCOMPLETE when setup or cleanup fails. Shell deny-list checks are additional
hints; OS namespaces and read-only mounts are the security boundary. Containers
share a kernel and are weaker than VMs. A malicious allowed registry can still
receive data. Review targets, agent adapters, success checks and saved rerun
configuration before executing them. Unknown agent adapters are trusted local
programs and must disable all native tools.

Linux SSH requires `--i-understand-ssh-linux-is-not-confined`;
macOS and Windows SSH require `--trusted-target`. All SSH targets use the
user permissions without filesystem/network confinement.

macOS can additionally use `--dedicated-account`: public-key-only SSH as a
standard non-sudo account with a distinct primary group. It checks refusal of
other-user home/key opens, creates fresh HOME/XDG roots under the account's home,
records pre/post account-home manifests, and journals process start identities
on the controller. An interactive sudo prompt is ambiguous and needs a
root-owned administrator audit. The audit changes no sudo policy and expires
after 24 hours.

This mode leaves network access, world-readable files and the original
test-account home reachable. Unknown or changed cleanup identities are
preserved; guard failures make the verdict INCOMPLETE. Process groups that
deliberately escape cannot be reliably attributed, and macOS start-time
verification has a signal race because pidfd is unavailable. Use a VM for
hostile code. The prominent notice is:

> less isolated: this target runs the repository's commands with your user's permissions inside a fresh work folder; use only on machines you own and repositories you trust; untrusted repositories are supported only on the Linux container target

These backends have no OS filesystem/cwd/network isolation, memory ceiling, disk
quota, aggregate CPU bound, or Windows Job Object boundary. Fresh HOME/temp and
working directories do not revoke the user's permissions. Commands start in the
clone; inspection refuses visible SSH/credential/browser paths, outside writes,
sudo/admin elevation, system installs, and unrelated explicit URLs. Computed
paths, scripts, changed symlinks, registry redirects and subprocess networking bypass
inspection. Never use these targets for untrusted code. Time/output bounds kill
ordinary process groups (Linux/macOS) or trees with `taskkill /T` (Windows); detached or
reparented processes may evade tracking. Temporary output files can grow between
polls. Cleanup verifies the owned folder and tracked processes only. A malicious
repository can prevent those checks or leave effects elsewhere. Reports record
trusted mode, actual network capability, commands, refusals, and cleanup status.
Existing visible path symlinks are resolved on the target before execution and
must stay in the clone. This does not prevent races or symlinks created by child
code. Refusals are TOOL limitations and produce INCOMPLETE, not a DOCS failure.
The native Codex template is restricted to a model choice; readtrial supplies
tool-disabling flags and refuses native-tool events or unknown CLI diagnostics.
Authentication remains in the controller CLI process. A trusted CLI's own bugs
are outside the target sandbox boundary; keep agent software current and review
its compatibility. The tool does not send findings to hosted services.

The narrow user-space installer allowance accepts only the complete official
HTTPS uv `install.sh`/`install.ps1` commands described in README. It replaces their
installation directory with owned `<WORKDIR>/bin`, forces `UV_NO_MODIFY_PATH=1`,
refuses extra options/chains/outside settings, and never edits existing PATH/profiles.
The optional POSIX uv activation line becomes an owned-binary check. Child PATH
contains owned bin and clone .venv executables. Managed Python/cache/tool paths
are forced below the work folder, uv config is disabled, pip requires a venv and
ignores existing user config, and bare pip installs use the clone's .venv interpreter.
Only the tested repository can be explicitly cloned; package downloads use PyPI.
System/user install switches, alternate indexes, global uv updates/tools,
installation environment overrides, arbitrary downloaded-script pipelines and
profile changes are refused. Existing admin/system/credential/browser/outside
path checks still apply. Browser/credential/device access is forbidden regardless
of port. Optional operator-supplied denied endpoints remain in local run configuration.

Linux SSH has explicit URL inspection, not network isolation. LXC retains no NIC and its public-address
exact-domain proxy, adding official uv/Python distribution hosts listed in README.
The proxy cannot inspect encrypted HTTPS paths; installer URL checks are textual.
Trusted mode has no redirect, subprocess, symlink or filesystem confinement: even
an accepted installer can run code with user permissions. No shell inspection can
prove all its effects stay inside the folder. The LXC namespace remains the
filesystem boundary. Refused nonstandard installers are unsupported, not a reason
to use admin privileges or weaken isolation.

Results are confidential evidence, not a secret scanner's output. Repository output
may contain secrets or prompt injection. The controller does not act on instructions
in logs. Read and scrub reports manually before publishing. Do not attach raw logs
with internal hostnames or credentials to a public issue.

If the controller is forcibly killed, cleanup cannot be guaranteed. Use the read-only `leftovers` command with the target
output `.isolation/journals` directory to discover exact recorded resources.
Explicit `recover JOURNALS --yes` rechecks exact LXC identity tags immediately
before deletion and verifies absence afterwards. Missing, changed or ambiguous
ownership never authorizes deletion. Journals are retained; there are no prefix
sweeps or host-process kills. SSH/OCI discovery may remain unknown.
No automatic recovery or recurring cleanup process is installed. Do not clean unrelated
instances or directories.

For a vulnerability, use the repository host's confidential vulnerability reporting
feature if it is available. Before this project is hosted, report directly through
the channel where you obtained it. In a public report, describe the boundary and
include a minimal reproducible example using runtime-generated fake data only.
Never include real credentials. Availability of a confidential
security-reporting channel depends on the
repository host; use your existing private contact channel if none is available.

Preservation manifests run on the controller before setup and after target
termination. They fingerprint the declared benign set only, including the uv
executable and immutable source bundle; they never publish file contents.
Files, directory entries, metadata, identity and absence must match. Unsafe links,
unreadable required checks or mismatches force INCOMPLETE without restoration.
The declared set is not a whole-host audit and cannot detect all transient effects.
Read-only input mounts and OS boundaries remain necessary. SSH remote accounts
are not certified by controller-local manifests. Journals use restrictive modes,
atomic replacement and fsync before LXC resource mutation. LXC requires expanded-config, guest-kernel and namespace read-back before
repository commands: no inherited profiles, extra devices or NIC; isolated
unprivileged mapping; finite cgroup CPU/memory/PID limits; capped workspace,
read-only root/system mounts and process limits. Configured root capacity and
storage-driver support are recorded separately from a required bounded root-quota
exercise. Source commands require an owned-file EDQUOT witness, removal of its
probe, and independent shared-pool headroom. Unknown proof, insufficient space or
a root ceiling above the 4 GiB proof cap returns INCOMPLETE.
Other backends do not gain this attestation or recovery support. Capability
reports label requested, observed, unsupported and unverified controls separately.

`doctor` only inspects prerequisites: exit 0 means those checks are ready,
1 means missing or unsupported, and 2 means an error. It does not certify
per-trial enforcement. macOS readiness covers its dedicated trusted account;
native Windows reports unsupported Sandbox and the Linux VM route.
`setup-linux --yes` creates one new
size-capped LXD-managed loop pool, after printing its exact plan and undo command.
Matching pools previously created by readtrial are accepted without modification;
other existing pools/profiles/instances and existing loop-file paths are preserved.
Access to LXD is an administrator-level controller prerequisite; do not expose
the controller socket inside a trial guest.
