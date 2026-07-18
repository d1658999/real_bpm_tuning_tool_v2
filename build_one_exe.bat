@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "APP_NAME=BPMTuningTool"
set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "RUST_EXE=%CD%\rust_optimizer\target\release\bpm-ranking-optimizer.exe"
set "QT_API=PyQt5"

echo [1/5] Preparing the Python build environment...
if not exist "%VENV_PYTHON%" (
    where py >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 goto :failed
)

"%VENV_PYTHON%" -m pip install -e . pyinstaller
if errorlevel 1 goto :failed

echo [2/5] Building the release Rust optimizer...
where cargo >nul 2>nul
if errorlevel 1 (
    echo ERROR: Cargo was not found. Install Rust from https://rustup.rs/ and retry.
    goto :failed
)
cargo build --release --manifest-path rust_optimizer\Cargo.toml
if errorlevel 1 goto :failed
if not exist "%RUST_EXE%" (
    echo ERROR: The Rust optimizer was not created at "%RUST_EXE%".
    goto :failed
)

echo [3/5] Building the single Windows executable...
"%VENV_PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "%APP_NAME%" ^
    --distpath "%CD%\dist" ^
    --workpath "%CD%\build\pyinstaller" ^
    --specpath "%CD%\build" ^
    --paths "%CD%" ^
    --collect-submodules bpm_tuner ^
    --collect-all skrf ^
    --copy-metadata scikit-rf ^
    --add-binary "%RUST_EXE%;rust_optimizer\target\release" ^
    bpm_tuner_app.py
if errorlevel 1 goto :failed

echo [4/5] Copying external BOM folders beside the executable...
if not exist "Capacitors_BOM" (
    echo ERROR: Capacitors_BOM was not found.
    goto :failed
)
if not exist "Inductors_BOM" (
    echo ERROR: Inductors_BOM was not found.
    goto :failed
)
robocopy "Capacitors_BOM" "dist\Capacitors_BOM" *.s2p /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :failed
robocopy "Inductors_BOM" "dist\Inductors_BOM" *.s2p /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :failed

echo [5/5] Build complete.
echo.
echo Output: "%CD%\dist\%APP_NAME%.exe"
echo Keep Capacitors_BOM and Inductors_BOM beside the EXE.
exit /b 0

:failed
echo.
echo BUILD FAILED. Review the error above.
exit /b 1
