@echo off
REM AI AudioFlow（純 Windows 版）啟動腳本。
REM 首次執行會自動 pip install -e .（需連網）；之後直接啟動 GUI。

cd /d "%~dp0"
python -m src.main %*
