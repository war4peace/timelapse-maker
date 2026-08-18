# timelapse-maker: the prerequisite stage behind the graphical installer.
#
# ASCII only, like install.ps1 and setup-gui.ps1, and for the same measured
# reason: Windows PowerShell 5.1 reads a .ps1 with no byte order mark as ANSI,
# so anything outside ASCII arrives as mojibake.
#
# WHAT THIS IS FOR
#
# install.ps1 states three things it deliberately does not do, and two of them
# are prerequisites rather than installation: it does not install Python, and
# it does not install ffmpeg. Both refusals are right for install.ps1, which is
# run from a checkout by somebody who already has a terminal open. Neither is
# right for a .exe downloaded from a release page, where "go and install Python
# first, then run me again" is the whole experience of the thing.
#
# So this stage answers those two questions and nothing else. It does not place
# a file, register a service or write a config: it makes sure the tools exist,
# and then hands over to install.ps1, which is the only program here that knows
# how to install anything. That is the rule tools/ was deleted over and the one
# item 11c.6b restates for the GUI. Two programs that both know how to register
# a service disagree within one release.
#
# WHAT IT REFUSES TO DO
#
# It never replaces something that is already there. A recorder very often has
# an ffmpeg the operator chose deliberately, and overriding that choice is what
# item 11c.6a refused; the same goes for an existing Python. Both downloads
# happen only when the machine has none, and only when the operator ticked the
# box for it. The narrow case this exists for is a clean Windows box, where the
# alternative is an installer that finishes and leaves no working product.
#
# Every download is checked against installer/prerequisites.json before it is
# run: the size first, then SHA-256. The size is not redundant. A 404 page, a
# captive portal and a proxy error all arrive as a successful HTTP response,
# which is a trap this project has already met in `timelapse update`, and a
# four kilobyte "sign in to continue" page reported as a hash mismatch reads as
# a corrupt download rather than as a hotel wifi.

[CmdletBinding()]
param(
    # Permission, not instruction. Each download happens only if the tool is
    # missing AND the operator agreed to it, which is why the installer can
    # offer both as ticked checkboxes without ever overriding a real install.
    [switch]$AllowPython,
    [switch]$AllowFfmpeg,

    # Prerequisites only, stopping before install.ps1. For testing this stage
    # on a machine that is not being installed onto.
    [switch]$NoInstall,

    # Both are worked out below rather than defaulted here. $PSScriptRoot is
    # not reliably populated while a param block is being bound on Windows
    # PowerShell 5.1, so `Split-Path -Parent $PSScriptRoot` as a default fails
    # at the bind with "cannot bind argument to parameter Path because it is an
    # empty string", before a single line of this script has run. Measured, on
    # the first real invocation: the error names Split-Path and reads as a bug
    # in the argument somebody passed rather than in the default they did not.
    [string]$Root = '',
    [string]$Log = ''
)

$ErrorActionPreference = 'Stop'

$Here = $PSScriptRoot
if (-not $Here) { $Here = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Root) { $Root = Split-Path -Parent $Here }
if (-not $Log) { $Log = Join-Path $env:ProgramData 'timelapse\install.log' }

# 5.1 negotiates SSL3 and TLS 1.0 by default on some builds, and both python.org
# and gyan.dev refuse those, so without this line every download fails with a
# connection error that says nothing about TLS.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Invoke-WebRequest renders a progress bar per chunk, which on 5.1 costs more
# time than the transfer does: a 111 MB download takes minutes longer with it
# than without. Nothing is watching anyway, since the installer runs this hidden.
$ProgressPreference = 'SilentlyContinue'

$PYTHONURL = 'https://www.python.org/downloads/windows/'
$FFMPEGURL = 'https://ffmpeg.org/download.html#build-windows'

# Where a downloaded ffmpeg goes. Under the installer's own directory rather
# than in %ProgramData% with the config and the frames, so that uninstalling
# removes it: [UninstallDelete] in the .iss owns this path. It is also what
# ffmpeg_roots() in timelapse_platform.py looks in, which is how the wizard
# comes to offer it as the default answer without being told.
$FFMPEGDIR = Join-Path $Root 'ffmpeg'

# UTF-8 with no byte order mark. Add-Content -Encoding UTF8 writes one on 5.1,
# and this log is what gets pasted into a bug report, where a leading EF BB BF
# turns the first line into mojibake and makes the tool look broken at exactly
# the moment somebody is already reporting that it is. Same family as the .ps1
# encoding rule, one layer out.
$LOGENC = New-Object Text.UTF8Encoding($false)

function Write-Log {
    param($Text, $Colour = 'Gray')
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    try {
        $dir = Split-Path -Parent $Log
        if (-not (Test-Path $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        [IO.File]::AppendAllText($Log, "$stamp  $Text" + [Environment]::NewLine,
                                 $LOGENC)
    } catch {
        # A log that cannot be written must not stop an install. The console
        # copy below is still there, and so is Inno's own log.
    }
    Write-Host "  $Text" -ForegroundColor $Colour
}

function Ok   { param($m) Write-Log "OK    $m" 'Green' }
function Warn { param($m) Write-Log "WARN  $m" 'Yellow' }
function Note { param($m) Write-Log "      $m" 'DarkGray' }
function Fail { param($m) Write-Log "FAIL  $m" 'Red' }

function Die {
    param($m)
    Fail $m
    Note "The full log is at $Log"
    exit 1
}

function Get-Pins {
    <#
      One table, read by three things: this script, tests/test_installer.py and
      the release workflow, which HEADs both URLs so a pin that has gone stale
      is found before a release rather than by the first person to run it.
    #>
    $path = Join-Path $Here 'prerequisites.json'
    if (-not (Test-Path $path)) {
        Die "prerequisites.json is missing from $Here"
    }
    return (Get-Content -Raw -Path $path | ConvertFrom-Json)
}

function Get-Verified {
    <#
      Download to a temporary file and prove it before anything runs it.

      Returns the path, or $null. The size is checked first because it is the
      cheap one and because it separates the two failures worth telling apart:
      a wrong size is almost always a portal or a proxy answering instead of the
      server, and a right size with a wrong hash is the asset having changed.
    #>
    param($Url, $Sha256, $Bytes, $Label)

    $file = Join-Path $env:TEMP ('timelapse-' + [IO.Path]::GetFileName($Url))
    if (Test-Path $file) { Remove-Item $file -Force -ErrorAction SilentlyContinue }

    Note "Downloading $Label ($([math]::Round($Bytes / 1MB)) MB)..."
    try {
        Invoke-WebRequest -Uri $Url -OutFile $file -UseBasicParsing
    } catch {
        Fail "Could not download $Label."
        Note $_.Exception.Message
        return $null
    }

    $got = (Get-Item $file).Length
    if ($got -ne $Bytes) {
        Fail "$Label is $got bytes; the pinned size is $Bytes."
        if ($got -lt 100000) {
            Note 'That is small enough to be a sign-in page or a proxy error'
            Note 'rather than the file. Check the network and try again.'
        }
        Remove-Item $file -Force -ErrorAction SilentlyContinue
        return $null
    }

    $hash = (Get-FileHash -Path $file -Algorithm SHA256).Hash
    if ($hash -ne $Sha256.ToUpper()) {
        Fail "$Label does not match its pinned SHA-256."
        Note "expected $($Sha256.ToUpper())"
        Note "got      $hash"
        Remove-Item $file -Force -ErrorAction SilentlyContinue
        return $null
    }

    Ok "Verified $Label"
    return $file
}

function Find-Python {
    <#
      The same order install.ps1 and setup-gui.ps1 use, and it is a third copy
      on purpose: this runs before either of them exists on the machine, and a
      shared module would be one more thing to place first. The properties that
      matter are pinned across all three by a test rather than by a promise.

      `py -3` first, because the launcher knows about every interpreter on the
      machine including ones not on PATH. WindowsApps is skipped: that entry is
      a stub that opens the Microsoft Store, and as a service command line it
      is a service that starts and does nothing.

      Note that neither -c snippet contains a double quote. PowerShell strips
      those on the way to a native executable, which cost eleven of eighteen
      checks on the first real elevated install.
    #>
    $candidates = @()
    if (Get-Command 'py' -ErrorAction SilentlyContinue) {
        try {
            $out = & py -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { $candidates += $out.Trim() }
        } catch { }
    }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -and $cmd.Source -notlike '*\WindowsApps\*') {
            $candidates += $cmd.Source
        }
    }
    foreach ($path in $candidates) {
        if (-not (Test-Path $path)) { continue }
        $version = Get-PythonVersion $path
        if ($version -and (Test-Floor $version)) { return $path }
    }
    return $null
}

function Get-PythonVersion {
    param($Python)
    try {
        $out = & $Python -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $parts = $out.Trim() -split '\s+'
    if ($parts.Count -lt 2) { return $null }
    return "$($parts[0]).$($parts[1])"
}

function Test-Floor {
    # 3.9 is the floor everywhere in this project: RHEL 9 and Debian 11 ship it
    # as the system python3. An older one is refused rather than worked around.
    param($Version)
    $parts = $Version -split '\.'
    if ([int]$parts[0] -lt 3) { return $false }
    return -not ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)
}

function Install-Python {
    param($Pin)

    $target = Join-Path $env:ProgramFiles ('Python' + ($Pin.version -split '\.')[0] +
                                           ($Pin.version -split '\.')[1])
    $file = Get-Verified $Pin.url $Pin.sha256 $Pin.bytes "Python $($Pin.version)"
    if (-not $file) { return $null }

    Note "Installing Python $($Pin.version) to $target..."
    # InstallAllUsers, because the capture service runs as LocalSystem and reads
    # this interpreter from a service command line: an install inside one user's
    # profile works until that profile is removed, and nothing then connects the
    # two events. Include_tcltk, because the graphical wizard is tkinter and a
    # Python without it produces a wizard that cannot open. Include_launcher,
    # because `py -3` is the first thing every stage here looks for.
    $arguments = @('/quiet', 'InstallAllUsers=1', 'PrependPath=1',
                   'Include_launcher=1', 'Include_tcltk=1', 'Include_pip=1',
                   'Include_test=0', 'Include_doc=0', 'AssociateFiles=0',
                   'Shortcuts=0', "TargetDir=$target")
    $proc = Start-Process -FilePath $file -ArgumentList $arguments -Wait -PassThru
    Remove-Item $file -Force -ErrorAction SilentlyContinue

    # 3010 is "installed, reboot wanted". The interpreter works now, so treating
    # it as a failure would refuse a machine that is ready.
    if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) {
        Fail "The Python installer exited $($proc.ExitCode)."
        if ($proc.ExitCode -eq 1602) { Note 'That code means it was cancelled.' }
        return $null
    }

    $python = Join-Path $target 'python.exe'
    if (-not (Test-Path $python)) {
        Fail "The Python installer reported success but $python is not there."
        return $null
    }
    return $python
}

function Find-Ffmpeg {
    <#
      Asked of the Python that will run the wizard, rather than answered again
      here, so that the two cannot disagree. find_tool() checks PATH first and
      then the handful of places a Windows ffmpeg actually lives, which is the
      same answer the wizard is about to offer as its default. A second
      implementation in PowerShell would be a second opinion, and the one that
      matters is the one the product uses.
    #>
    param($Python)
    $platform = Join-Path $Root 'scripts\timelapse_platform.py'
    if (-not (Test-Path $platform)) { return $null }
    # Relaxed for the same reason as everywhere else here: this call is
    # EXPECTED to fail on a machine with no ffmpeg, that failure is the answer,
    # and under Stop a traceback on stderr would end the install instead.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1
    try {
        $out = (& $Python $platform --find-tool ffmpeg 2>$null | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    return $out
}

function Install-Ffmpeg {
    param($Pin)

    $file = Get-Verified $Pin.url $Pin.sha256 $Pin.bytes "ffmpeg $($Pin.version)"
    if (-not $file) { return $null }

    $unpack = Join-Path $env:TEMP 'timelapse-ffmpeg-unpack'
    if (Test-Path $unpack) { Remove-Item $unpack -Recurse -Force }
    Note 'Unpacking ffmpeg...'
    try {
        Expand-Archive -Path $file -DestinationPath $unpack -Force
    } catch {
        Fail 'Could not unpack the ffmpeg archive.'
        Note $_.Exception.Message
        return $null
    }
    Remove-Item $file -Force -ErrorAction SilentlyContinue

    $bin = Join-Path $unpack ($Pin.binaries.Replace('/', '\'))
    if (-not (Test-Path $bin)) {
        # The layout inside the zip is pinned too, and this is what happens when
        # the vendor changes it: say which path was expected, rather than
        # reporting a missing ffmpeg.exe with no explanation.
        Fail "The archive does not contain $($Pin.binaries)."
        return $null
    }

    if (Test-Path $FFMPEGDIR) { Remove-Item $FFMPEGDIR -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $FFMPEGDIR | Out-Null
    Move-Item -Path $bin -Destination (Join-Path $FFMPEGDIR 'bin') -Force
    Remove-Item $unpack -Recurse -Force -ErrorAction SilentlyContinue

    # Verified by running it, which is the rule everywhere else in this project:
    # a probe must produce what the pipeline produces. An ffmpeg.exe that is
    # present and will not start is a nightly encode that fails at midnight.
    $exe = Join-Path $FFMPEGDIR 'bin\ffmpeg.exe'
    if (-not (Test-Path $exe)) {
        Fail "ffmpeg.exe is not at $exe after unpacking."
        return $null
    }
    # Everything it says is kept, and that is a rule this project already had
    # and this function broke: never discard a probe's stderr. "Unknown
    # encoder" and "No capable devices found" need opposite fixes and share an
    # exit code, and the same applies to a binary that will not start at all,
    # where the message is the only thing separating a download the machine
    # blocked from a missing runtime. The first version reported "will not run"
    # and nothing else, which told an operator a conclusion and no evidence.
    #
    # The error preference is relaxed around it because under Stop a native
    # command's stderr is a terminating error, and keyed on the banner rather
    # than on $LASTEXITCODE because that variable is only written by a process
    # that actually started: one that did not leaves the previous call's code
    # sitting there to be misread as this one's answer.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1
    try {
        $said = (& $exe -version 2>&1 | Out-String)
    } catch {
        $said = $_.Exception.Message
    } finally {
        $ErrorActionPreference = $previous
    }
    $code = $LASTEXITCODE
    $banner = @($said -split "`r?`n" |
                Where-Object { $_ -match '^ffmpeg version' }) |
              Select-Object -First 1

    if (-not $banner) {
        Fail 'ffmpeg unpacked but will not run.'
        Note "exit code $code, from $exe"
        foreach ($line in @($said -split "`r?`n" |
                            Where-Object { $_.Trim() } |
                            Select-Object -First 6)) {
            Note $line.Trim()
        }
        if (-not $said.Trim()) {
            Note 'It said nothing at all, which usually means the process never'
            Note 'started: antivirus, or a policy blocking a downloaded binary.'
        }
        return $null
    }

    Note "$banner"
    return $exe
}

# --- main ---------------------------------------------------------------------

Write-Log '---- prerequisite stage ----'
$pins = Get-Pins

Write-Log 'Looking for Python'
$python = Find-Python
if ($python) {
    Ok "Python $(Get-PythonVersion $python) at $python"
} elseif ($AllowPython) {
    Note 'No Python 3.9 or newer on this machine.'
    $python = Install-Python $pins.python
    if (-not $python) {
        Die "Could not install Python. Install it yourself from $PYTHONURL and run this again."
    }
    Ok "Python $($pins.python.version) at $python"
} else {
    Fail 'Python 3.9 or newer is required and this machine has none.'
    Note "Install it from $PYTHONURL, then run the installer again."
    Note 'Or run the installer again and leave the Python box ticked.'
    exit 1
}

Write-Log 'Looking for ffmpeg'
$ffmpeg = Find-Ffmpeg $python
if ($ffmpeg) {
    # Deliberately not replaced, and this is the whole of item 11c.6a: the
    # operator's own build is very often a deliberate choice, made for NVENC or
    # for a codec, and a second copy installed over the top of it is this
    # program overruling a decision that was not its own.
    Ok "ffmpeg at $ffmpeg"
    Note 'Left as it is: an ffmpeg already on this machine is your choice, not ours.'
} elseif ($AllowFfmpeg) {
    Note 'No ffmpeg on this machine.'
    $ffmpeg = Install-Ffmpeg $pins.ffmpeg
    if ($ffmpeg) {
        Ok "ffmpeg at $ffmpeg"
    } else {
        # A warning rather than a stop. Everything else can still be installed
        # and configured, and the wizard asks for the path anyway, so a failed
        # download costs the operator one question rather than the install.
        Warn 'Could not install ffmpeg.'
        Note "Get a build from $FFMPEGURL and give the wizard its path."
    }
} else {
    Warn 'No ffmpeg found, and the box for installing one was not ticked.'
    Note "Get a build from $FFMPEGURL and give the wizard its path."
    Note 'Without ffmpeg nothing can be encoded, so this is worth doing first.'
}

if ($NoInstall) {
    Write-Log 'Stopping before install.ps1, as asked.'
    exit 0
}

# --- hand over ----------------------------------------------------------------

Write-Log 'Running install.ps1'
$installer = Join-Path $Root 'install.ps1'
if (-not (Test-Path $installer)) { Die "install.ps1 is not at $installer" }

# Its own process, with both streams into files, for one reason worth stating:
# this whole stage runs hidden behind the installer's progress bar, so anything
# install.ps1 says goes nowhere unless it is captured. That is the same trap as
# a service writing to a stderr the SCM discards, and it is answered the same
# way. -NoWizard because the graphical wizard is offered on the finish page: a
# console asking for camera passwords is what the .exe exists to replace.
$out = Join-Path $env:TEMP 'timelapse-install-out.log'
$err = Join-Path $env:TEMP 'timelapse-install-err.log'
$proc = Start-Process -FilePath 'powershell.exe' -Wait -PassThru -NoNewWindow `
    -RedirectStandardOutput $out -RedirectStandardError $err `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', "`"$installer`"", '-NoWizard')

foreach ($stream in @($out, $err)) {
    if (Test-Path $stream) {
        $text = (Get-Content -Raw -Path $stream)
        if ($text -and $text.Trim()) {
            [IO.File]::AppendAllText($Log, $text.TrimEnd() +
                                     [Environment]::NewLine, $LOGENC)
        }
        Remove-Item $stream -Force -ErrorAction SilentlyContinue
    }
}

if ($proc.ExitCode -ne 0) {
    Die "install.ps1 exited $($proc.ExitCode)."
}

Write-Log 'Done.'
exit 0
