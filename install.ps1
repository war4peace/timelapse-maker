<#
.SYNOPSIS
  Install timelapse-maker on Windows.

.DESCRIPTION
  The counterpart to install.sh, not a replacement for it. It does the same
  job in the same order: check privilege, find the interpreter, place files,
  register the services, run the wizard.

  Three things it deliberately does NOT do, each decided rather than skipped:

    * It does not install ffmpeg. Windows has no package manager worth relying
      on, the builds people run come from gyan.dev or BtbN rather than from
      any package source, and a recorder very often already has an ffmpeg the
      operator chose. Installing a second copy would override a decision that
      was not ours. The wizard asks for the path and verifies it by running it.

    * It does not install Python. It finds one and records its absolute path,
      and if there is none it says where to get one and stops. Without an
      interpreter there is nothing to install.

    * It does not snapshot and restore every unit's enabled/disabled state
      across an upgrade, the way install.sh does; there is no field of Windows
      installs whose states need preserving yet. Re-running IS safe: an
      existing service is reconfigured rather than refused, and one that was
      running is restarted onto the new build, because a service already
      running is executing the scripts it read at startup.

  The first two of those refusals are right for THIS front door and not for
  every one. Somebody running this from a checkout already has a terminal open
  and can go and fetch an interpreter; somebody who downloaded a .exe from the
  release page cannot reasonably be told to. So the graphical installer has a
  prerequisite stage, installer\prepare.ps1, which finds or fetches Python and
  ffmpeg and then calls this script. It places nothing and registers nothing:
  the split is prerequisites there, installation here.

  Everything that decides anything lives in the Python scripts, and this file
  calls them: registration is `timelapse_setup.py --install-units`, which is
  also what the GUI installer front-ends rather than reimplements.
  Two installers that both know how to register a service disagree within one
  release; this project has already deleted one directory over exactly that.

.PARAMETER Unattended
  Take every default and ask nothing. For scripted deployments.

.PARAMETER NoWizard
  Place the files and register the services, but do not run the wizard.

.PARAMETER Uninstall
  Deregister the service and both tasks and remove the program files. The
  configuration, the frames and the videos are left alone.

.PARAMETER Prefix
  Where the scripts go. Defaults to %ProgramFiles%\timelapse.

.EXAMPLE
  .\install.ps1

.EXAMPLE
  .\install.ps1 -Unattended
#>
[CmdletBinding()]
param(
    [switch]$Unattended,
    [switch]$NoWizard,
    [switch]$Uninstall,
    [string]$Prefix = (Join-Path $env:ProgramFiles 'timelapse')
)

$ErrorActionPreference = 'Stop'

$VERSION   = '0.2.1'
$SRC       = Split-Path -Parent $MyInvocation.MyCommand.Path
$CONFDIR   = Join-Path $env:ProgramData 'timelapse'
$CONFIG    = Join-Path $CONFDIR 'config.json'
$BINDIR    = Join-Path $CONFDIR 'bin'
$PYTHONURL = 'https://www.python.org/downloads/windows/'

# The seven library and entry-point scripts plus the CLI dispatcher. Listed
# rather than globbed so that a stray file in scripts/ is never installed and
# `timelapse version` has a fixed set to report on.
$SCRIPTS = @(
    'timelapse_capture.py', 'timelapse_encode.py', 'timelapse_test.py',
    'timelapse_setup.py', 'timelapse_update.py', 'timelapse_platform.py',
    'timelapse_web.py', 'timelapse_cli.py', 'timelapse_gui.py'
)

function Say  { param($m) Write-Host "  $m" }
function Note { param($m) Write-Host "  $m" -ForegroundColor DarkGray }
function Ok   { param($m) Write-Host "  OK    " -ForegroundColor Green -NoNewline; Write-Host $m }
function Warn { param($m) Write-Host "  WARN  " -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Fail { param($m) Write-Host "  FAIL  " -ForegroundColor Red -NoNewline; Write-Host $m }
function Step { param($m) Write-Host ''; Write-Host "-- $m" -ForegroundColor Cyan }
function Die  { param($m) Fail $m; exit 1 }

function Invoke-Tool {
    <#
      Run a native command and return its exit code, without letting its
      stderr become a terminating error.

      $ErrorActionPreference is Stop for this whole script, which is right for
      the cmdlets: a Copy-Item that fails must not be walked past. But for a
      native executable, PowerShell 5.1 wraps every stderr line in a
      NativeCommandError record, and under Stop that THROWS. So an entirely
      ordinary probe, `python -c import requests` on a machine that has not got
      requests, killed this installer with a traceback where it should have
      returned 1.

      Measured on a clean VM 2026-08-18, and it had been that way since the
      Windows port shipped. It survived because every machine this had ever run
      on already had requests, so the probe succeeded, wrote nothing to stderr,
      and the trap never fired. The one branch nobody had exercised was the one
      that mattered: a fresh install is the whole point of an installer.

      LASTEXITCODE is set to a sentinel first, because it is only written by a
      native command: when one fails to start, the previous call's code is
      still sitting there and would be read as this one's answer.

      The child's output is DISCARDED, always, and that is what makes the
      return value the exit code and nothing else. For a command the operator
      should watch, use Invoke-Step.
    #>
    param($Exe, [string[]]$Arguments = @())
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1
    try {
        & $Exe @Arguments 2>&1 | Out-Null
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-Step {
    <#
      Run a native command in front of the operator and leave its exit code in
      $script:StepExit. Returns nothing, and is called as a bare statement.

      Invoke-Tool used to do this too, and measured in a real console on
      2026-09-14 (temp/m24_console_probe.ps1) it could not: a native command
      inside a function writes its stdout into that function's output whenever
      the caller captures it. So `$code = Invoke-Tool` came back as the child's
      lines followed by the code, `$code -ne 0` was true on a success, and
      "Registration did not fully succeed" was printed under a registration
      whose own lines had been swallowed. `[void](Invoke-Tool ...)` threw the
      output away outright, and one of those calls was the console wizard, so
      its prompts went into a pipe nobody read. The child only reaches the
      console when nothing captures the call, which is why this returns nothing
      anybody could be tempted to capture.
    #>
    param($Exe, [string[]]$Arguments = @())
    $script:StepExit = -1
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1
    try {
        & $Exe @Arguments
        $script:StepExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# The floor, in one place: install.ps1 states it four times otherwise, and a
# version that lives in several places drifts in one of them.
$PYTHON_MIN_MINOR = 9

function Find-Python {
    <#
      An absolute path, because it is baked into the service's command line and
      a service has no PATH worth trusting.

      `py -3` first: the launcher is what a python.org install registers and it
      knows about every interpreter on the machine, including ones not on PATH.
      Then PATH itself. The Microsoft Store stub is excluded explicitly, since
      it is on PATH by default, is not an interpreter, and opens the Store when
      run, which as a service means a service that starts and does nothing.

      The floor is applied per candidate, as prepare.ps1 already did: `py -3`
      resolving to a 3.8 on a machine that also has 3.12 on PATH used to be
      returned and then refused, so an install that could have worked did not.
      A candidate that cannot be asked its version is skipped rather than
      trusted. If every candidate is too old the newest of them is returned
      anyway, so the caller can say which version it found rather than "no
      Python found", which would be false and would send the operator looking
      for the one they have.
    #>
    $found = @()
    $launcher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $out = & py -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { $found += $out.Trim() }
        } catch { }
    }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $found += $cmd.Source }
    }
    $fallback = $null
    foreach ($path in $found) {
        if ($path -like '*\WindowsApps\*') { continue }
        if (-not (Test-Path $path)) { continue }
        $version = Test-PythonVersion $path
        if (-not $version) { continue }
        $parts = $version -split '\.'
        if ([int]$parts[0] -gt 3 -or
            ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge $PYTHON_MIN_MINOR)) {
            return $path
        }
        if (-not $fallback) { $fallback = $path }
    }
    return $fallback
}

function Test-PythonVersion {
    <#
      Returns "major minor", or $null.

      Note what this one-liner does NOT contain: a quote character. PowerShell
      strips embedded double quotes when it hands arguments to a native
      executable, so `print("%d.%d" % sys.version_info[:2])` arrives at Python
      as `print(%d.%d % sys.version_info[:2])` and dies with a SyntaxError
      pointing at a percent sign. Measured on the first real install: the
      version came back empty, the version check then refused a perfectly good
      Python 3.12, and the message read "this is ." with nothing after it.

      So: no quotes in anything passed after -c, and the formatting is done on
      this side where the quoting rules are known.
    #>
    param($Python)
    # The preference is relaxed for the same reason Invoke-Tool exists: an
    # interpreter that prints a deprecation warning at startup writes to stderr,
    # and under Stop that would end the install instead of answering a question
    # about the version. This one captures stdout, so it cannot use the helper.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1
    try {
        $out = & $Python -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>$null
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $parts = $out.Trim() -split '\s+'
    if ($parts.Count -lt 2) { return $null }
    return "$($parts[0]).$($parts[1])"
}

function Test-Requests {
    # Through Invoke-Tool, never as a bare call: on a machine without requests
    # this probe writes a traceback to stderr, which is exactly what it is for,
    # and a bare call turns that into a terminating error.
    param($Python)
    return (Invoke-Tool $Python @('-c', 'import requests')) -eq 0
}

function Install-Requests {
    param($Python)
    if (Test-Requests $Python) {
        Ok 'requests is already available'
        return
    }
    Note 'Installing requests...'
    # pip writes to stderr in the ordinary course of events (a new pip version,
    # a script directory not on PATH), so this one needs the same treatment
    # even though it is expected to succeed.
    Invoke-Step $Python @('-m', 'pip', 'install', '--quiet', '--upgrade',
                          'requests')
    if ($script:StepExit -ne 0) {
        Die 'Could not install requests. Check the network, then run: python -m pip install requests'
    }
    if (-not (Test-Requests $Python)) { Die 'requests still will not import.' }
    Ok 'Installed requests'
}

function Protect-ConfigDir {
    <#
      The Windows half of "0640 root:timelapse", and it works differently in a
      way that happens to be better. There, an editor that saves by rename
      leaves root's umask on a brand new file and the config loses its group.
      Here a new file inherits the ACL of the *directory*, so restricting the
      directory once protects everything created in it afterwards, including an
      editor's temporary copies. Inheritance is broken first, or the permissive
      ACL from %ProgramData% comes along with it.

      By SID and not by name. icacls resolves a name through the local SAM, and
      on a German Windows the group is Administratoren, on a French one
      Administrateurs; the English spelling comes back as "No mapping between
      account names and security IDs was done". This used to warn and carry on,
      which would leave the directory holding every camera password with the
      permissive ACL it inherited from %ProgramData%: readable by every account
      on the machine, on exactly the installs least able to notice. The numbers
      are the same in every language.
    #>
    $code = Invoke-Tool 'icacls' @($CONFDIR, '/inheritance:r', '/grant',
                                   '*S-1-5-18:(OI)(CI)F',
                                   '*S-1-5-32-544:(OI)(CI)F')
    if ($code -eq 0) {
        Ok "Restricted $CONFDIR to SYSTEM and Administrators"
    } else {
        Warn "Could not restrict $CONFDIR; it holds your camera passwords."
        Warn 'Check it by hand before configuring any camera:'
        Warn "  icacls `"$CONFDIR`""
    }
}

function Write-Wrapper {
    param($Python)
    # Two lines, and no baked-in copy of anything that can move. The dispatcher
    # finds its siblings beside itself and the config from timelapse_platform,
    # so this wrapper cannot end up pointing at a stale layout.
    $cmd = Join-Path $BINDIR 'timelapse.cmd'
    $body = @"
@echo off
rem timelapse-maker command wrapper. Generated by install.ps1; edit nothing here.
"$Python" "$(Join-Path $Prefix 'timelapse_cli.py')" %*
"@
    [IO.File]::WriteAllText($cmd, $body, [Text.Encoding]::ASCII)
    Ok "Command wrapper -> $cmd"
}

# The machine PATH goes through the registry, not through the Environment
# class. GetEnvironmentVariable hands back the value EXPANDED, and
# SetEnvironmentVariable writes it back as REG_SZ, so a stock PATH, which is
# REG_EXPAND_SZ and full of %SystemRoot%, came out of an install with every
# entry frozen to what it expanded to that day and no longer an expandable
# value at all. Read unexpanded, written back as ExpandString.
$ENVKEY = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'

function Get-MachinePath {
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($ENVKEY)
    try {
        return [string]$key.GetValue('Path', '',
            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    } finally {
        $key.Close()
    }
}

function Set-MachinePath {
    param([string]$Value)
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($ENVKEY, $true)
    try {
        $key.SetValue('Path', $Value, [Microsoft.Win32.RegistryValueKind]::ExpandString)
    } finally {
        $key.Close()
    }
    # Writing the registry tells no running program. The Environment class
    # broadcasts WM_SETTINGCHANGE on every machine-level set, including
    # clearing a variable that was never there, so this is the broadcast
    # without compiling a P/Invoke for it. Explorer then hands the new PATH to
    # anything it starts.
    [Environment]::SetEnvironmentVariable('TIMELAPSE_MAKER_PATH_CHANGED', $null, 'Machine')
}

function Add-ToPath {
    $current = Get-MachinePath
    if ($current -split ';' -contains $BINDIR) {
        Note "$BINDIR is already on the system PATH"
        return
    }
    if ($current.TrimEnd(';')) {
        Set-MachinePath ($current.TrimEnd(';') + ';' + $BINDIR)
    } else {
        Set-MachinePath $BINDIR
    }
    Ok "Added $BINDIR to the system PATH"
    # Only new processes inherit it, and an operator typing `timelapse` in the
    # window they installed from would otherwise conclude it had not worked.
    Note 'Open a new terminal before the `timelapse` command will be found.'
}

function Remove-FromPath {
    $current = Get-MachinePath
    $entries = @($current -split ';')
    $kept = @($entries | Where-Object { $_ -and $_ -ne $BINDIR })
    if ($kept.Count -ne @($entries | Where-Object { $_ }).Count) {
        Set-MachinePath ($kept -join ';')
    }
}

function New-StartMenuShortcut {
    # The whole point of a GUI is being findable without a command, so a
    # wizard nobody can locate is a wizard nobody uses. All-users Start menu,
    # matching the all-users install everything else here does.
    #
    # Never fatal: a shortcut is a convenience, and a policy or a locked-down
    # profile that refuses one must not cost the operator their install.
    try {
        $menu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\timelapse-maker'
        New-Item -ItemType Directory -Force -Path $menu | Out-Null
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut((Join-Path $menu 'Timelapse Setup.lnk'))
        # PowerShell directly, NOT timelapse-setup.cmd. A shortcut to a .cmd
        # opens a console window and leaves it there, so the graphical wizard
        # announces itself with a terminal, which is the one thing it exists to
        # spare the operator. Reported from a real install: "running Timelapse
        # Setup displays a CLI". The .cmd stays for double-clicking in
        # Explorer, where a console is expected anyway.
        $link.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $link.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ' +
                          "-File `"$(Join-Path $Prefix 'setup-gui.ps1')`""
        $link.WorkingDirectory = $Prefix
        $link.Description = 'Configure timelapse-maker'
        $link.WindowStyle = 7
        $link.Save()
        Ok 'Start menu -> timelapse-maker \ Timelapse Setup'
    } catch {
        Warn 'Could not create the Start menu shortcut.'
        Note "Run it directly instead: $(Join-Path $Prefix 'timelapse-setup.cmd')"
    }
}

function Remove-StartMenuShortcut {
    $menu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\timelapse-maker'
    if (Test-Path $menu) {
        try {
            Remove-Item -Recurse -Force $menu
            Ok 'Removed the Start menu entry'
        } catch {
            Warn "Could not remove $menu"
        }
    }
}

function Show-ManualRemoval {
    Note 'From an Administrator prompt, remove whichever of these is left:'
    Note '  sc delete "TimelapseCapture"'
    Note '  schtasks /Delete /TN "Timelapse Encode" /F'
    Note '  schtasks /Delete /TN "Timelapse Watch" /F'
}

function Invoke-Uninstall {
    param($Python)
    Step 'Removing'
    $setup = Join-Path $Prefix 'timelapse_setup.py'
    if (Test-Path $setup) {
        Invoke-Step $Python @($setup, '--remove-units', '--output', $CONFIG)
        if ($script:StepExit -ne 0) {
            # Before a single file goes. remove_units() fails when the service
            # manager will not let go of something, a service stuck stopping
            # being the usual one, and the scripts used to be deleted anyway:
            # that left TimelapseCapture registered against a program that no
            # longer existed, failing and being restarted by its own recovery
            # actions. The files are what running this again needs.
            Write-Host ''
            Fail 'Not everything could be deregistered, so nothing has been deleted.'
            Show-ManualRemoval
            Note 'Then run this again to remove the files.'
            exit 1
        }
    } else {
        Warn 'The scripts are already gone; nothing to deregister with.'
        Show-ManualRemoval
    }
    if (Test-Path $Prefix) {
        Remove-Item -Recurse -Force $Prefix
        Ok "Removed $Prefix"
    }
    if (Test-Path $BINDIR) {
        Remove-Item -Recurse -Force $BINDIR
        Remove-FromPath
        Ok 'Removed the command wrapper and its PATH entry'
    }
    Remove-StartMenuShortcut
    Write-Host ''
    # Same promise install.sh makes: an uninstall removes the program, never
    # the recordings. Deleting a fortnight of frames because somebody wanted to
    # reinstall is not a thing this should ever do quietly.
    Note 'Left alone, deliberately:'
    Note "  $CONFDIR   your configuration, frames, videos and logs"
    Note '  Remove it yourself if you really want it gone.'
}

# --- main --------------------------------------------------------------------

# ASCII only, and this file is ASCII only throughout, which is measured by a
# test rather than promised. Windows PowerShell 5.1 reads a .ps1 with no byte
# order mark as ANSI, so the box-drawing characters install.sh uses come out as
# a wall of mojibake: the installer's very first line then looks like a
# corrupted download, which is the worst possible moment to look broken. A BOM
# would fix it and is the usual answer; ASCII fixes it without making this the
# one file in the repo that needs a different encoding rule.
Write-Host ''
Write-Host '  +----------------------------------------------------------+' -ForegroundColor Cyan
Write-Host '  |   timelapse-maker - unattended IP camera timelapses       |' -ForegroundColor Cyan
Write-Host '  +----------------------------------------------------------+' -ForegroundColor Cyan
Write-Host "  EXPERIMENTAL (v$VERSION)" -ForegroundColor Yellow -NoNewline
Write-Host ' - early software, tested on one machine.'
Note 'Config format may change between versions. Not for production use.'
Note 'The Windows build is newer still: capture and encode only, no web UI.'

if (-not (Test-Admin)) {
    Write-Host ''
    Fail 'This needs an Administrator prompt.'
    Note 'There is no sudo to suggest: privilege on Windows comes from how the'
    Note 'window was opened. Close this one, find PowerShell in the Start menu,'
    Note 'right-click it, choose "Run as administrator", and run this again.'
    exit 1
}

Step 'Interpreter'
$python = Find-Python
if (-not $python) {
    Fail 'No Python found.'
    if ($Uninstall) {
        # Deregistering is Python's job, so without one nothing can be, and
        # deleting the files regardless is what leaves a service pointing at
        # nothing. Say what is left and how to finish by hand. Under the .exe
        # uninstaller this runs hidden, so install.md says it too.
        Note 'Uninstalling needs the Python this was installed with, to'
        Note 'deregister the service and the two tasks. Nothing was removed.'
        Show-ManualRemoval
        Note "Then delete $Prefix and $BINDIR, and take $BINDIR off the system PATH."
        exit 1
    }
    Note "Install Python 3.9 or newer from $PYTHONURL"
    Note 'Tick "Add python.exe to PATH" during the install, then run this again.'
    Note 'An "install for all users" one is preferable: see the note below.'
    exit 1
}
$pyver = Test-PythonVersion $python
if (-not $pyver) {
    # An interpreter that will not report its own version is one this script
    # knows nothing about, and guessing is worse than stopping: every check
    # after this point would be measuring the wrong thing.
    Die "Could not ask $python for its version. Is it really a Python?"
}
Ok "Python $pyver at $python"

$parts = $pyver -split '\.'
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)) {
    Die "Python 3.9 or newer is needed; this is $pyver. Get one from $PYTHONURL"
}

# Before the two steps below, both of which are about *installing*. Uninstalling
# needs an interpreter to run --remove-units with and nothing else: the first
# version reached here after Install-Requests, so `-Uninstall` on a machine
# without requests would have pip-installed it on its way to removing the
# program. An uninstall must not add anything to the machine.
if ($Uninstall) {
    Invoke-Uninstall $python
    Write-Host ''
    exit 0
}

# Warned rather than refused. It demonstrably works, so refusing it would be
# calling a working install broken, which this project has done once and does
# not intend to repeat. But it is worth saying out loud: the service runs as
# LocalSystem and reads the interpreter from another account's profile, so
# removing that profile, or reinstalling Python for a different user, stops the
# service starting, weeks later, with no obvious connection to the cause.
if ($python -like "$env:LOCALAPPDATA*" -or $python -like '*\Users\*') {
    Warn 'This Python is installed for one user, not for the whole machine.'
    Note 'It works, and the capture service will read it fine. But if that'
    Note 'user profile is ever removed, or Python is reinstalled for a'
    Note 'different user, the service stops starting and nothing will connect'
    Note 'the two events. An "install for all users" Python under Program'
    Note 'Files avoids that. Reinstalling later is easy: re-run this script.'
}

Install-Requests $python

Step 'Installing'
foreach ($name in $SCRIPTS) {
    if (-not (Test-Path (Join-Path $SRC "scripts\$name"))) {
        Die "Missing $name - run this from an unpacked release, not on its own."
    }
}
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
New-Item -ItemType Directory -Force -Path $CONFDIR | Out-Null
New-Item -ItemType Directory -Force -Path $BINDIR | Out-Null
foreach ($name in $SCRIPTS) {
    Copy-Item (Join-Path $SRC "scripts\$name") (Join-Path $Prefix $name) -Force
}
Ok "Scripts -> $Prefix"

# The graphical wizard's launcher. Copied only when it is there, so that an
# older release tree or a partial checkout installs everything else rather than
# dying on a file this one added.
foreach ($name in @('setup-gui.ps1', 'timelapse-setup.cmd')) {
    $from = Join-Path $SRC $name
    if (Test-Path $from) { Copy-Item $from (Join-Path $Prefix $name) -Force }
}
if (Test-Path (Join-Path $Prefix 'timelapse-setup.cmd')) {
    Ok "Setup wizard -> $(Join-Path $Prefix 'timelapse-setup.cmd')"
    New-StartMenuShortcut
}

# Refreshed on every install, which is the only place an operator sees keys
# added by a new version: the real config is kept as it is, on purpose.
Copy-Item (Join-Path $SRC 'config\config.example.json') `
          (Join-Path $CONFDIR 'config.example.json') -Force
Ok "Template -> $(Join-Path $CONFDIR 'config.example.json')"

Protect-ConfigDir
Write-Wrapper $python
Add-ToPath

Step 'Services'
# The one thing this script must not reimplement. --install-units builds the
# service command line and both task definitions from one table in Python, so
# there is no second copy here to drift from it.
Invoke-Step $python @((Join-Path $Prefix 'timelapse_setup.py'),
                      '--install-units', '--scripts-dir', $Prefix,
                      '--output', $CONFIG)
if ($script:StepExit -ne 0) {
    Warn 'Registration did not fully succeed; see above.'
}

Step 'Configuration'
if (Test-Path $CONFIG) {
    # An upgrade never reconfigures, which is the rule install.sh states and
    # the behaviour it has: reconfiguring is a separate job with its own
    # commands, and being walked through the whole wizard is a strange thing to
    # be offered by something you ran to get a bug fix. Worse here than there,
    # because these questions include every camera password.
    #
    # New keys arrive with defaults, so an untouched config keeps working, and
    # config.example.json above was refreshed so the new ones can be read.
    Note "Keeping the existing configuration at $CONFIG"
    Note 'To change it, either:'
    Note '  Start menu -> timelapse-maker -> Timelapse Setup   (a window)'
    Note '  timelapse setup   (or: timelapse cameras)          (a prompt)'
} elseif ($NoWizard) {
    Note 'Skipped. Configure it with:  timelapse setup'
    Note 'or from the Start menu: timelapse-maker -> Timelapse Setup'
} else {
    # Not $args: that is an automatic variable in PowerShell, and assigning to
    # it works at script scope and quietly does not inside a function, which is
    # the kind of difference that shows up only after this file grows one.
    $wizard = @('--output', $CONFIG, '--template',
                (Join-Path $CONFDIR 'config.example.json'))
    if ($Unattended) { $wizard += '--defaults' }
    # Interactive, so a bare Invoke-Step: captured in any way, its prompts go
    # into a pipe instead of the window (see Invoke-Step).
    Invoke-Step $python (@((Join-Path $Prefix 'timelapse_setup.py')) + $wizard)
}

# Protect-ConfigDir closed this whole tree, and by default the finished videos
# are inside it, so without this the operator cannot double-click their own
# timelapse. The rule for which directory and when lives in Python beside the
# wizard that creates it, not here: the wizard reaches the same function when
# it makes the directories, and this covers the upgrade, which never runs the
# wizard at all. A first install has no config yet and this exits 0; the wizard
# a few lines above has already done it by then.
Invoke-Step $python @((Join-Path $Prefix 'timelapse_setup.py'),
                      '--grant-video-access', $CONFIG)

# After the wizard, never during registration. Registering replaces the files
# on disk and leaves the running process alone; the wizard then rewrites the
# config underneath it. Restarting any earlier picks up the new build with the
# old settings, which is the worst of the three possible orderings because
# nothing about it looks wrong.
Invoke-Step $python @((Join-Path $Prefix 'timelapse_setup.py'),
                      '--restart-units')

Step 'Next steps'
# Both halves of this were wrong to leave implicit. NEW, because a PATH change
# reaches only processes started after it, and ADMINISTRATOR, because of the
# directory the wrapper lives in. Telling somebody to open a terminal and then
# watching them be refused is a worse first five minutes than saying so.
#
# This used to name `status` and `version` as needing neither, on the reasoning
# that they read nothing secret. That was wrong, and measured: `timelapse.cmd`
# sits inside the directory Protect-ConfigDir restricts to SYSTEM and
# Administrators, and under UAC's split token an allow entry for Administrators
# does not apply to an unelevated administrator. So cmd answers `Access is
# denied` before any of this program runs, and the four words are not ours.
# Every command needs the elevated prompt, so say that once rather than
# drawing a line the operator will fall over.
Say 'To change the settings without a prompt at all:'
Say '  Start menu -> timelapse-maker -> Timelapse Setup'
Note 'The same questions in a window. It asks for Administrator itself.'
Write-Host ''
Say 'Open a NEW Command Prompt or PowerShell, AS ADMINISTRATOR, then:'
Say '  timelapse test        check the cameras, ffmpeg and the disk'
Say '  timelapse cameras     add or edit a camera'
Say '  timelapse status      is the service running'
Say '  timelapse help        every command'
Note 'New, because PATH only reaches windows opened after it changed.'
Note 'Administrator for all of them, including the ones that only look:'
Note 'timelapse lives in the folder holding your camera passwords, and an'
Note 'ordinary window is refused by Windows before it starts.'
Write-Host ''
Say 'Start capturing:'
Say '  sc start "TimelapseCapture"'
Note 'It is registered to start automatically after the network is up, so'
Note 'this is only needed before the first reboot.'
Write-Host ''
