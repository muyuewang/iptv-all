@echo off
REM ==========================================================================
REM  Refresh Windows icon cache
REM  Use when the taskbar / desktop still shows an old icon after replacing exe.
REM  Tip: right-click -> Run as administrator works best.
REM ==========================================================================
setlocal
echo === Refreshing Windows icon cache ===

echo [1/5] Stopping Explorer...
taskkill /f /im explorer.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/5] Deleting icon cache databases...
del /a /f /q "%LOCALAPPDATA%\IconCache.db" >nul 2>&1
del /a /f /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache*.db" >nul 2>&1
del /a /f /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\thumbcache*.db" >nul 2>&1

echo [3/5] Refreshing shell icon associations...
ie4uinit.exe -show >nul 2>&1

echo [4/5] Restarting Explorer...
start explorer.exe

echo [5/5] Done. If the taskbar icon is still old, sign out and sign in once.
echo.
pause
