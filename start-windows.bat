@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Pehle Python install karo: https://www.python.org/downloads/ ^(Add to PATH tick karna^) & pause & exit /b)
where ffmpeg >nul 2>nul || (echo ffmpeg install ho raha hai... & winget install -e --id Gyan.FFmpeg & echo Install ke baad ye window band karke start-windows.bat dobara chalao & pause & exit /b)
where deno >nul 2>nul || winget install -e --id DenoLand.Deno
if exist .env goto haveenv
set /p KEY=Groq API key paste karo: 
(echo GROQ_API_KEY=%KEY%& echo JOBS_DIR=jobs& echo KEEP_HOURS=336)> .env
:haveenv
python -m pip install -r requirements-local.txt
start http://localhost:7860
python app.py
pause
