@echo off
rem timelapse-maker: double-click this to open the setup wizard.
rem
rem Two lines of batch calling PowerShell, for the reason timelapse.cmd is two
rem lines calling Python: a batch file has no way to say anything useful, and
rem anything it did say would have to be maintained in a second place.
rem
rem -ExecutionPolicy Bypass applies to this one invocation only and changes
rem nothing about the machine. Without it the default policy refuses to run a
rem downloaded script, which presents as the window opening and closing again
rem with nothing said.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-gui.ps1" %*
