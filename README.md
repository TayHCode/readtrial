# readtrial

Test whether a repository works the way its README says. A weak AI agent acts as
an impatient stranger: it sees the README, proposes commands, and records where
it gets stuck. A restricted runner executes those commands in a fresh disposable
target. An independent script decides PASS, PASS (partial), FAIL, or INCOMPLETE.

The result is command evidence, advisory findings, and a learning loop:
**try the README → fix the project or docs → rerun → compare findings**.
No model SDK or runtime packages. Python 3.11+ and Git are required on the
controller. Start from a checkout of
[readtrial](https://github.com/TayHCode/readtrial).

## Which isolation do I get on my OS?

| Your OS | Supported route | Isolation and limits |
| --- | --- | --- |
| Linux | Linux controller → LXC trial guest | Unprivileged namespaces, read-only system mounts, no NIC, exact-domain proxy, CPU/RAM/PID/disk ceilings; preflight must prove enforcement. Linux SSH is trusted mode only. |
| macOS | Linux controller → dedicated macOS SSH account | Standard non-sudo account, fresh HOME/XDG roots, home guards and journaled cleanup; **trusted mode only**, no network confinement or aggregate resource quotas. Use a Linux VM and LXC for untrusted code. |
| Windows | Disposable Linux VM controller → LXC trial guest | Follow the Linux setup inside the VM; the VM is the controller, and LXC is the separate trial target. Native Windows SSH is **trusted mode only**, without quotas or Job Object isolation. |

**Windows Sandbox is not supported:** readtrial has no backend to create,
control, attest or recover Sandbox targets. Create the Linux VM yourself;
readtrial does not orchestrate VMs.

## What runs on each OS

| Feature | Linux | macOS | Windows |
| --- | --- | --- | --- |
| `--help`, offline `demo` | Yes | Yes | Yes |
| Unit suite | All tests | Skips `os.listxattr` tests and one `verify` test | Skips tests that need POSIX |
| `verify`, `run`, `doctor`, isolated trials | Yes | Unverified | No: POSIX only |

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
python3 -m readtrial demo --out trial-results/demo
python3 -m unittest discover -s tests -q
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
py -3 -m readtrial demo --out trial-results\demo
py -3 -m unittest discover -s tests -q
```

With uv installed instead:

```powershell
uv run --no-project --python 3.12 python -m readtrial --help
```

For isolated trials on Windows, create a disposable Linux VM and follow
[Isolated trials](#isolated-trials) inside it. Run native Windows SSH trials from a Linux controller with `--trusted-target`.
The SSH login shell may be cmd; trial commands use PowerShell with profiles off.
The unit suite skips the POSIX-only controller tests. Windows-controller trials
are not supported.

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
python -m pip install --no-index --no-deps --no-build-isolation .
readtrial --help
```

This optional installation uses the included standard-library build backend.
A fresh venv needs only pip, with no network or preinstalled setuptools.
`python3 scripts/check_offline_install.py` checks committed HEAD in a clean clone
and fresh venv, runs all declared steps, and keeps its evidence in `.scratch/`.

## Isolated trials

Use a Linux controller with internet access: a real Linux host or a fresh Linux
VM both work, including a disposable Linux VM hosted on Windows or macOS.
The VM where you install readtrial and set up LXD is your
**controller**. A **trial guest** is the separate target that `readtrial run`
creates or prepares later; being a VM does not make the controller a trial guest.
Start in this checkout. Install
[uv](https://docs.astral.sh/uv/getting-started/installation/) if missing; the
commands below install readtrial into a local environment without changing Python:

```sh
TOOLS="$PWD/.scratch/controller-tools"
mkdir -p "$TOOLS/bin"
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$TOOLS/bin" UV_NO_MODIFY_PATH=1 sh
"$TOOLS/bin/uv" venv --python 3.12 .venv
"$TOOLS/bin/uv" pip install .
.venv/bin/readtrial doctor
```

`doctor` changes nothing. Exit 0 means the inspected prerequisites are ready;
1 means a prerequisite is missing or the route is unsupported; 2 means an error
or invalid arguments. Readiness is not full isolation attestation: every LXC
trial must still pass namespace and quota enforcement checks. On macOS, doctor
checks the designated trusted account, not container isolation. On native
Windows it exits 1 and explains the Linux VM route and lack of Sandbox support.
Follow its printed instructions
on the **controller**, then rerun it. Installing LXD or changing group membership
requires administrator access and may require a new login.
Ubuntu may include an LXD bootstrap launcher without LXD itself. Doctor reports
that as missing and prints the explicit install command; it never runs the
bootstrap installer for you.
Inside a target created or prepared by `readtrial run`, missing LXD is expected:
stop there; never expose a host
socket or create nested infrastructure to bypass isolation.

Once LXD and login access are ready, follow doctor's storage instruction even if
it still exits 1 for missing size limits. Create a size-capped pool, rerun doctor,
and start a trial only after doctor exits 0:

```sh
.venv/bin/readtrial setup-linux --yes
.venv/bin/readtrial doctor
.venv/bin/readtrial run . --target lxc:ubuntu:24.04 --lxc-pool readtrial --agent-command 'claude -p --model claude-haiku-5-5' --check 'python3 -m readtrial --help' --out trial-results/isolated
.venv/bin/readtrial leftovers trial-results/isolated/target-1/.isolation/journals
```

Install/authenticate the Claude CLI on the controller before the trial; credentials
stay there. A completed trial removes its exact container automatically. For an
interrupted trial, inspect leftovers, then use `readtrial recover JOURNALS --yes`
with that same journal directory. Recovery preserves unknown resources and journals.

Setup prints its plan and refuses changes without `--yes`. Default storage is a
30 GiB LXD-managed btrfs loop file that persists after reboot. Use `--size-gib 10`,
`--pool another-name`, or `--location /absolute/new-loop-file.img` to customize it;
pass a custom pool to trials with `--lxc-pool`. Existing pools are preserved:
use an already suitable pool, or choose a new name if `readtrial` exists.
Nested Linux containers may lack loop devices, so they cannot create this pool.
Doctor and setup explain that case: use a real Linux host or VM, or have its
administrator provide a quota-capable LXD pool. A `dir` pool cannot enforce the
required root size limit; do not expose host devices or sockets to bypass it.
After all trials finish, undo only a pool you created with this setup:

```sh
lxc storage delete readtrial
```

## Dedicated macOS test account

Use a separate **standard account** on a Mac you own for trusted README trials.
This reduces access to other users' private files; it is still trusted
SSH execution. Use a VM for untrusted code. The Linux controller performs real
trials; the Mac runner works with stock Python 3.9 and never changes users.

`doctor --target ssh:readtrial-mac --test-user trialuser` changes nothing and
checks the actual SSH identity, private primary group, sudo denial, and refused
opens of other user homes and SSH keys. An unknown result prevents trial upload.
It always prints the following setup steps, including when the account is ready.
Do not recreate an existing account.

Dedicated macOS account setup (administrator performs steps 1-3):

1. In an administrator Terminal, create a NEW standard account and its own group.
   If this account already exists, skip creation and use doctor to verify it.
   Remote Login must already be enabled by the administrator. Do not change services.

```sh
TRIAL_USER=trialuser
TRIAL_GROUP="${TRIAL_USER}grp"
TRIAL_HOME=/Users
TRIAL_HOME="$TRIAL_HOME/$TRIAL_USER"
if id "$TRIAL_USER" >/dev/null 2>&1; then echo 'Account exists; run doctor instead'; exit 1; fi
if dscl . -read "/Groups/$TRIAL_GROUP" >/dev/null 2>&1; then echo 'Group exists; choose a new name'; exit 1; fi
TRIAL_GID=$(dscl . -list /Groups PrimaryGroupID | awk 'BEGIN {m=500} $2+0>m {m=$2} END {print m+1}')
sudo dscl . -create "/Groups/$TRIAL_GROUP"
sudo dscl . -create "/Groups/$TRIAL_GROUP" PrimaryGroupID "$TRIAL_GID"
sudo sysadminctl -addUser "$TRIAL_USER" -fullName 'README trial' -GID "$TRIAL_GID" -home "$TRIAL_HOME" -shell /bin/sh -password -
sudo dscl . -create "$TRIAL_HOME" PrimaryGroupID "$TRIAL_GID"
sudo chown "$TRIAL_USER:$TRIAL_GROUP" "$TRIAL_HOME"
sudo chmod 700 "$TRIAL_HOME"
# No -admin flag. Do not add this account to admin or wheel.
# If Remote Login allows selected users, add only this user to its existing SSH group:
sudo dseditgroup -o edit -a "$TRIAL_USER" -t user com.apple.access_ssh
```

2. On the Linux controller, make a trial-only key (choose a NEW filename):

```sh
mkdir -p -m 700 .scratch/mac-key
ssh-keygen -t ed25519 -N '' -f .scratch/mac-key/id_ed25519
cat .scratch/mac-key/id_ed25519.pub
```

   Copy that one PUBLIC key into the following administrator-side Terminal block:

```sh
TRIAL_USER=trialuser
TRIAL_GROUP="${TRIAL_USER}grp"
TRIAL_HOME=/Users
TRIAL_HOME="$TRIAL_HOME/$TRIAL_USER"
printf 'Paste the trial public key line, then press Enter: '
read -r TRIAL_PUBLIC_KEY
sudo install -d -m 700 -o "$TRIAL_USER" -g "$TRIAL_GROUP" "$TRIAL_HOME/.ssh"
printf 'restrict %s\n' "$TRIAL_PUBLIC_KEY" | sudo tee -a "$TRIAL_HOME/.ssh/authorized_keys" >/dev/null
sudo chown "$TRIAL_USER:$TRIAL_GROUP" "$TRIAL_HOME/.ssh/authorized_keys"
sudo chmod 600 "$TRIAL_HOME/.ssh/authorized_keys"
```

   On the controller, replace your-mac with the Mac's hostname or IP address:

```sh
ssh -i .scratch/mac-key/id_ed25519 -o PasswordAuthentication=no trialuser@your-mac 'id; test "$(id -u)" != 0'
# Check the displayed host key fingerprint with the administrator.
# Add an SSH alias using this key so later commands select the same account:
TRIAL_KEY_PATH="$PWD/.scratch/mac-key/id_ed25519"
cat >> "$HOME/.ssh/config" <<SSH_CONFIG
Host readtrial-mac
    HostName your-mac
    User trialuser
    IdentityFile "$TRIAL_KEY_PATH"
    IdentitiesOnly yes
    PasswordAuthentication no
SSH_CONFIG
# This verifies key authentication; server-side password policy belongs to the administrator.
```

3. Sudo policy audit. A password prompt from sudo -n -l is NOT proof of denial.
   From the supplied readtrial checkout on the Mac, using stock Python 3.9:

```sh
sudo /usr/bin/python3 readtrial/account.py --test-user trialuser --yes
```

   With Python 3.11+, the equivalent CLI is readtrial attest-sudo --test-user
   trialuser --yes (also administrator-only).

   This checks sudo -l -U, changes no policy, and creates a root-owned denial file
   in the test home, valid for 24 hours. It refuses ambiguous/allowed policy.
   It refuses to overwrite an existing file. To renew, the administrator removes
   only "$TRIAL_HOME/.readtrial-sudo-denied.json" from step 1, then repeats the audit.
   Other users must ensure their home and .ssh deny this test account;
   doctor checks refusal without reading their contents. Resolve failed checks
   as the administrator; readtrial never changes other users' permissions.

4. From the Linux controller checkout:

```sh
python3 -m readtrial doctor --target ssh:readtrial-mac --test-user trialuser
python3 -m readtrial run ./your-trusted-repo --target ssh:readtrial-mac --dedicated-account --trusted-target --agent-command 'claude -p --model claude-haiku-5-5' --check 'python3 -m unittest discover -s tests -q' --out trial-results/mac-first
```

Use only trusted repositories. A fresh HOME/XDG/tmp/cache/tools is made under the
test account's home for each trial. Commands never switch users. Network access,
world-readable files, and the test account's original home remain reachable.
Pre/post guards hash pre-existing test-home files and verify other-user access refusal.
Cleanup removes only the exact journaled owned directory and start-verified process
groups. It preserves unknown paths/PIDs and reports INCOMPLETE on guard failure.
macOS has no pidfd: start verification cannot guarantee a race-free hostile-code
boundary. Escaped process groups cannot be attributed reliably. Use a VM for that.

If you also want to run readtrial's help or local doctor on the test account,
start in the supplied readtrial checkout under that account's home. Stock Python
3.9 cannot run the full controller. Install uv only into a fresh test folder:

```sh
TEST_TOOLS="$HOME/agent-tests/readtrial-tools-$(date +%Y%m%d%H%M%S)"
mkdir -p -m 700 "$TEST_TOOLS/tmp" "$TEST_TOOLS/bin" "$TEST_TOOLS/config" "$TEST_TOOLS/data" "$TEST_TOOLS/state"
export TMPDIR="$TEST_TOOLS/tmp" UV_CACHE_DIR="$TEST_TOOLS/cache" UV_PYTHON_INSTALL_DIR="$TEST_TOOLS/python"
export XDG_CONFIG_HOME="$TEST_TOOLS/config" XDG_DATA_HOME="$TEST_TOOLS/data" XDG_STATE_HOME="$TEST_TOOLS/state" XDG_CACHE_HOME="$TEST_TOOLS/cache"
export UV_TOOL_DIR="$TEST_TOOLS/tools" UV_TOOL_BIN_DIR="$TEST_TOOLS/bin" UV_PYTHON_BIN_DIR="$TEST_TOOLS/pybin" UV_NO_CONFIG=1 UV_PYTHON_INSTALL_BIN=0
export PYTHONDONTWRITEBYTECODE=1
curl -LsSf https://astral.sh/uv/install.sh -o "$TEST_TOOLS/install-uv.sh"
UV_INSTALL_DIR="$TEST_TOOLS/bin" UV_NO_MODIFY_PATH=1 sh "$TEST_TOOLS/install-uv.sh"
"$TEST_TOOLS/bin/uv" run --no-project --python 3.12 python -m readtrial --help
"$TEST_TOOLS/bin/uv" run --no-project --python 3.12 python -m readtrial doctor --test-user trialuser
```

The administrator audit above runs with stock Python 3.9 and imports only the
standard library, so it needs no administrator-side installation. The Python 3.11+
controller also offers `readtrial attest-sudo --test-user trialuser --yes`.
Only administrators perform account setup; repository trial commands run
through the standard test account.

Each trial journals an exact new directory directly under the account's home.
Its HOME, XDG configuration/data/cache/state, temporary files, downloaded Python,
and tools stay there. Process identity (PID, UID, group, start time) is saved on
the controller before command execution. Cleanup verifies it before signaling;
an unexpected PID, altered journal, changed directory identity, or unowned file
is preserved and reported INCOMPLETE. Pre/post manifests hash pre-existing
account-home files without following symlinks. Changed files are reported and
preserved. Other-user access refusal is checked before and after cleanup.

The original test-account home and its keys remain accessible to that account.
The network and world-readable host files remain accessible. macOS lacks pidfd;
start-time checks have a small race, and escaped process groups cannot be
reliably attributed. This mode cannot enforce aggregate memory, disk, CPU, or
process-count quotas, and it does not attest all other-user files.

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
`--instance-prefix sample-trial-` to recognize your instances; only exact owned
names are deleted. `--dry-run` on run/rerun prints the configuration and planned
actions without fetching, calling an agent, creating output, or contacting a target. A fresh unprivileged container has
no inherited profiles, NIC, home mounts, credentials, or container sockets.
Bootstrap installs Git, curl, Python, bubblewrap, certificates, and util-linux, without
adding Python venv support. This preserves stock prerequisite failures.
An exact-domain loopback proxy supplies package/repository access; no NIC provides
a direct network route. Unsupported isolation or quota setup fails INCOMPLETE.

## Environment, preservation and leftovers

Every Linux trial gets fresh mode-0700 HOME, XDG config/data/cache/state, temp,
cache and tool directories inside its disposable workspace. All commands and
independent checks share those roots; another trial starts with new empty roots.
uv installation/Python/cache/tools remain below the workspace and config loading
is disabled. Environment redirection supplements the container boundary.

The controller records pre/post manifests for its uv executable (or absence),
the source bundle, and optional explicit benign local paths. Use
`--protected-path ./synthetic-config` or `--readonly-input ./synthetic-input` to
add a file or directory. These options do not mount anything into a target.
Manifests compare presence, entry names, file type, permissions, UID/GID,
filesystem identity, timestamps, size, extended metadata/ACL digests and SHA-256 digests. Unsafe symlinks,
special files, unreadable required paths, changed inputs or failed postchecks
produce INCOMPLETE. Pre-existing files are never restored or removed.
These checks cover only the declared controller-local set, and cannot prove
that no transient writes occurred. On SSH they do not certify the remote host.

Before LXC creation, quota readiness checks reject unknown drivers and a `dir`
pool without an ext4/XFS project-quota mount. Driver capability is recorded
separately from independent enforcement proof. Before repository commands, the
owned instance must pass expanded-config, guest-kernel and actual namespace
read-back checks: no profiles, only the exact root/proxy devices, an isolated
unprivileged user mapping, no NIC or direct route, finite CPU/RAM/PID limits,
a capped workspace, read-only system mounts and process limits. Unknown or
mismatched evidence fails INCOMPLETE. A configured root size alone is not
evidence that its quota was exercised. Each LXC trial additionally requires a
bounded owned-file write to stop with EDQUOT while shared storage has independent
headroom; the probe is removed before source commands. Insufficient headroom,
a root ceiling above 4 GiB, or an inconclusive probe yields INCOMPLETE. Shared storage/profiles
are never adjusted to make preflight pass.

Each target has a durable mode-0600 write-ahead journal under
`target-1/.isolation/journals` in the mode-0700 output tree. LXC creation and
transport writes record exact identities before mutation; LXC instances carry
an identity tag. Cleanup checks the exact tag before deletion and confirms the
exact instance absent. A mismatched tag leaves the resource untouched and
produces INCOMPLETE. A killed controller can leave resources; inspect them with:

```sh
python3 -m readtrial leftovers trial-results/container/target-1/.isolation/journals
```

Discovery only reads journals and checks exact recorded identities. It does not
search or remove resources by prefix. Malformed or unsafe journals are refused;
unknown or ambiguous resources stay unresolved. For LXC an absent instance also
proves its recorded HOME/control paths absent. Explicit `readtrial recover JOURNALS --yes` deletes only exact LXC identities
after rechecking their ownership tags, then verifies absence; repeat recovery is
harmless. It preserves original journals and refuses unknown/ambiguous resources.
It never kills host processes or sweeps by prefix. SSH/OCI resource discovery
can report unknown and does not certify cleanup.

`result.json` and `run.json` distinguish requested policy from observed effective
capabilities, quota readiness, guard comparison, cleanup and unresolved leftovers.
Unverified fields are not enforcement proof. Saved run schema 2 retains protection
paths and isolation requirements on rerun; older schemas require a new run.
`--dry-run` prints the plan without asserting any effective capability.

Browser profiles, credentials and device access are forbidden in general policy.
Operators can add `--denied-endpoint example.invalid:45678`; these endpoints stay
in local run configuration. Visible URL inspection is an additional refusal rule,
not an OS boundary against computed addresses or child code.

## Trusted macOS and Windows SSH targets

`--trusted-target` is required on these targets and prints this notice prominently:

> less isolated: this target runs the repository's commands with your user's permissions inside a fresh work folder; use only on machines you own and repositories you trust; untrusted repositories are supported only on the Linux container target

readtrial runs on the Linux controller. macOS needs its stock POSIX shell/tools
and Git, not Python 3.11; the repository's commands use the target's actual tools.
Windows needs Git, PowerShell, and `taskkill`; cmd as the SSH shell is supported.
The runner creates a new work folder under the user's temp directory (`mktemp -d`
on macOS; a unique PowerShell directory under `%TEMP%` on Windows). It supplies
fresh HOME/temp paths and disables Git credential helpers/hooks during cloning.
No existing user software or system prerequisite is installed by setup.

```sh
python3 -m readtrial run ./your-repo --target ssh:your-mac --trusted-target --agent-command 'claude -p --model claude-haiku-5-5' --check 'python3 script.py --help' --out trial-results/mac
python3 -m readtrial run ./your-repo --target ssh:your-windows --trusted-target --agent-command 'codex exec -m gpt-6-luna' --check 'py -3 script.py --help' --out trial-results/windows
```

The flag, exact agent/checks, and limits persist through `rerun`. Every report
records trusted mode and the actual isolation/network level. The agent still has
no host tools: all proposed commands pass through this runner and are logged.

| Target | Isolation level | Filesystem/cwd | Network | Resource/cleanup controls |
| --- | --- | --- | --- | --- |
| Linux LXC | Confined, unprivileged container plus namespace | Restricted writable workspace; read-only system mounts | Exact repository/registry proxy, no NIC | CPU/RAM/process/disk ceilings; time/output bounds; verified deletion |
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
the runner already puts `<WORKDIR>/bin` first on the child PATH. No existing PATH or
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
and ignores existing user configuration. Missing venvs still fail; none is silently made.
PyPI package installs and an HTTPS `git clone` of the exact repository under test
into a relative child folder are accepted. Installation environment overrides,
system/user installs, alternate indexes, global uv tools/self-update, shell profile
changes, and outside paths remain refused. The existing privilege, service,
credential, browser and system-package deny list remains in force.

An allowance does not change the target boundary. All SSH targets use user
permissions and have unrestricted subprocess networking; only explicit URLs are inspected.
LXC's no-NIC proxy adds exact uv/Python distribution hosts (`astral.sh`,
`releases.astral.sh`, `github.com`, `release-assets.githubusercontent.com`, `objects.githubusercontent.com`,
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
installed bundled catalog without networking and creates a restricted per-invocation
`model_catalog_json` copy for the **same requested model**, selecting direct routing
and removing experimental, patch and search capabilities. It then applies the tool-disabling features and
`--output-schema`. No existing CLI configuration or model is replaced. A model absent
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
action is unwrapped before execution and annotated only when unwrapping occurred;
another malformed reply receives one
corrective retry rather than ending the trial immediately. Completion without any README command is a protocol error. Every
adapter failure is reported as `agent-protocol-error:` with a bounded scrubbed
diagnosis. CLI failures include up to 480 characters of redacted provider error
text, omitting session/usage metadata. Target temporary paths are abbreviated as `<WORKDIR>`
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
The note appears in `commands.jsonl`, `findings.json`, `report.md`, and one stdout
line. It records agent behavior and does not change the script verdict.
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

The following records describe earlier command/backend checks. They do not certify
the new preservation/journal boundary or independently exercise root-quota
enforcement. Current runs must satisfy preflight; unsupported pools return INCOMPLETE.

This table describes live release-preparation runs, separate from fake unit tests.
See [DEMO.md](DEMO.md) for outcomes and limits.

| Component | Live evidence | Status |
| --- | --- | --- |
| Linux SSH target | Fresh Debian workspace, explicit acknowledgement, limits, command inspection, cleanup | Unconfined; trusted code only |
| LXC target | Earlier fresh stock Ubuntu checks: no profiles/NIC, proxy, resource limits, cleanup | Earlier backend verified; current enforcement requires per-run preflight |
| Claude Haiku 5.5 | Schema-enforced command proposals on Debian and Windows SSH | Live; scripted verdicts |
| Codex Luna | Direct-routing restricted adapter; README commands on Ubuntu LXC and macOS SSH | Live; scripted verdicts |
| Learning loop | Old/current public README trials, exact checks, rerun diff | Linux, macOS, Windows |
| macOS SSH target | Stock Python 3.9.6, trusted runner, group kill, verified cleanup; dedicated-account synthetic trials with unchanged home guards and fresh roots | Trusted mode only; account provisioning and native Mac controller trials unverified |
| Windows SSH target | cmd SSH, PowerShell runner, taskkill tree cleanup | Trusted mode only |
| OCI target | Docker and Podman unavailable in the verification environment | Unsupported |
| Python 3.11 and 3.14 | Managed interpreters; clean-clone unit suite and installed CLI quickstart | Verified locally |

Reproduce the release check with uv-managed interpreters:

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
  remote commands, and browser/credential/device access. No system package installation on
  SSH. LXC OS isolation is the boundary. SSH filtering supplies
  refusals only; it cannot constrain arbitrary repository code.
- Destroy only resources created by this run, in cleanup even after failure.
  Verify removal. Cleanup failure is INCOMPLETE. Forced controller death or a lost
  connection can leave an owned target; use its exact name from local evidence.
- Keep raw results confidential: they may contain sensitive repository content. Review
  and scrub them manually before sharing. See [SECURITY.md](SECURITY.md).

## Verify a finished product

`verify` is a separate mode alongside the existing README trial. A fresh verifier
uses the product's documented CLI or command-driven API, observes each current
requirement and records proof. It takes a local Git project and a JSON array of
`{"id": "feature-id", "text": "required behavior"}` entries. It uses committed
HEAD, so commit the finished product first; uncommitted files and build outputs
are excluded. Install or build inside the fresh copy through the launch recipe.

The local mode requires POSIX, Git, and explicit `--trusted-local` consent:
commands run with your user's permissions. It creates a fresh source copy,
HOME, TMPDIR and XDG state; it drops inherited product credentials and applies
the README runner's visible-command refusals, time limits, output caps and process
group cleanup. Normal SIGINT/SIGTERM interruption also runs cleanup. This is **not filesystem or network confinement**, and it has no
aggregate memory, CPU or disk quota. Use trusted projects on a disposable machine.
The existing README mode still requires its supported disposable targets.

This offline counter rehearsal launches one process, adds three, reads three
from the same instance, then stops it and hashes transcripts. Scripted observations
are predefined; inspect the outputs. It makes no model/network calls, and its
placeholder source fingerprint is only for demonstration. Use a committed checkout.

```sh
python3 -m readtrial verify . --requirements examples/verify-requirements.json --scripted-plan examples/verify-plan.json --model scripted-verifier --builder-model sample-builder --source-hash 0000000000000000000000000000000000000000000000000000000000000000 --trusted-local --independent-agent --keep-running --out trial-results/verify-demo
```

Always choose a new output folder. To repeat that run, keep the same inputs and
add `--recipe trial-results/verify-demo/recipe.json` with a new `--out`.
Saved recipes retain their source hash; a changed hash is recorded and readiness
is retried against the new copy. Readiness must exercise the product. Failed
readiness saves `validated: false` with the failure. Unusable saved commands
(shape, policy, syntax or availability) are retained in `unusable-recipe.json`
and replaced through the agent; previously invalid recipes also get a fresh
proposal. Executed readiness failures remain unverified. See the protocol for
the wrapper and complete reuse contract.

For live verification, replace `--scripted-plan` with an authenticated
`--agent-command 'claude -p --model VERIFIER_MODEL'` or
`--agent-command 'codex exec -m VERIFIER_MODEL'`. The model must match the job and
differ from the builder. Native tools are disabled. Live calls may cost money.
Custom agents and offline plans return bare actions; Claude uses an object root
`{"action": ...}`. The first call permits only a recipe, enforced before commands.

Propose `{requirement_id, command, expect_exit: [0]}` before execution.
`expect_exit` lists accepted integer exits (0–255); `[0]` is the default.
A requirement may declare `expect_exit: [1]`, `[2]`, or `[1, 2]`; its commands
inherit that exact list and cannot override it. Otherwise a command proposal may
declare its own list before execution. The older `expected_exit_code: 1` remains
supported as a singleton; do not supply both fields. Then report
`{requirement_id, status, observed, reason}`. Matching non-zero exits can prove
behavior; an undeclared non-zero or any active mismatch fails proof.
Proof binds `expect_exit`, actual exit and match; the legacy scalar field is null
for multiple exits. Recipe phases accept `{command: "...", expect_exit: [1]}`;
strings expect [0]. Readiness, cleanup and completed launches must match; a kept
launch must stay alive. Every compound segment must execute with exit 0 or a
declared exit, and its final shell exit must match. Prefer simple commands.

This offline example proves intentional exits 1 and 2:

```sh
python3 -m readtrial verify . --requirements examples/expected-exit-requirements.json --scripted-plan examples/expected-exit-plan.json --model scripted-verifier --builder-model sample-builder --source-hash 0000000000000000000000000000000000000000000000000000000000000000 --trusted-local --independent-agent --out trial-results/expected-exits
```

Observations can select `proof_steps: [2]` and supersede an earlier attempt with
`superseded: [{"step": 1, "reason": "Corrected path."}]`. Each needs a later matching
replacement; all attempts stay recorded. Refusals and controller limits never
prove behavior. Finish with `{done: true, not_checked: ["gaps or none"]}`.
See [the protocol](VERIFY-PROTOCOL.md) for complete action and proof rules.

`--keep-running` requires a live owned launch. Cleanup terminates its process
group, runs owned-state cleanup and removes the copy even after failures.
Failed cleanup invalidates observations; proof retains lifecycle evidence.

A requirement can declare `not_verifiable_in_sandbox: "reason"`; it stays
unverified with `blocked_by: "sandbox"`, no commands and no product exit.
For v2-r9 the owner compares the original skill with VERIFY-DESIGN.md externally;
for v2-r10 the owner reviews private project receipts and evidence externally.
Neither limitation proves product failure or success. Both stay in `not_checked`.
See [sandbox examples](examples/sandbox-requirements.json) and the protocol.
The local copy provides no OS confinement.

Exit codes: 0 means all requested observations have successful command proof;
1 means some requirement remains unverified (including declared sandbox limitations) or startup failed; 2 means the runner,
provider, protocol or cleanup failed. These statuses do not decide a Factory gate.

### Offline reactive agent example

This offline stand-in uses `--agent-command`, reads README/requirements/history
on stdin, chooses documented commands and compares actual numeric JSON output.
It demonstrates the protocol; independent judgement needs a real model.
Omit `--keep-running` for this short-lived CLI. Use a committed checkout and a
new output folder. Relative agent executables/scripts resolve from the starting
directory or project before isolation; paths outside those roots are refused.

```sh
python3 -m readtrial verify . --requirements examples/standin-passing-requirements.json --job examples/verify-passing-job.json --builder-model sample-builder --agent-command "python3 examples/standin_agent.py" --trusted-local --independent-agent --out trial-results/agent-demo/run
```

Expect exit **0**, with `addition` verified from actual output.
`examples/agent_product/README.md` supplies the commands, also listed here so
this repository can be the input project:

- add: `python3 examples/agent_product/product.py add 2 3`
- multiply: `python3 examples/agent_product/product.py multiply 2 3`

The sample job includes `stage: "verify"`, the exact IDs, an offline stand-in
identity and a synthetic source fingerprint. It is an example of the wire format,
not a real Factory job or source fingerprint. Validate the finished report with:

```sh
python3 -m readtrial validate-verify trial-results/agent-demo/run/verify.json --job examples/verify-passing-job.json
```

Expect exit **0** and one line confirming the shape, exact IDs, job model/hash and
proof files with matching SHA-256. An unverified requirement is valid evidence.
Validation requires complete execution detail and a hashed source/command-set
binding with a unique run ID. Legacy proofs must be rerun; missing or unknown fields are errors.
Resolution/spawn failures, killed processes and incomplete shell evidence cannot
verify behavior. See [the protocol](VERIFY-PROTOCOL.md) for supported shell forms.
This read-only check does not pass a gate, prove judgement, or replace the
Factory's own validation. Invalid evidence exits **2** with a one-line reason.

For an honest failure (undeclared as a release quickstart), use the original job
and both requirements. Expect exit **1**: multiplication returns 5 instead of 6.
The stand-in derives the failure from output; both requirements have proof.
Self-verification declares `expected_exit_code: 1` before the demo runs and
checks its recorded gaps separately. Multiplication inside remains unverified.
Expect exit **0** from this outer run:

```sh
python3 -m readtrial verify . --requirements examples/honest-failure-requirements.json --job examples/honest-failure-job.json --builder-model sample-builder --agent-command "python3 examples/standin_agent.py" --trusted-local --independent-agent --out trial-results/honest-proof
```

Stand-in vocabulary: `OP exits with code NUMBER and output containing TEXT.`
Commands for these requirements:

- honest_failure: `python3 -m readtrial verify . --requirements examples/standin-requirements.json --job examples/verify-job.json --builder-model sample-builder --agent-command "python3 examples/standin_agent.py" --trusted-local --independent-agent --out trial-results/agent-demo/failing`
- gaps: `python3 -c "import json; print(json.dumps(json.load(open('trial-results/agent-demo/failing/verify.json'))['not_checked']))"`

The original honest-failure command (exit **1**, undeclared in release-check) is:

```sh
python3 -m readtrial verify . --requirements examples/standin-requirements.json --job examples/verify-job.json --builder-model sample-builder --agent-command "python3 examples/standin_agent.py" --trusted-local --independent-agent --out trial-results/agent-demo/failing
```

To demonstrate an obsolete startup recipe offline, run this single command with
a new output folder (no previous demo run is needed):

```sh
python3 -m readtrial verify . --requirements examples/standin-requirements.json --job examples/verify-job.json --builder-model sample-builder --agent-command "python3 examples/standin_agent.py" --trusted-local --independent-agent --recipe examples/stale-recipe.json --command-seconds 1 --out trial-results/agent-demo/stale
```

Expect exit **1**: readiness calls a removed operation. Both requirements remain
unverified; proof records `recipe_reused: true`, `source_changed: true`, the failed
readiness output and cleanup. This expected failure is also undeclared as a
release quickstart. The saved recipe has `validated: false`, so reusing it asks
the agent for a fresh recipe. A changed source hash alone does
not invalidate a recipe that still passes readiness.

### Factory handoff

Supply the approved requirements and the verifier job as private JSON inputs.
The job must include `stage: "verify"`. It supplies `route.model`, `source_hash`,
`required_check_ids`, optional saved `recipe` path and the private `evidence_dir`.
Do not use a build job here; `examples/verify-job.json` shows the required fields.
The recorded builder model comes from the caller's build record. The exact call
has this form; substitute your private paths and recorded builder model:

```sh
python3 -m readtrial verify PROJECT --requirements requirements.json --job verify-job.json --builder-model BUILDER_MODEL --agent-command 'claude -p --model VERIFIER_MODEL' --trusted-local --independent-agent --out evidence/readtrial-run
```

Use the job's verifier model in the agent command. Put the new output folder
beneath `job.evidence_dir`. Add `--keep-running` when the recipe launches an app;
for short-lived CLI checks omit it. Without `--job`, supply `--model` and
`--source-hash` explicitly. The hash must be the caller's current source
fingerprint; readtrial does not invent or independently calculate that external
fingerprint. An empty requirement list becomes `smoke`. A job requiring
`regression-smoke` adds that startup requirement automatically; otherwise the ID
set must match the job exactly.

Give the Factory **`evidence/readtrial-run/verify.json`**. It records model,
source fingerprint and commit, recipe, checks, gaps and tool problems. Each check
has complete step results, proof selection and a private hashed artifact binding
all requirements and commands to that source. Verified check exit codes are 0;
actual product exits remain in step results. Unverified checks retain a reason.
See [the evidence contract](VERIFY-PROTOCOL.md) for required fields and validation.
`recipe.json` saves the reusable recipe. Output files use mode 600 and folders
700. Factory checks the job route, source fingerprint, proof files and exact IDs
and decides its own gates; readtrial does not decide gate passage.

To check whether the Factory completed a real project verification through
readtrial, inspect its private job evidence and recorded verify receipt. A tool
run leaves the original `verify.json`, hashed per-requirement proof files and
`recipe.json`, plus a verify receipt whose tool provenance names **readtrial**, its **version**,
its **commit**, and the **SHA-256 of verify.json**. Compare that hash with the saved
verify.json bytes and match the named version/commit to the tool used. Match the receipt to that evidence, job and source
fingerprint, and check that it completed without fallback. A fallback instead
records a runner/provider problem and explicit fallback authorization, followed
by evidence from the plain verifier. A receipt alone, an offline demo or a toy
CLI run does not establish that completion condition. This README does not claim
that the Factory has completed it.

## Development

```sh
python3 -m unittest discover -s tests -q
```

Unit tests use fake targets and agents, no real models, SSH, LXC, or OCI. Small
local subprocess tests exercise bounds. Fake sensitive values are built at runtime.
Optional checks: `uvx ruff check --no-cache readtrial tests` and
`uvx mypy --ignore-missing-imports readtrial`.

## Limitations

Linux controllers support confined LXC targets and explicitly acknowledged Linux/macOS/Windows
SSH targets. Nested controllers
without loop devices require an existing quota-capable pool; never expose host
devices or sockets to bypass preflight.
README trials requiring repository authentication, browser/UI testing, deployment, and VM orchestration are
unsupported. Verify mode supports trusted local POSIX CLI and command-driven API journeys;
it has no native browser, desktop UI or TUI driver, and no interactive stdin runner.
A scripted plan proves execution only, not independent semantic judgment. Readiness
revalidation cannot prove that an old recipe still covers every feature. SIGKILL,
machine failure or a child that escapes its process group can leave local processes;
use a disposable machine and inspect private proof. Visible network URLs, including
loopback URLs, remain subject to the existing endpoint allowlist; use a documented
product client for local API journeys. Independence relies on caller attestation
and model identity; readtrial cannot prove who built the product. Live verify
coverage is limited to a small Claude CLI journey. Large source bundles/histories, missing or oversized README text,
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

Verify-mode ideas also come from Lauren Tan's
[create-verification-skill in Cursor pstack](https://github.com/cursor/plugins/blob/main/pstack/skills/create-verification-skill/SKILL.md)
([MIT licence](https://github.com/cursor/plugins/blob/main/pstack/LICENSE)):
exercise the user interface, check usable readiness, retain one owned app between
steps, preserve action/result proof, reuse startup instructions, report gaps and
clean up owned state. No reference text, prompts, code or file structure was copied.

See [the verification design note](VERIFY-DESIGN.md) for each idea and its file/function
implementation. A comparison with the original needs access to that original.
