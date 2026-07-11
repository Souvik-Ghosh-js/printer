@echo off
REM ============================================================
REM  Build MohiniPrintWorker.exe from worker_app.py
REM  Run this on the shop PC (Windows) inside the printer folder.
REM ============================================================

echo Installing build dependencies...
pip install pyinstaller requests pywin32

echo Building the .exe...
REM --onefile   : single .exe
REM --windowed  : no console window (GUI app)
REM --name      : output name
pyinstaller --onefile --windowed --name "MohiniPrintWorker" ^
    --hidden-import win32print --hidden-import win32api ^
    worker_app.py

echo.
echo ============================================================
echo  Done. Your app is here:
echo     dist\MohiniPrintWorker.exe
echo.
echo  Double-click it to run. It opens a window and starts
echo  watching for print jobs automatically.
echo ============================================================
pause
