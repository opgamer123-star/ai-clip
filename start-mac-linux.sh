#!/bin/sh
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Python3 install karo"; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg install karo (Mac: brew install ffmpeg, Linux: sudo apt install ffmpeg)"; exit 1; }
[ -f .env ] || { printf "Groq API key paste karo: "; read KEY; printf "GROQ_API_KEY=%s\nJOBS_DIR=jobs\nKEEP_HOURS=336\n" "$KEY" > .env; }
python3 -m pip install -r requirements-local.txt
(sleep 3; (open http://localhost:7860 || xdg-open http://localhost:7860) >/dev/null 2>&1) &
python3 app.py
