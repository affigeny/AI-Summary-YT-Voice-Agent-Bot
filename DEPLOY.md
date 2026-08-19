# Деплой Telegram-бота на Render (Background Worker, Docker)

## Причина прошлых проблем (диагноз)

1. **Нужен тип `Background Worker`, а не `Web Service`.** Бот использует `run_polling()` — долгоживущий процесс. Web Service ждёт HTTP-порт и на free-тире умирает.
2. **Нет FFmpeg в образе.** `pydub` не может декодировать аудио без FFmpeg — бот падает на первом голосовом.
3. **Неверный `AI_API_URL`.** В коде запрос идёт на `{AI_API_URL}/chat/completions`, т.е. нужен OpenAI-совместимый endpoint.

## Что уже подготовлено

- `Dockerfile` — образ с FFmpeg + Python 3.11.
- `render.yaml` — Blueprint-конфиг (worker+docker).

## Шаги деплоя

### Вариант A — Blueprint (проще, ферзьv автоматически)

1. На Render: **New + → Blueprint** → подключи репозиторий.
2. В каждом env var вставь значения (из SECRETS.env).

### Вариант B — вручную (если Blueprint не цепляется)

1. **New + → Background Worker**
2. **Repo**: `https://github.com/affigeny/YT_Bot_Sum`
3. **Runtime**: Docker
4. **Env vars** (Environment → Environment Variables):
   - `TELEGRAM_BOT_TOKEN` = твой токен
   - `AI_PROVIDER` = `openai`
   - `AI_API_URL` = `https://openrouter.ai/api/v1` (OpenAI-совместимый)
   - `AI_API_KEY` = `sk-or-v1-...` (OpenRouter)
   - `AI_MODEL` = `openai/gpt-4o-mini`
   - `DB_PATH` = `bot_database.db`
5. **Deploy** и следи за логами.

## Какой AI-провайдер выбрать

Рекомендую **OpenRouter** (`https://openrouter.ai/api/v1`), т.к. код заточен под `chat/completions`.

## Что проверить после деплоя

1. Логи Render: должно появиться `🚀 v2.0.0 Асинхронный ИИ-Бот ... запущен...`
2. В Telegram написать боту `/start` — должен ответить приветствием.
