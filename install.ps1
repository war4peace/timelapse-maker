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

$VERSION   = '0.2.0'
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
    #>
    param($Exe, [string[]]$Arguments = @(), [switch]$Quiet)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1
    try {
        if ($Quiet) {
            & $Exe @Arguments 2>&1 | Out-Null
        } else {
            & $Exe @Arguments
        }
        return $LASTEXITCODE
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

function Find-Python {
    <#
      An absolute path, because it is baked into the service's command line and
      a service has no PATH worth trusting.

      `py -3` first: the launcher is what a python.org install registers and it
      knows about every interpreter on the machine, including ones not on PATH.
      Then PATH itself. The Microsoft Store stub is excluded explicitly, since
      it is on PATH by default, is not an interpreter, and opens the Store when
      run, which as a service means a service that starts and does nothing.
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
    foreach ($path in $found) {
        if ($path -like '*\WindowsApps\*') { continue }
        if (Test-Path $path) { return $path }
    }
    return $null
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
    return (Invoke-Tool $Python @('-c', 'import requests') -Quiet) -eq 0
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
    if ((Invoke-Tool $Python @('-m', 'pip', 'install', '--quiet', '--upgrade',
                               'requests')) -ne 0) {
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
    #>
    $code = Invoke-Tool 'icacls' @($CONFDIR, '/inheritance:r', '/grant',
                                   'SYSTEM:(OI)(CI)F',
                                   'Administrators:(OI)(CI)F') -Quiet
    if ($code -eq 0) {
        Ok "Restricted $CONFDIR to SYSTEM and Administrators"
    } else {
        Warn "Could not restrict $CONFDIR; it holds your camera passwords."
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

function Add-ToPath {
    $current = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if ($current -split ';' -contains $BINDIR) {
        Note "$BINDIR is already on the system PATH"
        return
    }
    $joined = ($current.TrimEnd(';') + ';' + $BINDIR)
    [Environment]::SetEnvironmentVariable('Path', $joined, 'Machine')
    Ok "Added $BINDIR to the system PATH"
    # Only new processes inherit it, and an operator typing `timelapse` in the
    # window they installed from would otherwise conclude it had not worked.
    Note 'Open a new terminal before the `timelapse` command will be found.'
}

function Remove-FromPath {
    $current = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $kept = ($current -split ';' | Where-Object { $_ -and $_ -ne $BINDIR })
    [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'Machine')
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

function Invoke-Uninstall {
    param($Python)
    Step 'Removing'
    $setup = Join-Path $Prefix 'timelapse_setup.py'
    if (Test-Path $setup) {
        [void](Invoke-Tool $Python @($setup, '--remove-units',
                                     '--output', $CONFIG))
    } else {
        Warn 'The scripts are already gone; nothing to deregister with.'
        Note 'If a service or task is left over, remove it by hand:'
        Note '  sc delete "TimelapseCapture"'
        Note '  schtasks /Delete /TN "Timelapse Encode" /F'
        Note '  schtasks /Delete /TN "Timelapse Watch" /F'
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
$code = Invoke-Tool $python @((Join-Path $Prefix 'timelapse_setup.py'),
                              '--install-units', '--scripts-dir', $Prefix,
                              '--output', $CONFIG)
if ($code -ne 0) {
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
    # Interactive, so its output and its prompts are left alone; only the
    # error preference is relaxed, and that by Invoke-Tool rather than here.
    [void](Invoke-Tool $python (@((Join-Path $Prefix 'timelapse_setup.py')) + $wizard))
}

# After the wizard, never during registration. Registering replaces the files
# on disk and leaves the running process alone; the wizard then rewrites the
# config underneath it. Restarting any earlier picks up the new build with the
# old settings, which is the worst of the three possible orderings because
# nothing about it looks wrong.
[void](Invoke-Tool $python @((Join-Path $Prefix 'timelapse_setup.py'),
                             '--restart-units'))

Step 'Next steps'
# Both halves of this were wrong to leave implicit. NEW, because a PATH change
# reaches only processes started after it, and ADMINISTRATOR, because the two
# commands named here read the configuration file that holds the camera
# passwords and refuse without it. Telling somebody to open a terminal and then
# watching them be refused is a worse first five minutes than saying so.
Say 'To change the settings without a prompt at all:'
Say '  Start menu -> timelapse-maker -> Timelapse Setup'
Note 'The same questions in a window. It asks for Administrator itself.'
Write-Host ''
Say 'Open a NEW Command Prompt or PowerShell, AS ADMINISTRATOR, then:'
Say '  timelapse test        check the cameras, ffmpeg and the disk'
Say '  timelapse cameras     add or edit a camera'
Note 'New, because PATH only reaches windows opened after it changed.'
Note 'Administrator, because those two read the file holding your camera'
Note 'passwords. These need neither, from any window:'
Say '  timelapse status      is the service running'
Say '  timelapse version     what is installed'
Write-Host ''
Say 'Start capturing:'
Say '  sc start "TimelapseCapture"'
Note 'It is registered to start automatically after the network is up, so'
Note 'this is only needed before the first reboot.'
Write-Host ''
