# Clipper

Link ya video se best moments, captions ke saath vertical clips.

## Phone pe (Android, Termux) - bina laptop ke
1. F-Droid se **Termux** install karo (Play Store wala purana hai).
2. `auto-clipper.zip` ko phone ki Downloads mein save karo.
3. Termux kholo aur ye commands ek ek karke chalao:
```
termux-setup-storage
pkg install -y unzip
cp ~/storage/downloads/auto-clipper.zip ~/
unzip auto-clipper.zip -d clipper
cd clipper
sh setup-termux.sh
sh start-termux.sh
```
4. `setup-termux.sh` Groq key poochega (console.groq.com se free). Paste karo.
5. Chrome mein http://localhost:7860 kholo, menu se "Add to Home screen".
6. Termux band mat karna (notification mein "Acquire wakelock" dabao).
Dobara use: Termux kholo, `cd clipper`, `sh start-termux.sh`.

## Windows laptop
`start-windows.bat` pe double click. Phone se same wifi pe `http://PC-IP:7860`.

## Cloud (SnapDeploy etc.)
Dockerfile se deploy karo. Sirf file upload chalta hai (link mode band). Link try karne ke liye build arg `WITH_YTDLP=1`.

## Keys (.env file mein)
GROQ_API_KEY (zaruri), GEMINI_API_KEY (optional: gameplay dekhna, facecam), ACCESS_CODE (optional password), JOBS_DIR, KEEP_HOURS.
