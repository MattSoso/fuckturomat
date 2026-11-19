@echo off
setlocal
cd /d "%~dp0"
title CreativeCrafts Faktury
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 goto try_python
py -3 launcher.py
goto finished
:try_python
where python >nul 2>nul
if errorlevel 1 goto missing
python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 goto missing
python launcher.py
goto finished
:missing
echo.
echo Nie znaleziono Python 3.9 lub nowszego.
echo Zainstaluj Python z https://www.python.org/downloads/windows/
echo Zaznacz Add python.exe to PATH podczas instalacji.
echo Potem ponownie kliknij URUCHOM.bat.
echo.
pause
exit /b 1
:finished
set "cc_exit=%errorlevel%"
echo.
if not "%cc_exit%"=="0" echo Program zakonczyl sie z bledem. Zachowaj komunikat widoczny powyzej.
pause
exit /b %cc_exit%
