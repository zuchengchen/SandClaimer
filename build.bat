@echo off
setlocal
cd /d "%~dp0"

echo [0/5] Reading version (single source: sand_patch.TOOL_VERSION) ...
for /f "delims=" %%v in ('python -c "import sand_patch;print(sand_patch.TOOL_VERSION)"') do set "VER=%%v"
if "%VER%"=="" ( echo ERROR: cannot read TOOL_VERSION & exit /b 1 )
echo     VER=%VER%
set "EXENAME=SandClaimer-%VER%.exe"

echo [1/5] Installing dependencies ...
python -m pip install -r requirements.txt || exit /b 1

echo [2/5] Patching Nuitka pywebview plugin (add win32 submodules) ...
python patch_plugin.py || exit /b 1

echo [3/5] Generating icon.ico ...
python make_icon.py || exit /b 1

echo [4/5] Nuitka compile (onefile, machine code) ...
python -m nuitka --standalone --onefile --assume-yes-for-downloads --mingw64 --experimental=force-dependencies-pefile --windows-console-mode=disable --windows-icon-from-ico=icon.ico --company-name="SandClaimer" --product-name="Sand Claimer" --product-version=%VER% --file-version=%VER%.0 --include-data-dir=web=web --output-filename=%EXENAME% --output-dir=nuitka-out app.py || exit /b 1

echo [5/5] Inno Setup packaging ...
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVer=%VER% /DExeName=%EXENAME% installer.iss || exit /b 1

echo.
echo Done:
echo   EXE:       nuitka-out\%EXENAME%
echo   Installer: installer\SandClaimer-Setup-%VER%.exe
