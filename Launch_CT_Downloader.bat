@echo off
setlocal
cd /d "%~dp0"
where pyw >nul 2>nul
if not errorlevel 1 (start "" pyw -3 "%~dp0CT_Downloader.py" & exit /b)
where pythonw >nul 2>nul
if not errorlevel 1 (start "" pythonw "%~dp0CT_Downloader.py" & exit /b)
echo Python 3 was not found.
echo This app uses Python's built-in Tkinter GUI; no pip packages are required.
echo.
where winget >nul 2>nul
if errorlevel 1 goto MANUAL
choice /C YN /M "Install Python 3 using Windows Package Manager (winget)"
if errorlevel 2 goto END
winget install --id Python.Python.3.13 --exact --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto MANUAL
echo.
echo Installation requested. Close and reopen this launcher once Python is available.
pause
exit /b
:MANUAL
echo Install Python 3 for Windows from https://www.python.org/downloads/windows/
echo Enable 'Add Python to PATH' if offered, then reopen this launcher.
pause
:END
endlocal
