"""SSH execution for explicitly trusted repositories. No OS isolation claim."""
from __future__ import annotations

import base64
import json
import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .installers import DOWNLOAD_DOMAINS, environment, prepare
from .process import Result, decode_output, execute
from .proxy import REGISTRIES
from .shell import path_requests, posix_script, powershell_script, statuses
from .worker import policy

if TYPE_CHECKING:
    from .targets import Target

LINUX_NOTICE = ("Linux SSH is not confined: commands run with your user permissions in a fresh folder; "
                "time/output limits and a visible-command deny list are enforced; no network or filesystem confinement; "
                "use only on machines you own and repositories you trust. Only LXC is a live verified confined target.")

NOTICE = ("less isolated: this target runs the repository's commands with your user's permissions "
          "inside a fresh work folder; use only on machines you own and repositories you trust; "
          "untrusted repositories are supported only on the Linux container target")

UTF8_SETTINGS = ("$OutputEncoding=New-Object Text.UTF8Encoding($false); "
                 "[Console]::OutputEncoding=$OutputEncoding; [Console]::InputEncoding=$OutputEncoding; "
                 "$PSDefaultParameterValues['Get-Content:Encoding']='utf8'; ")


def inspect(command: str, domains: set[str], workdir: str = "", windows: bool = False, repository: str = "") -> str:
    """Refuse visible hazards. Encoded/computed paths and child code can bypass this."""
    _, command, denial = prepare(command, workdir, windows, repository)
    if denial:
        return denial
    if workdir:
        command = re.sub(re.escape(workdir) + r"(?=[/\\]|[\s\'\";]|$)", ".", command)
    denial = policy(command, windows=windows)
    if denial:
        return denial
    lower = command.lower()
    visible_paths = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>;)]+", "", command)
    if re.search(r"(?:^|[\s'\"=><;])/(?!dev/null(?:[\s'\";]|$))", visible_paths):
        return "absolute path outside workspace"
    if re.search(r"(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|\\\\|%[a-z_]+%|\$env:(?!home\b|tmp\b|temp\b|path\b)\w+)", command):
        return "path outside workspace or host environment"
    if re.search(r"(?i)\bcd\s+(?:/|~|\$|%)", command):
        return "path outside workspace"
    if re.search(r"(?i)(?:-verb\s+['\"]?runas|\b(?:runas|brew|port|winget|choco|scoop|schtasks|sc\.exe|reg\.exe|security|launchctl|osascript)\b|git\s+config\s+--(?:global|system)|\b(?:kill|pkill|killall|taskkill|Stop-Process|Set-ExecutionPolicy)\b)", command):
        return "privilege or system change"
    if any(word in lower for word in ("google/chrome", "google\\chrome", "application support", "keychain",
                                      "login data", "browser", "firefox", "msedge", "chromium", "chrome", ".gnupg", ".azure", ".npmrc", ".pypirc", "vault", "dpapi")):
        return "browser profile or credential store"
    for address in re.findall(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>;)]+", command):
        try:
            parsed = urlsplit(address)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in domains | REGISTRIES | DOWNLOAD_DOMAINS or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
                return "network host outside repository/registry allowlist"
        except ValueError:
            return "invalid network address"
    if re.search(r"\b(?:curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|irm|ping|nc|netcat)\b", command, re.IGNORECASE) and not re.search(r"https?://", command):
        return "network command requires an explicit approved URL"
    return ""


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell(script: str) -> str:
    return "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(("$ProgressPreference='SilentlyContinue'; " + UTF8_SETTINGS + script).encode("utf-16le")).decode()


class TrustedSSH:
    def __init__(self, target: Target):
        self.target = target
        self.windows = target.platform == "Windows"
        self.sequence = 0

    def remote(self, script: str) -> list[str]:
        t = self.target
        return t.prefix + [powershell(script)] if self.windows else t.outer(["bash" if t.platform == "Linux" else "sh", "-c", script])

    def prepare(self, bundle: Path) -> None:
        t = self.target
        if self.windows:
            script = (f"$p=Join-Path ([IO.Path]::GetTempPath()) {ps_quote(t.id)}; "
                      "if (Test-Path -LiteralPath $p) { exit 2 }; "
                      "New-Item -ItemType Directory -Path $p -ErrorAction Stop | Out-Null; $p")
        else:
            script = f'mktemp -d "${{TMPDIR:-/tmp}}/{t.id}.XXXXXX"'
        result = t.management(self.remote(script))
        t.base = result.output.strip()
        # Refuse unexpected shell noise/path syntax before any deletion is possible.
        valid = (bool(re.fullmatch(r"[A-Za-z]:\\[^\r\n'\x00]*\\" + re.escape(t.id), t.base)) if self.windows else
                 bool(re.fullmatch(r"/[^\r\n'\x00]*?/" + re.escape(t.id) + r"\.[A-Za-z0-9]+", t.base)))
        if not valid:
            from .targets import TargetError
            raise TargetError("unexpected owned temporary directory path")
        t.created = True
        if self.windows:
            q = ps_quote(t.base)
            upload = (f"$ProgressPreference='SilentlyContinue'; $p={q}; "
                      "foreach ($n in @('repo','home','tmp','control','bin')) { New-Item -ItemType Directory -Path (Join-Path $p $n) | Out-Null }; "
                      f"[IO.File]::WriteAllBytes((Join-Path $p 'source.bundle'), [Convert]::FromBase64String('{base64.b64encode(bundle.read_bytes()).decode()}')); exit 0\n")
            t.management(t.prefix + ["powershell -NoProfile -NonInteractive -Command -"], data=upload.encode())
        else:
            q = shlex.quote(t.base)
            upload = f"chmod 700 {q}; mkdir {q}/repo {q}/home {q}/tmp {q}/control {q}/bin; cat > {q}/source.bundle"
            t.management(self.remote(upload), data=bundle.read_bytes())
        clone = (f"git -c core.hooksPath=NUL -c credential.helper= clone {ps_quote(t.base + '/source.bundle')} ." if self.windows else
                 f"git -c core.hooksPath=/dev/null -c credential.helper= clone {shlex.quote(t.base + '/source.bundle')} .")
        result = self.command(clone, setup=True)
        if result.exit_code:
            from .targets import TargetError
            raise TargetError("trusted clone failed: " + result.output)

    def command(self, command: str, *, setup: bool = False, timeout: float | None = None) -> Result:
        t = self.target
        started = time.monotonic()
        denial = "" if setup else inspect(command, t.domains, t.base, self.windows, t.repository)
        budget = min(timeout if timeout is not None else t.limits.command_seconds, t.deadline - time.monotonic())
        if denial:
            result = Result(126, "", 0, denied=denial)
        elif budget <= 0:
            result = Result(124, "", 0, timed_out=True)
        else:
            runnable = command if setup else prepare(command, t.base, self.windows, t.repository)[0]
            self.sequence += 1
            script = self.windows_script(runnable, budget, guard=not setup) if self.windows else self.mac_script(runnable, budget, guard=not setup)
            if self.windows:
                argv = t.prefix + ["powershell -NoProfile -NonInteractive -Command -"]
                encoded_script = base64.b64encode(script.encode("utf-16le")).decode()
                data = (UTF8_SETTINGS + "$ProgressPreference='SilentlyContinue'; & ([ScriptBlock]::Create([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('" + encoded_script + "')))); exit 0\n").encode()
            else:
                argv, data = self.remote(script), b""
            transport = execute(argv, data=data, timeout=budget + 10, cap=t.limits.output_bytes * 6 + 4096)
            try:
                value = json.loads(transport.output)
                if "output_b64" in value:
                    value["output"] = decode_output(base64.b64decode(value.pop("output_b64"), validate=True))
                if "steps_b64" in value:
                    value["steps"] = statuses(base64.b64decode(value.pop("steps_b64"), validate=True).decode("ascii"), runnable, self.windows)
                result = Result(**value)
                if result.exit_code == 126 and 'READTRIAL_PATH_REFUSAL:' in result.output:
                    result.denied = result.output.split('READTRIAL_PATH_REFUSAL:', 1)[1].strip()
                failed = next((step['exit_code'] for step in result.steps if step['exit_code']), 0)
                if failed and not result.exit_code:
                    result.exit_code = failed
                if transport.exit_code or transport.truncated or transport.timed_out:
                    raise ValueError("transport did not complete")
            except (ValueError, TypeError):
                t.log("transport", "trusted command transport", transport)
                from .targets import TargetError
                raise TargetError("trusted runner transport failed: " + transport.output) from None
            if runnable != command:
                t.log('installation-rewrite', runnable, result)
        result.seconds = round(time.monotonic() - started, 3)
        t.log("setup" if setup else "command", command, result)
        return result

    def path_guard(self, command: str) -> str:
        """Resolve visible paths on the target, never against controller files."""
        inspected = prepare(command, self.target.base, self.windows, self.target.repository)[1]
        requests = [path for path in path_requests(inspected, self.windows) if path != '/dev/null']
        if not requests:
            return ''
        program = ("import base64,json,sys; from pathlib import Path; "
                   "root=Path.cwd().resolve(); "
                   "paths=json.loads(base64.b64decode(sys.argv[1])); "
                   "bad=any(root != (root/p).resolve() and root not in (root/p).resolve().parents for p in paths); "
                   "print('READTRIAL_PATH_REFUSAL: resolved path outside workspace') if bad else None; "
                   "sys.exit(126 if bad else 0)")
        args = base64.b64encode(json.dumps(requests).encode()).decode()
        if self.windows:
            return ('py -3 -c ' + ps_quote(program) + ' ' + ps_quote(args) +
                    "; if (!$? -or $LASTEXITCODE) { Write-Output 'READTRIAL_PATH_REFUSAL: path resolver unavailable or failed'; exit 126 };\n")
        return ('python3 -c ' + shlex.quote(program) + ' ' + shlex.quote(args) +
                " || { printf 'READTRIAL_PATH_REFUSAL: path resolver unavailable or failed\\n'; exit 126; }\n")

    def mac_script(self, command: str, budget: float, *, guard: bool = True) -> str:
        t = self.target
        q = shlex.quote(t.base)
        slot = f"{q}/control/{self.sequence}"
        # Job control gives the child a process group on stock macOS /bin/sh.
        # The leader stays alive after recording exit, for background cleanup too.
        measured = (self.path_guard(command) if guard else '') + posix_script(command, slot + ".steps")
        child = (f"cd {q}/repo || exit 2; sh -c {shlex.quote(measured)}; "
                 f"echo $? > {slot}.exit; sleep 86400")
        install_env = " ".join(name + "=" + shlex.quote(value) for name, value in environment(t.base).items())
        watchdog = f"sleep {budget}; echo timeout > {slot}.timeout; kill -KILL -- -\"$1\" 2>/dev/null"
        # A separate watchdog supplies wall time independent of output polling.

        return f"""exec 2>/dev/null
set -m
umask 077
: > {slot}.steps
env -i {install_env} PATH={q}/bin:{q}/repo/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin HOME={q}/home TMPDIR={q}/tmp LANG=en_US.UTF-8 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 sh -c {shlex.quote(child)} > {slot}.out 2>&1 < /dev/null &
p=$!
echo "$p" > {slot}.pid
sh -c {shlex.quote(watchdog)} watcher "$p" >/dev/null 2>&1 &
w=$!
echo "$w" > {slot}.watch.pid
n=0; code=0; timed=false; capped=false
while test ! -f {slot}.exit; do
  size=$(wc -c < {slot}.out)
  if test "$size" -gt {t.limits.output_bytes}; then code=125; capped=true; break; fi
  if test -f {slot}.timeout; then code=124; timed=true; break; fi
  sleep 0.1; n=$((n+1))
done
if test -f {slot}.exit && test "$code" = 0; then code=$(cat {slot}.exit); fi
kill -KILL -- -"$p" 2>/dev/null || :
wait "$p" 2>/dev/null || :
kill -KILL -- -"$w" 2>/dev/null || :
wait "$w" 2>/dev/null || :
if ps -axo pgid=,stat= | awk -v g="$w" '$1 == g && $2 !~ /^Z/ {{ found=1 }} END {{ exit !found }}'; then exit 3; fi
rm {slot}.watch.pid
if ps -axo pgid=,stat= | awk -v g="$p" '$1 == g && $2 !~ /^Z/ {{ found=1 }} END {{ exit !found }}'; then exit 3; fi
rm {slot}.pid
size=$(wc -c < {slot}.out)
if test "$size" -gt {t.limits.output_bytes}; then code=125; capped=true; fi
printf '{{"exit_code":%s,"seconds":%s,"timed_out":%s,"truncated":%s,"output_b64":"' "$code" "$(awk -v n="$n" 'BEGIN {{print n/10}}')" "$timed" "$capped"
head -c {t.limits.output_bytes} {slot}.out | base64 | tr -d '\\r\\n'
printf '","steps_b64":"'
cat {slot}.steps | base64 | tr -d '\\r\\n'
printf '"}}\\n'
"""

    def windows_script(self, command: str, budget: float, *, guard: bool = True) -> str:
        t = self.target
        base = ps_quote(t.base)
        slot = t.base + f"/control/{self.sequence}"
        out, end, pid = (ps_quote(slot + suffix) for suffix in (".out", ".exit", ".pid"))
        # A nested shell prevents command `exit` from exiting the tree leader.
        measured = (self.path_guard(command) if guard else '') + powershell_script(command, ps_quote(slot + ".steps"))
        nested = powershell(measured + "\nif (!$?) { exit 1 }; exit $LASTEXITCODE")
        err = ps_quote(slot + '.err')
        inner = (UTF8_SETTINGS + f"Set-Location -LiteralPath {ps_quote(t.base + '/repo')}; "
                 "$native=New-Object Diagnostics.ProcessStartInfo; $native.FileName='powershell.exe'; "
                 f"$native.WorkingDirectory={ps_quote(t.base + '/repo')}; "
                 f"$native.Arguments={ps_quote(nested.partition(' ')[2])}; "
                 "$native.UseShellExecute=$false; $native.RedirectStandardError=$true; $native.RedirectStandardOutput=$true; "
                 "$native.StandardOutputEncoding=$OutputEncoding; $native.StandardErrorEncoding=$OutputEncoding; "
                 "$child=New-Object Diagnostics.Process; $child.StartInfo=$native; [void]$child.Start(); "
                 f"$of=[IO.File]::Open({out}, 'Create', 'Write', 'ReadWrite'); $ef=[IO.File]::Open({err}, 'Create', 'Write', 'ReadWrite'); "
                 "$stdout=$child.StandardOutput.BaseStream.CopyToAsync($of); $stderr=$child.StandardError.BaseStream.CopyToAsync($ef); "
                 "$child.WaitForExit(); $stdout.Wait(); $stderr.Wait(); $of.Dispose(); $ef.Dispose(); "
                 f"[IO.File]::WriteAllText({end}, [string]$child.ExitCode); "
                 "Start-Sleep -Seconds 86400")
        encoded = base64.b64encode(inner.encode("utf-16le")).decode()
        install_env = "\n".join("$info.EnvironmentVariables[" + ps_quote(name) + "]=" + ps_quote(value) for name, value in environment(t.base, True).items())
        return f"""{UTF8_SETTINGS}$ErrorActionPreference='Stop'
$base={base}; $out={out}; $end={end}; $pidfile={pid}
[IO.File]::WriteAllText({ps_quote(slot + ".steps")}, '')
$info=New-Object Diagnostics.ProcessStartInfo
$info.FileName='powershell.exe'; $info.Arguments='-NoProfile -NonInteractive -EncodedCommand {encoded}'
$info.UseShellExecute=$false; $info.CreateNoWindow=$true
$info.WorkingDirectory={ps_quote(t.base + '/repo')}
$keep=@{{}}
foreach ($name in @('PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','ProgramFiles','ProgramFiles(x86)')) {{ $keep[$name]=$info.EnvironmentVariables[$name] }}
$info.EnvironmentVariables.Clear()
foreach ($name in $keep.Keys) {{ if ($keep[$name]) {{ $info.EnvironmentVariables[$name]=$keep[$name] }} }}
$info.EnvironmentVariables['PATH']=(Join-Path $base 'bin')+';'+(Join-Path $base 'repo/.venv/Scripts')+';'+$info.EnvironmentVariables['PATH']
{install_env}
$info.EnvironmentVariables['HOME']=Join-Path $base 'home'
$info.EnvironmentVariables['USERPROFILE']=Join-Path $base 'home'
$info.EnvironmentVariables['APPDATA']=Join-Path $base 'home'
$info.EnvironmentVariables['LOCALAPPDATA']=Join-Path $base 'home'
$info.EnvironmentVariables['TEMP']=Join-Path $base 'tmp'; $info.EnvironmentVariables['TMP']=Join-Path $base 'tmp'
$info.EnvironmentVariables['GIT_CONFIG_NOSYSTEM']='1'; $info.EnvironmentVariables['GIT_CONFIG_GLOBAL']='NUL'
$info.EnvironmentVariables['GIT_TERMINAL_PROMPT']='0'
$p=New-Object Diagnostics.Process; $p.StartInfo=$info; [void]$p.Start()
[IO.File]::WriteAllText($pidfile, [string]$p.Id)
$clock=[Diagnostics.Stopwatch]::StartNew(); $code=0; $timed=$false; $capped=$false
while (!(Test-Path -LiteralPath $end)) {{
 if ($p.HasExited) {{ $code=$p.ExitCode; break }}
 $size=0; foreach ($file in @($out,{err})) {{ if (Test-Path -LiteralPath $file) {{ $size += (Get-Item -LiteralPath $file).Length }} }}
 if ($size -gt {t.limits.output_bytes}) {{ $code=125; $capped=$true; break }}
 if ($clock.Elapsed.TotalSeconds -ge {budget}) {{ $code=124; $timed=$true; break }}
 Start-Sleep -Milliseconds 50
}}
if ((Test-Path -LiteralPath $end) -and $code -eq 0) {{ $code=[int][IO.File]::ReadAllText($end) }}
$tree=@($p.Id)
$all=@(Get-CimInstance Win32_Process)
do {{ $new=@($all | Where-Object {{ $_.ParentProcessId -in $tree -and $_.ProcessId -notin $tree }} | Select-Object -ExpandProperty ProcessId); $tree += $new }} while ($new.Count -gt 0)
[IO.File]::WriteAllText($pidfile, ($tree -join ','))
& taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null
$p.WaitForExit(3000) | Out-Null
$cleanupClock=[Diagnostics.Stopwatch]::StartNew()
while ((Get-Process -Id $tree -ErrorAction SilentlyContinue) -and $cleanupClock.Elapsed.TotalSeconds -lt 3) {{ Start-Sleep -Milliseconds 100 }}
if (Get-Process -Id $tree -ErrorAction SilentlyContinue) {{ throw 'owned process cleanup not confirmed' }}
Remove-Item -LiteralPath $pidfile
$bytes=New-Object byte[] {t.limits.output_bytes}; $count=0; $size=0
foreach ($file in @($out,{err})) {{ if (Test-Path -LiteralPath $file) {{
 $f=[IO.File]::Open($file, 'Open', 'Read', 'ReadWrite')
 try {{ $size += $f.Length; $count += $f.Read($bytes,$count,$bytes.Length-$count) }} finally {{ $f.Dispose() }}
}} }}
if ($size -gt $bytes.Length) {{ $code=125; $capped=$true }}
$text=[Convert]::ToBase64String($bytes,0,$count)
$steps=[Convert]::ToBase64String([IO.File]::ReadAllBytes({ps_quote(slot + ".steps")}))
@{{exit_code=$code;output_b64=$text;seconds=[Math]::Round($clock.Elapsed.TotalSeconds,3);timed_out=$timed;truncated=$capped;steps_b64=$steps}} | ConvertTo-Json -Compress
"""

    def close(self) -> None:
        t = self.target
        if not t.created:
            return
        if self.windows:
            q = ps_quote(t.base)
            script = (f"$base={q}; Get-ChildItem -LiteralPath (Join-Path $base 'control') -Filter '*.pid' | ForEach-Object {{ "
                      "$owned=@([IO.File]::ReadAllText($_.FullName).Split(',') | ForEach-Object { [int]$_ }); foreach ($id in $owned) { if (Get-Process -Id $id -ErrorAction SilentlyContinue) { & taskkill.exe /PID $id /T /F 2>&1 | Out-Null } }; "
                      "if (Get-Process -Id $owned -ErrorAction SilentlyContinue) { throw 'owned process remains' } }; "
                      "Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction Stop; "
                      "if (Test-Path -LiteralPath $base) { exit 2 }; 'owned folder absent; tracked process trees terminated'")
        else:
            q = shlex.quote(t.base)
            script = f"""for f in {q}/control/*.pid; do
test -f "$f" || continue
p=$(cat "$f"); case "$p" in ''|*[!0-9]*) exit 2;; esac
kill -KILL -- -"$p" 2>/dev/null || :
sleep 0.1
if ps -axo pgid=,stat= | awk -v g="$p" '$1 == g && $2 !~ /^Z/ {{ found=1 }} END {{ exit !found }}'; then exit 3; fi
done
rm -rf -- {q}
test ! -e {q} || exit 2
printf 'owned folder absent; tracked process groups terminated\\n'
"""
        result = execute(self.remote(script), timeout=30, cap=t.limits.output_bytes)
        t.log("cleanup-confirmation", "remove owned folder and confirm tracked processes absent", result)
        if result.exit_code or result.timed_out or result.truncated:
            from .targets import TargetError
            raise TargetError("trusted target cleanup failed: " + result.output)
