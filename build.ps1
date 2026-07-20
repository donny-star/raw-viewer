param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
& $Python -m pip install -r requirements-dev.txt
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name RAWViewer --paths src src/raw_viewer/app.py
Write-Host "Created dist/RAWViewer.exe"
