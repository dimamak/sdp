@echo off
REM Run the daily push using Git Bash explicitly.
REM Windows ships its own bash.exe that launches WSL, so a plain `bash push_daily.sh`
REM fails with "execvpe(/bin/bash)" on machines without a WSL distro installed.
REM Usage:  push.cmd            (push now)
REM         push.cmd --dry-run  (show what would be sent)
setlocal
set "BASH="
if exist "%ProgramFiles%\Git\bin\bash.exe" set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined BASH if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "BASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not defined BASH if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "BASH=%LocalAppData%\Programs\Git\bin\bash.exe"
if not defined BASH (
  echo Git Bash not found. Install Git for Windows:  winget install Git.Git
  exit /b 1
)

set "DIR=%~dp0"
set "DIR=%DIR:\=/%"
REM last command's exit code propagates on its own; an explicit `exit /b
REM %ERRORLEVEL%` here reports a stale value because it expands at parse time
"%BASH%" -lc "'%DIR%push_daily.sh' %*"
