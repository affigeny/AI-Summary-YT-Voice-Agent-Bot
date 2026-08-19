# YT_Bot_Sum

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://t.me/YT_Bot_Sum_bot)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/affigeny/YT_Bot_Sum/blob/main/LICENSE)
[![GitHub release](https://img.shields.io/github/v/tag/affigeny/YT_Bot_Sum)](https://github.com/affigeny/YT_Bot_Sum/tags)
[![Render](https://img.shields.io/badge/Deploy-Render-EB6030?logo=render)](https://render.com/)

> Telegram-бот для транскрибации аудио/видео в текст с умным обходом YouTube блокировок.

## 🚀 Возможности

| Функция | Описание |
|---------|----------|
| 📹 **YouTube / Shorts** | Извлечение субтитров с обходом bot-check |
| 🎙 **Голосовые / аудио** | Распознавание речи через Whisper |
| 🤖 **LLM-переработка** | Суммаризация через OpenRouter |
| 🛠 **Кастомные шаблоны** | Свой промпт для анализа |
| 💬 **Интерактивный чат** | Диалог с ИИ по контенту |
| ⬇️ **Скачивание медиа** | Видео/аудио в любом качестве |
| 🩺 **Health-check** | Обход засыпания на Render Free |
| 🧪 **YouTube bypass** | 5 методов обхода блокировок |

## 🛠 Технологии

| Компонент | Технология |
|-----------|------------|
| Фреймворк | python-telegram-bot 21.x |
| Транскрибация | faster-whisper (модель `small`) |
| YouTube | yt-dlp с browser cookies |
| LLM | OpenRouter (Nemotron, GPT-4o-mini) |
| База данных | SQLite |
| Деплой | Render Web Service (Free) |

## 📦 Установка

```bash
git clone https://github.com/affigeny/YT_Bot_Sum.git
cd YT_Bot_Sum
pip install -r requirements.txt
```

## 🚀 Деплой

### Локально
```bash
python bot.py
```

### Render (автоматический)
Репозиторий подключён к Render. Каждый push в `main` вызывает автоматический деплой.

**Проверить статус:** https://render.com/dashboard

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