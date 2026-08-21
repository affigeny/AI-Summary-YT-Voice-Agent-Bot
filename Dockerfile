# Dockerfile для деплоя Telegram-бота на Render (Web Service, Free)
# FFmpeg нужен pydub для декодирования аудио/видео перед распознаванием речи.

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости (кэшируются отдельным слоем Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом весь код (список файлов больше не нужно править при каждом новом модуле)
COPY *.py ./

# Запуск бота (polling + фиктивный health-check сервер на $PORT)
CMD ["python", "bot.py"]
