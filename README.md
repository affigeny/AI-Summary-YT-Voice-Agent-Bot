# YT_Bot_Sum

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://t.me/YT_Bot_Sum_bot)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/affigeny/YT_Bot_Sum/blob/main/LICENSE)
[![GitHub release](https://img.shields.io/github/v/tag/affigeny/YT_Bot_Sum)](https://github.com/affigeny/YT_Bot_Sum/tags)
[![Render](https://img.shields.io/badge/Deploy-Render-EB6030?logo=render)](https://render.com/)

> Telegram-бот: ссылка YouTube / ID видео / аудио / голосовое → транскрипт → переработка по шаблону через ИИ. Продвинутый обход блокировок YouTube.

## 🚀 Возможности

| Функция | Описание |
|---------|----------|
| 📹 **YouTube / Shorts / ID** | Ссылка, shorts или просто 11-символьный ID видео |
| 🛡 **Обход блокировок** | Цепочка из 7 методов (см. ниже), прокси и куки из env |
| 🎙 **Голосовые / аудио / видео** | Распознавание через faster-whisper (язык — авто) |
| 🤖 **LLM-переработка** | Саммари · Минто · экшен-план · конспект · свои шаблоны |
| 💾 **История транскриптов** | Всё сохраняется в БД, `/history` — обработать заново |
| 💬 **Интерактивный чат** | Диалог с ИИ по содержимому после обработки |
| ⬇️ **Скачивание медиа** | Видео/аудио в любом качестве кнопками |
| 🩺 **Health-check** | Обход засыпания на Render Free |

## 🛠 Технологии

| Компонент | Технология |
|-----------|------------|
| Фреймворк | python-telegram-bot 21.x |
| Транскрипт YouTube | yt_transcript.py — многоходовая цепочка методов |
| Транскрибация аудио | faster-whisper (модель `small`, VAD, авто-язык) |
| LLM | OpenRouter / OpenAI-совместимый API |
| База данных | SQLite (шаблоны, транскрипты, кэш, настройки) |
| Деплой | Render Web Service (Free) + `/health` |

## 🛡 Цепочка обхода блокировок YouTube

Движок `yt_transcript.py` пробует методы по порядку до первого успеха:

| # | Метод | Как работает |
|---|-------|--------------|
| 1 | ⚡ InnerTube API | `youtube-transcript-api` — быстрый прямой API-вызов |
| 2 | 🛰️ kome.ai API | Внешний сервис тянет транскрипт со своих серверов — работает даже с датацентровых IP, которые YouTube блокирует |
| 3 | 🤖 yt-dlp Android VR | `player_client=android_vr` — без PO-токена, стабилен |
| 4 | 💧 Piped-зеркала | Публичные зеркала, проксирующие YouTube |
| 5 | 🔧 yt-dlp (web) | Стандартный клиент с браузерным User-Agent |
| 6 | 📺 yt-dlp TV-клиент | `player_client=tv` — не требует PO-токен |
| 7 | 📱 yt-dlp mWeb | `player_client=mweb` — мобильный веб-клиент |
| 8 | 🪞 Invidious-зеркала | Публичные зеркала (список проверяется, env `INVIDIOUS_INSTANCES`) |
| 9 | 🍪 yt-dlp + куки | Куки аккаунта (env `YT_COOKIES` или файл) |

Если субтитров нет или все методы заблокированы — бот скачивает аудио
(клиенты android_vr → tv → mweb → web → куки) и распознаёт речь локально
через Whisper.

`/bypass` — закрепить конкретный метод или прогнать «🧪 Тест всех методов»:
отчёт по субтитрам **и** по скачиванию аудио (на датацентровых IP субтитры
часто блокируются, а аудио качается — тогда бот работает через Whisper).

## ⚙️ Переменные окружения

| Переменная | Назначение | По умолчанию |
|------------|-----------|--------------|
| `TELEGRAM_BOT_TOKEN` | Токен бота | — |
| `AI_API_KEY` / `AI_API_URL` / `AI_MODEL` | LLM (OpenRouter) | openrouter / gemini-2.0-flash-exp:free |
| `WHISPER_MODEL` | Модель faster-whisper | `small` |
| `YT_COOKIES` | **Содержимое** cookies.txt в env (для Render) | — |
| `YT_COOKIES_FILE` | Путь к файлу куки | — |
| `YT_PROXY` | Прокси для всех запросов к YouTube | — |
| `LLM_MAX_CHARS` | Лимит контекста для LLM (голова+хвост) | 24000 |
| `WHISPER_TIMEOUT` | Таймаут распознавания, сек | 900 |
| `INVIDIOUS_INSTANCES` / `PIPED_INSTANCES` | Свои зеркала (через запятую) | публичные |
| `DB_PATH` | Путь к SQLite | bot_database.db |

## 📱 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Справка и начало работы |
| `/bypass` | Выбор/тест метода обхода YouTube |
| `/templates` | Список шаблонов |
| `/add_template ID \| Название \| Промпт` | Свой шаблон (появится на кнопках) |
| `/del_template ID` | Удалить свой шаблон |
| `/history` | Сохранённые транскрипты — обработать заново |
| `/stats` | Статистика |

После получения транскрипта появляются кнопки: шаблоны (2 в ряд) · 📄 полный транскрипт файлом · 🕘 история · ⬇️ скачать видео/аудио.

## 📦 Установка

```bash
git clone https://github.com/affigeny/YT_Bot_Sum.git
cd YT_Bot_Sum
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... python bot.py
```

### Render
Репозиторий подключён к Render: каждый push в `main` — автодеплой. Секреты задаются в панели.

## 📊 Версии

| Версия | Дата | Изменения |
|--------|------|-----------|
| **5.2.0** | 2026-08-21 | Добавлен метод 🛰️ kome.ai (внешний API отдаёт транскрипт со своих серверов — решает блокировку датацентровых IP Render) |
| 5.1.0 | 2026-08-20 | Обновлены живые зеркала Invidious/Piped, добавлен mWeb-клиент, «Тест методов» теперь проверяет и скачивание аудио (Whisper-путь), расширенная цепочка клиентов аудио-fallback |
| 5.0.0 | 2026-08-20 | Новый движок обхода (7 методов), единый диспетчер колбэков (починены кнопки), приём голого ID видео, аудио/видео файлы, история транскриптов, куки/прокси из env, фикс таймаутов Whisper и LLM, удалён мёртвый transcription.py |
| 4.3.4 | 2026-08-19 | Попытка починить кнопки (безуспешно — конфликт хендлеров) |
| 4.3.0 | 2026-08-19 | Первый youtube_bypass |

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
