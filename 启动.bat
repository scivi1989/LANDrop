@echo off
chcp 65001 >nul
title LAN Share - LAN File Transfer
python "%~dp0server.py" %*
pause
