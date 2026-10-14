@echo off
rem Build _site/ and serve it on http://127.0.0.1:8000/ (only needs Python).
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0build_site.py" --serve
) else (
  python "%~dp0build_site.py" --serve
)
