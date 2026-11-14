@echo off
rem Launch Converter without a console window. Files/folders given as arguments are added to the queue.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py" %*
