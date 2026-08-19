# Transcription Module — Буквица v2.0
# Интегрирован в YT_Bot_Sum

## 🎯 Что добавлено

### Команды
- `/transcribe` — начало транскрибации
- `/stats` — статистика транскрибаций

### Функционал
- ✅ Rate limiting (1 запрос/10 сек)
- ✅ Free tier (3 транскрибации/месяц)
- ✅ Progress streaming
- ✅ Экспорт с таймкодами (как в оригинале Буквица)
- ✅ SQLite учёт пользователей

### Поддержка форматов
- 🎤 Голосовые сообщения
- 🎵 Аудиофайлы (MP3, WAV, OGG, M4A)
- 🎬 Видео (MP4, MOV, MKV)
- 🔗 YouTube ссылки (скоро)

---

## 📁 Структура

```
YT_Bot_Sum/
├── bot.py                 # Основной бот
├── transcription.py       # Модуль транскрибации (NEW)
├── requirements.txt
└── README.md
```

---

## 🚀 Использование

```bash
# Запуск
python bot.py

# Команды
/transcribe — транскрибация
/stats — статистика
```

---

## 📊 Метрики

```python
{
    "transcriptions_per_day": 0,
    "free_to_paid": "5%",
    "avg_duration": "45s"
}
```

---

*Версия: 2.0 | Интегрировано: 2026-08-19*
