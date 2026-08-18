; timelapse-maker: the Windows installer, compiled by Inno Setup 6.
;
; ASCII only, like every .ps1 this project ships. Inno itself is happy with
; UTF-8, but the rule exists so that no file here needs its own encoding note,
; and mojibake in an installer's first line is the worst possible moment to
; look like a corrupted download.
;
; WHAT THIS FILE DOES, AND WHAT IT MUST NEVER DO
;
; It lays the release tree down, and then it calls installer\prepare.ps1, which
; makes sure Python and ffmpeg exist and then calls install.ps1, which is the
; only program in this project that knows how to install anything. Nothing in
; this file registers a service, writes a scheduled task, sets an ACL or writes
; a configuration file. That is the rule tools/ was deleted over and the one
; item 11c.6b restates for the GUI: two installers that both know how to
; register a service disagree within one release, and the one that drifts is
; the one nobody runs from a terminal.
;
; A test in tests/test_installer.py holds this file to that: no sc.exe, no
; schtasks, no icacls, and install.ps1 named on both the install and the
; uninstall path.
;
; TWO DIRECTORIES, ON PURPOSE
;
;   {app}                        Program Files\timelapse-maker
;                                the release tree, the docs, the uninstaller,
;                                and a downloaded ffmpeg if there was none.
;   Program Files\timelapse      the scripts that actually run, placed there by
;                                install.ps1, which owns that path and has
;                                since the first Windows release.
;
; They are separate because the uninstaller lives in the first and deletes the
; second: install.ps1 -Uninstall does a recursive delete of its own prefix, and
; a program that deletes the directory it is running from mid-uninstall is a
; different kind of bug report.

#ifndef AppVersion
  ; The release workflow passes /DAppVersion=<tag>, so this default only ever
  ; applies to a local build. It is checked against install.ps1's $VERSION by a
  ; test, because a version that lives in ten places drifts in one of them.
  #define AppVersion "0.2.0"
#endif

#define AppName "timelapse-maker"
#define AppPublisher "war4peace"
#define AppURL "https://github.com/war4peace/timelapse-maker"

[Setup]
; Never change this GUID. It is what makes the next release upgrade this one
; in place rather than sitting beside it in Add or remove programs.
AppId={{9A1F06AB-FF08-40FE-92A0-B8FC2A530724}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Unattended daily timelapses from IP cameras

DefaultDirName={autopf}\timelapse-maker
DefaultGroupName={#AppName}
; Neither page offers a real choice. install.ps1 owns where the scripts go and
; creates the Start menu entry itself, so letting the operator move {app} would
; move the payload away from the program without moving the program.
DisableDirPage=yes
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=timelapse-maker-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; Everything this installs is machine-wide: a service, two scheduled tasks, the
; system PATH and a directory under Program Files. There is no useful per-user
; mode to fall back to, so ask for the elevation up front rather than failing
; at the first write.
PrivilegesRequired=admin
; x64compatible rather than x64: it covers ARM64 Windows running the x64
; payload under emulation, and both pinned downloads are amd64 builds.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The service hosting uses the SCM through ctypes and the port targets Windows
; 10 and Server 2016 upwards. Refusing here beats a service that will not start.
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
; The same EXPERIMENTAL warning install.sh and install.ps1 both open with. An
; operator who reads only one screen of this should still read that one.
WelcomeLabel2=This installs timelapse-maker {#AppVersion}, which pulls full-resolution snapshots from your IP cameras and encodes one video per camera per day.%n%nEXPERIMENTAL: early software, tested on a small number of machines. The configuration format may change between versions.%n%nWindows build: capture and encode. There is no web interface on this platform.

[Tasks]
; Permission rather than instruction, and the wording says so. Both stages look
; first and download only when the machine has nothing: an ffmpeg the operator
; chose deliberately is never replaced, which is the whole of item 11c.6a.
Name: "python"; Description: "Install Python if this machine has none"; GroupDescription: "Prerequisites (only used if missing):"
Name: "ffmpeg"; Description: "Install ffmpeg if this machine has none"; GroupDescription: "Prerequisites (only used if missing):"

[Files]
; scripts\*.py is a glob rather than a list, deliberately, and it is safe here
; for a reason worth stating: this only lays down the source tree. What gets
; INSTALLED is install.ps1's own $SCRIPTS list, which a test pins against the
; files on disk, so a stray file in scripts\ still reaches nothing.
Source: "..\install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\setup-gui.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\timelapse-setup.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\*.py"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\config\config.example.json"; DestDir: "{app}\config"; Flags: ignoreversion
Source: "prepare.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "prerequisites.json"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\install.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\architecture.md"; DestDir: "{app}\docs"; Flags: ignoreversion

; config\config.json is NOT here, and must never be: it is the real
; configuration, it holds every camera password, and it is gitignored precisely
; so that a build machine cannot pick one up and ship it.

; No [Icons] section, and its absence is a decision rather than an oversight.
; install.ps1 already creates the Start menu entry, on both front doors, so an
; [Icons] line here would write the same shortcut a second time and hand its
; removal to a second uninstaller. The rule that install.ps1 owns installing is
; not only about services.

[Run]
; postinstall, so it opens when the operator clicks Finish rather than in the
; middle of the install. PowerShell hidden and not the .cmd: a shortcut whose
; target is a batch file opens a console window and leaves it there, which is
; the single thing a graphical wizard exists to spare somebody.
;
; {sys} is System32 rather than SysWOW64 only because this installs in 64-bit
; mode; a 32-bit PowerShell would see a different Program Files and a different
; registry, and would put the service's interpreter path in the wrong one.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{code:ScriptPrefix}\setup-gui.ps1"""; Description: "Set up my cameras now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Deregisters the service and both tasks and removes the scripts. It leaves the
; configuration, the frames and the videos alone, which is the same promise
; install.sh makes: deleting a fortnight of recordings because somebody wanted
; to reinstall is not a thing this does quietly.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -Uninstall"; RunOnceId: "RemoveTimelapse"; Flags: runhidden waituntilterminated

[UninstallDelete]
; A downloaded ffmpeg is unpacked here after the file list was recorded, so
; nothing would otherwise remove it. It goes because we put it there; an ffmpeg
; the operator installed themselves is somewhere else entirely and is untouched.
Type: filesandordirs; Name: "{app}\ffmpeg"

[Code]
var
  StageFailed: Boolean;

function ScriptPrefix(Param: String): String;
begin
  { Where install.ps1 puts the scripts, which is its default and not ours to
    choose. Spelled once here so the shortcut and the finish-page action cannot
    disagree with each other. }
  Result := ExpandConstant('{commonpf}\timelapse');
end;

function LogPath(): String;
begin
  Result := ExpandConstant('{commonappdata}\timelapse\install.log');
end;

function RunPrepare(): Boolean;
var
  Params: String;
  ResultCode: Integer;
begin
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' +
            ExpandConstant('{app}\installer\prepare.ps1') + '"';
  if WizardIsTaskSelected('python') then
    Params := Params + ' -AllowPython';
  if WizardIsTaskSelected('ffmpeg') then
    Params := Params + ' -AllowFfmpeg';

  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
                 Params, ExpandConstant('{app}'), SW_HIDE,
                 ewWaitUntilTerminated, ResultCode);
  if Result then
    Result := (ResultCode = 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Answer: Integer;
  Dummy: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  { One status message covering the whole stage. It can genuinely take several
    minutes on a machine that needs both downloads, and a progress bar that
    looks stuck with no explanation is how an operator comes to kill an
    installer halfway through registering a service. }
  WizardForm.StatusLabel.Caption :=
    'Installing timelapse-maker. Any prerequisites are downloaded now, which' +
    ' can take a few minutes.';
  WizardForm.StatusLabel.Update();

  StageFailed := not RunPrepare();

  if StageFailed then
  begin
    Answer := MsgBox('Setup could not finish installing timelapse-maker.' #13#10#13#10
      'The log says what stopped it:' #13#10 + LogPath() + #13#10#13#10
      'Open it now?', mbError, MB_YESNO);
    if Answer = IDYES then
      ShellExec('open', LogPath(), '', '', SW_SHOW, ewNoWait, Dummy);
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  { The finish page says "Setup has finished installing" whatever happened, so
    a failed stage would be reported as a success by the last thing the
    operator reads. Same reasoning as the nightly encode not calling an idle
    run a fault: the page has to say which of the two it was. }
  if (CurPageID = wpFinished) and StageFailed then
  begin
    WizardForm.FinishedHeadingLabel.Caption := 'Setup did not finish';
    WizardForm.FinishedLabel.Caption :=
      'The files were copied, but installing timelapse-maker did not complete.' #13#10#13#10 +
      'See ' + LogPath() + ' for the reason. Fixing it and running this' +
      ' installer again is safe: it reconfigures what is already there rather' +
      ' than refusing.';
    WizardForm.RunList.Visible := False;
  end;
end;
