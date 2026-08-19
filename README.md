# YT_Bot_Sum

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://t.me/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/tag/affigeny/YT_Bot_Sum)](https://github.com/affigeny/YT_Bot_Sum/tags)
[![Render](https://img.shields.io/badge/Deploy-Render-EBFF71?logo=render)](https://render.com)

Telegram-бот для извлечения и суммаризации контента из YouTube-видео, голосовых сообщений и аудиофайлов. Распознаёт речь локально через Whisper, перерабатывает текст через LLM (OpenRouter) по выбранным шаблонам.

## 🚀 Возможности

| Возможность | Описание |
|-------------|----------|
| 📹 **YouTube / Shorts** | Извлечение субтитров (включая автогенерацию) через `yt-dlp` с browser-like headers. Fallback: скачивание аудио → локальное распознавание через Whisper. |
| 🎙 **Голосовые / аудио** | Поддержка голосовых сообщений Telegram и аудиофайлов (mp3, wav, m4a, ogg). Распознавание через `faster-whisper` (модель `small` по умолчанию). |
| 🤖 **LLM-переработка** | Интеграция с OpenRouter (OpenAI-совместимый API). Дефолтная модель: `nvidia/nemotron-3-super-120b-a12b:free`. |
| 🗄 **SQLite-база** | Хранение пользовательских шаблонов, кэша транскриптов, истории диалогов. |
| 🛠 **Кастомные шаблоны** | Команда `/add_template ID | Название | Промпт` для сохранения собственных сценариев обработки. |
| 💬 **Интерактивный чат** | Диалог с ИИ по контексту видео/аудио: уточняющие вопросы, анализ, пересказ. |
| ⬇️ **Скачивание медиа** | Инлайн-кнопки для выбора качества видео и аудио (ограничение Telegram: ≤50 MB). |
| 🩺 **Health-check** | Встроенный HTTP-сервер на `$PORT` (эндпоинт `/health` → `200 OK`) для обхода засыпания на Render Free. |

## 📦 Быстрый старт

### Локальный запуск

```bash
# Клонирование репозитория
git clone https://github.com/affigeny/YT_Bot_Sum.git
cd YT_Bot_Sum

# Виртуальное окружение
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# Установка зависимостей
pip install -r requirements.txt

# Установка FFmpeg (обязательно для pydub)
# macOS:
brew install ffmpeg
# Linux (Debian/Ubuntu):
sudo apt-get update && sudo apt-get install -y ffmpeg

# Переменные окружения
export TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
export AI_API_KEY="sk-or-v1-..."        # OpenRouter API key
export AI_API_URL="https://openrouter.ai/api/v1"
export AI_MODEL="nvidia/nemotron-3-super-120b-a12b:free"
export DB_PATH="./bot_database.db"

# Запуск
python bot.py
```

### Деплой на Render (бесплатный тариф)

Репозиторий настроен как **Web Service** с health-check (обход limit на Free tier).

1. **Форкните** репозиторий или используйте свой.
2. В панели Render: **New → Web Service** → подключите репозиторий, ветка `main`.
3. **Environment Variables**:

   | Ключ | Значение | Обязательно |
   |------|----------|-------------|
   | `TELEGRAM_BOT_TOKEN` | Токен от [@BotFather](https://t.me/BotFather) | ✅ |
   | `AI_API_KEY` | Ключ OpenRouter (`sk-or-v1-...`) | ✅ |
   | `AI_API_URL` | `https://openrouter.ai/api/v1` | ⚠️ по умолчанию |
   | `AI_MODEL` | `nvidia/nemotron-3-super-120b-a12b:free` | ⚠️ по умолчанию |
   | `DB_PATH` | `/var/data/bot_database.db` | ⚠️ по умолчанию |
   | `YT_COOKIES_FILE` | Путь к cookies.txt (см. ниже) | ❌ опционально |
   | `YT_COOKIES_FROM_BROWSER` | `chrome` / `firefox` / `safari` | ❌ опционально |

4. **Secret Files** (опционально, для обхода bot-check): загрузите `cookies.txt` в `/var/data/cookies.txt`.
5. Нажмите **Deploy** и проверьте логи.
6. После деплоя: `https://<service>.onrender.com/health` → `{"status":"ok"}`.

> **⚠️ Засыпание на Free tier:** Render усыпляет сервис после ~15 минут без трафика. Для постоянного онлайна настройте внешний мониторинг (UptimeRobot, Cron-job.org) на `/health` с интервалом 10–13 минут.

## ⚙️ YouTube bot-check

YouTube блокирует выкачку субтитров без авторизации: *«Sign in to confirm you're not a bot»*.

Бот детектирует блокировку и:
1. Пробует аудио-fallback (скачивание + Whisper).
2. Если и оно заблокировано — сообщает пользователю о необходимости куки.

**Обход блокировки:**

```bash
# Локально (из браузера)
yt-dlp --cookies-from-browser chrome -o cookies.txt "https://www.youtube.com/watch?v=VIDEO_ID"

# Передача в бота
export YT_COOKIES_FILE="/path/to/cookies.txt"
# Или (только локальный запуск, требует установленного браузера)
export YT_COOKIES_FROM_BROWSER=chrome
```

**На Render:** загрузите `cookies.txt` через **Secret Files** → `/var/data/cookies.txt`, задайте `YT_COOKIES_FILE=/var/data/cookies.txt`.

## 📁 Структура проекта

```
.
├── bot.py              # Основной код (модульные классы, PEP8)
├── Dockerfile          # Образ с FFmpeg + Python 3.11
├── render.yaml         # Blueprint-конфиг для Render
├── requirements.txt    # Зависимости Python
├── DEPLOY.md           # Развёрнутая инструкция по деплою
├── README.md           # Этот файл
├── .gitignore          # Исключения git
├── .dockerignore       # Исключения Docker
└── LICENSE             # MIT License
```

### Архитектура (`bot.py`)

| Класс | Назначение |
|-------|------------|
| `BotDatabase` | SQLite: шаблоны, кэш YouTube, история диалогов |
| `LLMClient` | Запросы к OpenRouter (OpenAI-совместимый API) |
| `YTClient` | `yt-dlp`: субтитры, метаданные, форматы скачивания |
| `STTClient` | `faster-whisper`: локальное распознавание речи |
| `MediaBot` | Обработчики Telegram: текст, голос, аудио, инлайн-кнопки |

## 🏷 История версий

| Версия | Дата | Изменения |
|--------|------|-----------|
| **v4.1.4** | 2026-08-19 | Таймауты на всех асинхронных операциях; исправлен `async` вызов `_transcribe_youtube_audio`; блокировка теперь возвращает ответ за ~3 сек вместо зависания |
| **v4.1.3** | 2026-08-19 | Исправлен `render.yaml` (указывал на неверный репозиторий); добавлена поддержка куки (`YT_COOKIES_FILE`, `YT_COOKIES_FROM_BROWSER`); обновлён `DEPLOY.md` |
| **v4.1.2** | 2026-08-19 | Улучшен regex для YouTube-ссылок; обработка `YouTubeBlockingError`; логирование |
| **v4.1.1** | 2026-08-18 | regex для YouTube-ссылок, улучшена структура кода |
| **v4.1.0** | 2026-08-18 | `yt-dlp` вместо `youtube-transcript-api`; browser-like headers; устойчивость к блокировкам |
| **v4.0.0** | 2026-08-18 | Faster-whisper (локальный STT); инлайн-кнопки; модульная архитектура |
| **v3.0.0** | — | PEP8-рефакторинг, health-check сервер, фикс `/var/data/` |
| **v2.1** | — | SQLite-база: шаблоны, кэш, история |
| **v2.0** | — | Переход на `python-telegram-bot` v20 (async) |
| **v1.x** | — | Синхронный прототип |

## 🗺 Roadmap

| Версия | Планируется |
|--------|-------------|
| **v5.0.0** | Стриминг ответов LLM (progressive display) |
| **v6.0.0** | LRU-кеш ответов LLM (снижение затрат) |
| **v7.0.0** | Мультиязычность интерфейса (i18n) |
| **v8.0.0** | OCR для изображений/PDF → суммаризация |
| **v9.0.0** | Персонализированные модели (fine-tune Whisper/LLM на данных пользователя) |
| **v10.0.0** | Социальный слой: публичные каналы саммари, рейтинги |
| **v11.0.0** | Интеграция с календарями (Google Calendar / Outlook) |
| **v12.0.0** | Edge-деплой: Raspberry Pi / Jetson Nano (полностью офлайн) |

## 📜 Лицензия

[MIT License](LICENSE) — свободное использование, модификация, распределение.

---

**Автор:** [affigeny](https://github.com/affigeny)  
**Создано для:** EdTech-проекта и демонстрации AI/Python навыков  
**Проблемы / вопросы:** [GitHub Issues](https://github.com/affigeny/YT_Bot_Sum/issues)
