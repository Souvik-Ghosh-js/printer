@echo off
REM ============================================================
REM  Build MohiniPrintWorker.exe from worker_app.py
REM  Run this on a Windows PC inside the printer folder.
REM ============================================================
setlocal

echo Installing build dependencies...
python -m pip install pyinstaller requests pywin32
if errorlevel 1 goto :fail

echo.
echo Building the .exe...
REM Call PyInstaller as a module so it works even when the
REM 'pyinstaller' command isn't on PATH (common with Store Python).
python -m PyInstaller --onefile --windowed --name "MohiniPrintWorker" ^
    --hidden-import win32print --hidden-import win32api --hidden-import win32timezone ^
    worker_app.py
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo  SUCCESS. Your app is here:
echo     %CD%\dist\MohiniPrintWorker.exe
echo.
echo  Double-click it to run. It opens a window and starts
echo  watching for print jobs automatically.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo ############################################################
echo  BUILD FAILED. See the error above.
echo  Common fix: run this instead, directly in the terminal:
echo     python -m PyInstaller --onefile --windowed --name "MohiniPrintWorker" worker_app.py
echo ############################################################
pause
exit /b 1
