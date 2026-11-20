#!/data/data/com.termux/files/usr/bin/sh
cd "$(dirname "$0")"
termux-wake-lock 2>/dev/null
echo "Chrome mein kholo: http://localhost:7860  (Termux band mat karna)"
HOST=127.0.0.1 python app.py
