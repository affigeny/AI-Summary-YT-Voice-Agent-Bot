# Dockerfile для деплоя Telegram-бота на Render (Web Service, Free)
# Ставит FFmpeg — обязателен для pydub (декодирование аудио/голосовых).

FROM python:3.11-slim

# FFmpeg — критично: без него pydub не сможет конвертировать аудио
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости (кэшируются отдельным слоем Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом код
COPY bot.py .

# Запуск бота (polling + фиктивный health-check сервер на $PORT)
CMD ["python", "bot.py"]
