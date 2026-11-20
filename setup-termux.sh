#!/data/data/com.termux/files/usr/bin/sh
cd "$(dirname "$0")"
pkg update -y
pkg install -y python ffmpeg nodejs
pip install -r requirements-phone.txt
[ -f .env ] || { printf "Groq API key paste karo: "; read KEY; printf "GROQ_API_KEY=%s\nJOBS_DIR=jobs\nKEEP_HOURS=168\n" "$KEY" > .env; }
echo "Setup ho gaya. Ab ./start-termux.sh chalao"
