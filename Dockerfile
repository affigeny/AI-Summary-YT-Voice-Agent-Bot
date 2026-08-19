# Dockerfile для деплоя Telegram-бота на Render (Background Worker)
# Ставит FFmpeg, который обязателен для pydub (декодирование аудио/голосовых)

FROM python:3.11-slim

# FFmpeg — критично: без него pydub не сможет конвертировать аудио
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости (кэшируется слоем)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом код
COPY advanced_voice_yt_bot_v2.1_db.py .

# Command задаётся в Render (Background Worker), здесь дефолт
CMD ["python", "advanced_voice_yt_bot_v2.1_db.py"]
