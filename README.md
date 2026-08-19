<div align="center">

# 🤖 AI Summary YT Voice Agent Bot

**Принимает** YouTube-ссылки · голосовые · аудиофайлы<br>
**Извлекает** транскрипт → **перерабатывает** через LLM (OpenRouter)<br>
по выбранному шаблону: саммари, Пирамида Минто, экшен-план, конспект

<img src="https://img.shields.io/badge/version-3.0.0-blue?style=for-the-badge" alt="version">
<img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="license">
<img src="https://img.shields.io/badge/python-3.11-ffd43b?style=for-the-badge" alt="python">

</div>

---

## ✨ Возможности

| Возможность | Описание |
|-----------|----------|
| 📹 **YouTube/Shorts** | Умный парсинг субтитров (RU → EN → автогенерация), кэш в SQLite |
| 🎙 **Голосовые / аудио** | Распознавание речи (Google Speech) через FFmpeg + pydub |
| 🤖 **LLM-переработка** | OpenRouter (OpenAI-совместимый API), любой кастомный промпт |
| 🗄 **SQLite-база** | Шаблоны, кэш и история диалогов сохраняются навсегда |
| 🛠 **Кастомные шаблоны** | `/add_template ID \| Название \| Промпт` |
| 💬 **Чат с ИИ** | Свободное общение с моделью по полученному контексту |
| 🩺 **Health-check** | Фиктивный HTTP-сервер на `/health` для Free Web Service |

---

## 🚀 Быстрый старт

### Локально

```bash
git clone https://github.com/affigeny/AI-Summary-YT-Voice-Agent-Bot.git
cd AI-Summary-YT-Voice-Agent-Bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# macOS: brew install ffmpeg   # Linux: sudo apt install ffmpeg

export TELEGRAM_BOT_TOKEN="токен_бота"
export AI_API_KEY="ключ_openrouter"
export AI_API_URL="https://openrouter.ai/api/v1"
export AI_MODEL="openai/gpt-4o-mini"

python bot.py
```

### На Render (Web Service, Free)

Проект задеплоен как **Web Service** с фиктивным HTTP-сервером на `$PORT` и путём `/health` — это позволяет держать бота на бесплатном тарифе. Сам бот работает через `run_polling()`.

Подробности и список env-переменных — в [`DEPLOY.md`](DEPLOY.md).

> ⚠️ **Про засыпание:** Free Web Service усыпает после ~15 минут без HTTP-трафика. Чтобы бот был в сети 24/7, настрой бесплатный **UptimeRobot**-монитор на `https://ai-summary-yt-voice-agent-bot-ir8e.onrender.com/health` с интервалом 10–13 минут.

---

## 📁 Структура проекта

```
.
├── bot.py              # 🌟 основной код (PEP8, модульная архитектура)
├── Dockerfile          # образ с FFmpeg
├── render.yaml         # Blueprint-конфиг для Render
├── requirements.txt   # зависимости Python
├── DEPLOY.md          # подробная инструкция по деплою
├── README.md          # этот файл
├── .gitignore         # что не коммитим
├── .dockerignore      # что не попадает в образ
├── LICENSE            # MIT
└── legacy/            # 📦 старые версии (история, не удаляются)
    ├── advanced_voice_yt_bot.py          # v1.x
    ├── advanced_voice_yt_bot_v2.py       # v2.x
    └── advanced_voice_yt_bot_v2.1_db.py  # v2.1 с БД
```

> В `bot.py` код разбит на модульные классы: `BotDatabase` (работа с БД), `LLMClient` (обращение к LLM), `AdvancedMediaYTAgentBot` (бизнес-логика/обработчики). Старые монолитные версии переехали в `legacy/`.

---

## 🏷 Управление версиями

- Текущая версия: **v3.0.0**
- Версия задаётся в `bot.py` (`__version__` и `VERSION_STRING`)
- Релизы помечаются **git-тегами**: `git tag v3.0.0`

История версий:

| Версия | Изменения |
|--------|-----------|
| **v3.0.0** | PEP8-рефакторинг, модульные классы, фикс `/var/data`, health-check сервер |
| **v2.1** | SQLite-база: шаблоны, кэш YouTube, история диалогов |
| **v2.0** | Асинхронная версия (aiogram → python-telegram-bot v20) |
| **v1.x** | Синхронный прототип |

---

## 🗺 Roadmap

| Версия | Что планируется |
|--------|----------------|
| **v8.0.0** | Мультимодальный ввод: OCR для изображений/PDF → суммаризация текста из сканов |
| **v9.0.0** | Персонализированные модели: дообучение Whisper и LLM на данных конкретного пользователя (с согласием + шифрование) |
| **v10.0.0** | Социальный слой: публичные каналы саммари, голосование за полезность |
| **v11.0.0** | Автопланирование: бот предлагает календарные события на основе извлечённых экшен-планов (интеграция с Google Calendar/Outlook) |
| **v12.0.0** | Edge-развёртывание: версия для работы на Raspberry Pi / Jetson Nano (полностью офлайн) |

---

## 📜 Лицензия

MIT — подробности в [`LICENSE`](LICENSE).

---

<div align="center">

**Автор:** [affigeny](https://github.com/affigeny) · Сделано для собственного EdTech-проекта и демонстрации AI-навыков

</div>
