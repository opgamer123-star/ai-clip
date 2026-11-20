FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG WITH_YTDLP=0
RUN if [ "$WITH_YTDLP" = "1" ]; then pip install --no-cache-dir yt-dlp; fi
COPY . .
EXPOSE 7860
ENV PORT=7860
CMD ["python", "app.py"]
