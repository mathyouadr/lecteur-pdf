@echo off
cd /d "%~dp0"
set LECTEUR_HTTPS=1
start "" http://127.0.0.1:8765
python serveur.py
