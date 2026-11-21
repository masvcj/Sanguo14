@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "TRAINER_PYTHON=C:\Users\User\AppData\Local\Programs\Python\Python314\python.exe"
set "PYTHONPATH=%~dp0.build-tools;%PYTHONPATH%"
set "TRAINER_EXE=%~dp0dist\SAN14PK_Heritage_Plus43_Trainer.exe"
if not exist "%TRAINER_PYTHON%" (
    echo 找不到 Python：%TRAINER_PYTHON%
    goto failed
)
"%TRAINER_PYTHON%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo 正在从 PyPI 安装打包工具到项目目录，需要网络连接...
    "%TRAINER_PYTHON%" -m pip install --target "%~dp0.build-tools" pyinstaller
    if errorlevel 1 goto failed
)
echo 正在打包 EXE 和默认配置（含个性与战法库）...
"%TRAINER_PYTHON%" -m PyInstaller --noconfirm --onefile --windowed --noupx --add-data "profiles\san14pk_heritage.json;profiles" --add-data "profiles\san14ce_catalog.json;profiles" --name "SAN14PK_Heritage_Plus43_Trainer" main.py
if errorlevel 1 goto failed
if not exist "%TRAINER_EXE%" goto failed
echo 正在使用 Windows Defender 扫描生成的 EXE...
if not exist "%ProgramFiles%\Windows Defender\MpCmdRun.exe" (
    echo 未找到 Defender 扫描工具，未执行扫描或启动测试。
    goto failed
)
"%ProgramFiles%\Windows Defender\MpCmdRun.exe" -Scan -ScanType 3 -File "%TRAINER_EXE%"
if errorlevel 1 (
    echo 扫描发现问题或未完成，请查看上方结果。未启动 EXE。
    goto failed
)
if not exist "%TRAINER_EXE%" (
    echo 扫描后文件不存在，未启动 EXE。请查看 Windows 安全中心的保护历史记录。
    goto failed
)
echo 打包和扫描完成，即将打开修改器，请检查界面能否正常使用。
echo EXE：%TRAINER_EXE%
start "" "%TRAINER_EXE%"
pause
exit /b 0
:failed
echo 操作未完成，请保留上面的错误信息。
pause
exit /b 1
