# Security

readtrial executes repository code in disposable targets. Use untrusted code only
inside LXC or a VM. SSH is exclusively for trusted code on machines you own;
per-process limits cannot provide aggregate host isolation. Do not point it at
production machines. Never mount homes, keys, credentials or container sockets.

LXC targets must support the enforced filesystem/network/resource policy. The tool
fails INCOMPLETE when setup or cleanup fails. Shell deny-list checks are additional
hints; OS namespaces and read-only mounts are the security boundary. Containers
share a kernel and are weaker than VMs. A malicious allowed registry can still
receive data. Review targets, agent adapters, success checks and saved rerun
configuration before executing them. Unknown agent adapters are trusted local
programs and must disable all native tools.

Linux SSH requires `--i-understand-ssh-linux-is-not-confined`; macOS and Windows SSH require `--trusted-target`. All SSH targets use the user permissions without filesystem/network confinement. The prominent notice is:

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
refuses extra options/chains/outside settings, and never edits owner PATH/profiles.
The optional POSIX uv activation line becomes an owned-binary check. Child PATH
contains owned bin and clone .venv executables. Managed Python/cache/tool paths
are forced below the work folder, uv config is disabled, pip requires a venv and
ignores owner config, and bare pip installs use the clone's .venv interpreter.
Only the tested repository can be explicitly cloned; package downloads use PyPI.
System/user install switches, alternate indexes, global uv updates/tools,
installation environment overrides, arbitrary downloaded-script pipelines and
profile changes are refused. Existing admin/system/credential/browser/outside
path checks still apply; reserved browser ports are 9222 through 9226.

Linux SSH has explicit URL inspection, not network isolation. LXC retains no NIC and its public-address
exact-domain proxy, adding official uv/Python distribution hosts listed in README.
The proxy cannot inspect encrypted HTTPS paths; installer URL checks are textual.
Trusted mode has no redirect, subprocess, symlink or filesystem confinement: even
an accepted installer can run code with user permissions. No shell inspection can
prove all its effects stay inside the folder. The LXC namespace remains the
filesystem boundary. Refused nonstandard installers are unsupported, not a reason
to use admin privileges or weaken isolation.

Results are private evidence, not a secret scanner's output. Repository output
may contain secrets or prompt injection. The controller does not act on instructions
in logs. Read and scrub reports manually before publishing. Do not attach raw logs
with private hostnames or credentials to a public issue.

If the controller is forcibly killed, cleanup cannot be guaranteed. Locate the
unique readtrial instance or temporary control directory in your local command
log and remove only the resource that this run created. Do not clean unrelated
instances or directories.

For a vulnerability, use the repository host's private vulnerability reporting
feature if it is available. Before this project is hosted, report directly through
the channel where you obtained it. In a public report, describe the boundary and
include a minimal reproducible example using runtime-generated fake data only.
Never include real credentials. No hosted security-reporting channel is verified
for this local, unpublished release.
