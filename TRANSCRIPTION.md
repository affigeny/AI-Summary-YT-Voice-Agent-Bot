# Транскрибация аудио/видео — как устроено в v5.0

## 🎯 Поток обработки

```
Ссылка YouTube / ID видео ──► yt_transcript.fetch_transcript_sync()
                              ├── ⚡ InnerTube API (youtube-transcript-api)
                              ├── 🔧 yt-dlp web
                              ├── 📺 yt-dlp TV-клиент      ← главный обход bot-check
                              ├── 🤖 yt-dlp Android VR
                              ├── 🪞 Invidious-зеркала
                              ├── 💧 Piped-зеркала
                              └── 🍪 yt-dlp + куки (YT_COOKIES / YT_COOKIES_FILE)
                                      │ нет субтитров/всё заблокировано
                                      ▼
                              ⬇ скачивание аудио (tv → web → cookies)
                              🎙 faster-whisper (VAD, авто-язык, CPU int8)

Голосовое / аудио / видео файл ──► faster-whisper напрямую (до 20 МБ — лимит Bot API)

Транскрипт ──► SQLite (transcripts) ──► кнопки шаблонов / 📄 файлом / /history
```

## 📁 Структура

```
YT_Bot_Sum/
├── bot.py            # Бот: хендлеры, БД, LLM, скачивание, STT, инлайн-интерфейс
├── yt_transcript.py  # Движок получения транскрипта (цепочка методов обхода)
├── youtube_bypass.py # Меню /bypass (выбор и тест методов)
└── checkpoint_manager.py # Утилита чекпойнтов (опционально)
```

## ⏱ Таймауты (env)

| Переменная | Смысл | По умолчанию |
|------------|-------|--------------|
| `YT_FETCH_TIMEOUT` | На всю цепочку методов транскрипта | 180 с |
| `YT_DOWNLOAD_TIMEOUT` | На скачивание аудио/видео | 300 с |
| `WHISPER_TIMEOUT` | На распознавание одного файла | 900 с |
| `MAX_AUDIO_SECONDS` | Обрезка слишком длинного аудио | 1800 с |

Все блокирующие вызовы выполняются в executor — event loop не замирает,
бот остаётся отзывчивым во время чужой загрузки.

---

*Версия: 5.0 | Обновлено: 2026-08-20*
