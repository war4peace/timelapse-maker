# timelapse-maker: launcher for the graphical setup wizard.
#
# ASCII only, like install.ps1 and for the same measured reason: Windows
# PowerShell 5.1 reads a .ps1 with no byte order mark as ANSI, so anything
# outside ASCII arrives as mojibake and the first thing the operator sees looks
# like a corrupted download.
#
# This exists because of an ordering problem the GUI cannot solve itself. The
# wizard is written in Python, and the person it is for may not have Python, so
# something that is not Python has to be able to say so. PowerShell is on every
# supported Windows and needs no install, which makes it the only thing that
# can deliver that message.
#
# It decides nothing else. Finding Python, elevating, and starting the wizard
# is the whole job; every question, check and write belongs to the Python side,
# for the same reason install.ps1 contains no sc.exe.

[CmdletBinding()]
param(
    [string]$Prefix = (Join-Path $env:ProgramFiles 'timelapse'),
    [switch]$NoElevate
)

$ErrorActionPreference = 'Stop'
$PYTHONURL = 'https://www.python.org/downloads/windows/'

Add-Type -AssemblyName System.Windows.Forms | Out-Null

function Show-Problem($text) {
    [System.Windows.Forms.MessageBox]::Show(
        $text, 'timelapse-maker setup',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
}

function Find-Python {
    # Same order install.ps1 uses: the launcher first, because it knows about
    # every install on the machine, then PATH. The WindowsApps entry is
    # excluded deliberately: it is a stub that opens the Store rather than an
    # interpreter, and it reports a version quite happily first.
    $candidates = @()
    $launcher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $found = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $candidates += $found.Trim() }
        } catch { }
    }
    foreach ($cmd in @('python', 'python3')) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found -and $found.Source -notlike '*WindowsApps*') {
            $candidates += $found.Source
        }
    }
    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) { return $path }
    }
    return $null
}

$gui = Join-Path $Prefix 'timelapse_gui.py'
if (-not (Test-Path $gui)) {
    Show-Problem ("timelapse-maker is not installed at $Prefix." + [Environment]::NewLine +
                  [Environment]::NewLine +
                  "Run install.ps1 from an Administrator PowerShell prompt first," + [Environment]::NewLine +
                  "then start this again.")
    exit 1
}

$python = Find-Python
if (-not $python) {
    Show-Problem ("Python is needed and was not found on this machine." + [Environment]::NewLine +
                  [Environment]::NewLine +
                  "Install Python 3.9 or newer from:" + [Environment]::NewLine +
                  $PYTHONURL + [Environment]::NewLine +
                  [Environment]::NewLine +
                  "Choose 'install for all users' if the option is offered, so" + [Environment]::NewLine +
                  "the background services can find it too.")
    exit 1
}

# Setup writes the configuration, which holds camera passwords and lives in a
# directory only administrators may read, so elevate before asking anything
# rather than after thirty answers.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $admin -and -not $NoElevate) {
    $self = $MyInvocation.MyCommand.Path
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
                   '-File', "`"$self`"", '-Prefix', "`"$Prefix`"")
    try {
        # Hidden, or the elevated relaunch puts a console window on screen in
        # front of the wizard, which is the thing this whole entry point exists
        # to avoid: an operator who wanted to click sees a terminal instead.
        Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments `
                      -Verb RunAs -WindowStyle Hidden
    } catch {
        Show-Problem ("Setup needs Administrator rights and the prompt was refused." +
                      [Environment]::NewLine + [Environment]::NewLine +
                      "Right-click this file and choose 'Run with PowerShell'" +
                      [Environment]::NewLine + "from an administrator account.")
        exit 1
    }
    exit 0
}

# pythonw so no console window flashes up behind the wizard. Falling back to
# python.exe rather than failing: a window that is merely ugly beats no wizard.
$quiet = Join-Path (Split-Path $python -Parent) 'pythonw.exe'
if (-not (Test-Path $quiet)) { $quiet = $python }

# stderr goes to a file, and that is not tidiness. pythonw.exe has no console
# at all, so anything that fails before the window opens is discarded and the
# operator sees precisely nothing happen: the same class of trap as a service
# writing to a stderr that goes nowhere. A NameError in the wizard's entry
# point shipped exactly this way and was invisible until it was run with
# python.exe instead.
$errlog = Join-Path $env:TEMP 'timelapse-setup-error.log'
if (Test-Path $errlog) { Remove-Item $errlog -Force -ErrorAction SilentlyContinue }

$proc = Start-Process -FilePath $quiet -ArgumentList "`"$gui`"" `
                      -Wait -PassThru -WindowStyle Hidden `
                      -RedirectStandardError $errlog

$detail = ''
if (Test-Path $errlog) { $detail = (Get-Content $errlog -Raw) }

# Keyed on there being output rather than on the exit code: exit 1 is also what
# closing the wizard without saving reports, and a message box every time
# somebody clicks Cancel would be worse than none.
if ($detail -and $detail.Trim()) {
    $lines = $detail.Trim() -split "`r?`n"
    $tail = ($lines | Select-Object -Last 6) -join [Environment]::NewLine
    Show-Problem ("The setup wizard stopped before it could finish." +
                  [Environment]::NewLine + [Environment]::NewLine + $tail +
                  [Environment]::NewLine + [Environment]::NewLine +
                  "The full text is in:" + [Environment]::NewLine + $errlog)
}

exit $proc.ExitCode
