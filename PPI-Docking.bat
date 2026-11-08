@echo off
REM Abre o Painel PPI (localhost:8777). O Streamlit antigo foi aposentado; este
REM atalho continua existindo para nao quebrar quem ja o tinha na area de
REM trabalho -- ele agora chama o lancador do painel.
powershell -NoLogo -ExecutionPolicy Bypass -File "%~dp0iniciar_painel.ps1"
