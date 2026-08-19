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
COPY youtube_bypass.py .
COPY transcription.py .
COPY checkpoint_manager.py .

# Запуск бота (polling + фиктивный health-check сервер на $PORT)
CMD ["python", "bot.py"]

## 📱 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Начало работы |
| `/transcribe` | Транскрибация YouTube/аудио |
| `/stats` | Статистика использования |
| `/bypass` | Выбор метода обхода YouTube |
| `/add_template` | Создание своего шаблона |

## 🔧 YouTube Bypass Methods

| Метод | Описание | Когда использовать |
|-------|----------|-------------------|
| 🚫 no_cookies | Стандартный | Когда YouTube не блокирует |
| 📁 cookie_file | Файл куки | При блокировке "Sign in" |
| 🌐 browser_chrome | Куки из Chrome | Для локального запуска |
| 🦊 browser_firefox | Куки из Firefox | Для локального запуска |
| 🧪 test_all | Тест всех | Для диагностики |

## 📊 Версии

| Версия | Дата | Изменения |
|--------|------|-----------|
| **4.3.3** | 2026-08-19 | Добавлены логи отладки |
| 4.3.2 | 2026-08-19 | Исправлен Dockerfile |
| 4.3.1 | 2026-08-19 | Улучшена обработка ошибок |
| 4.3.0 | 2026-08-19 | Добавлен youtube_bypass |
| 4.1.4 | 2026-08-19 | Базовая версия |

## 📝 License

MIT License — см. [LICENSE](LICENSE)

## 🤝 Contributing

1. Форкните репозиторий
2. Создайте ветку (`git checkout -b feature/AmazingFeature`)
3. Закоммитьте изменения
4. Push в ветку
5. Откройте Pull Request

## 📞 Контакты

- **GitHub:** [@affigeny](https://github.com/affigeny)
- **Telegram:** [@Evandrus](https://t.me/Evandrus)

---

**Сделано с ❤️ для продуктивности**
