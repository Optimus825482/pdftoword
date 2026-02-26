$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

$env:FLASK_DEBUG = "false"
python -m waitress --host=0.0.0.0 --port=5000 PDFTODOCX:app
