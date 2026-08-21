# Деплой Telegram-бота на Render (Web Service, Docker)

## Текущая архитектура (диагноз прошлых проблем)

1. **Тип `Web Service`, а не `Background Worker`.** Free-тариф Render доступен
   только для Web Service / Static Site. Бот запускается как `Web Service` и
   держит фиктивный HTTP-сервер на `$PORT` (эндпоинт `/health` → `200 OK`),
   чтобы сервис не «засыпал» и проходил health-check.
2. **FFmpeg обязателен.** `pydub` не декодирует аудио/голос без FFmpeg — бот
   падал на первом голосовом. `Dockerfile` ставит FFmpeg.
3. **OpenAI-совместимый endpoint.** Код шлёт запрос на `{AI_API_URL}/chat/completions`.
   Провайдер — Google Gemini через `https://generativelanguage.googleapis.com/v1beta/openai`
   (бесплатно, не нужен OpenRouter). Формат `messages` и `choices[0].message.content`
   совпадают со схемой OpenAI, поэтому `LLMClient` работает без изменений.

## Подготовлено

- `Dockerfile` — образ с FFmpeg + Python 3.11.
- `render.yaml` — Blueprint-конфиг (web + docker, healthCheckPath: /health).
- `bot.py` — модульный код, `run_polling()` + встроенный health-check сервер.

## Шаги деплоя

### Вариант A — Blueprint (автоматически)
1. Render: **New + → Blueprint** → подключи репозиторий `affigeny/YT_Bot_Sum`.
2. Вставь env vars из панели (см. ниже). `render.yaml` уже задаёт большинство.

### Вариант B — вручную
1. **New + → Web Service**
2. **Repo**: `https://github.com/affigeny/YT_Bot_Sum`
3. **Runtime**: Docker
4. **Env vars**:
   - `TELEGRAM_BOT_TOKEN` = токен от BotFather
   - `AI_PROVIDER` = `openai`
   - `AI_API_URL` = `https://generativelanguage.googleapis.com/v1beta/openai`
   - `AI_API_KEY` = `AQ.Ab8...Google...`
   - `AI_MODEL` = `gemini-3.6-flash`
   - `DB_PATH` = `/var/data/bot_database.db`
   - `YT_COOKIES_FILE` (опц.) = `/var/data/cookies.txt` — см. ниже про bot-check
5. **Deploy** и следи за логами.

## Как обойти YouTube bot-check (субтитры не качаются)

YouTube всё чаще требует авторизацию («Sign in to confirm you're not a bot»).
Бот это детектирует и предлагает задать куки:

- **Локально:** `export YT_COOKIES_FROM_BROWSER=chrome` (возьмёт куки из
  установленного Chrome/Firefox/Safari на той же машине).
- **На Render:** сгенерируй `cookies.txt` локально:
  `yt-dlp --cookies-from-browser chrome -o cookies.txt "https://youtube.com"`
  Залей его в Render **Secret Files** как `/var/data/cookies.txt` и задай
  `YT_COOKIES_FILE=/var/data/cookies.txt`.

Без куки бот всё равно попробует fallback — скачать аудио и распознать через
локальный Whisper, но и он может быть заблокирован тем же bot-check.

## Что проверить после деплоя

1. Логи Render: `Бот 4.1.2 запущен: polling + health-check сервер.`
2. Открой `https://<сервис>.onrender.com/health` → должно вернуть `200 OK`.
3. В Telegram напиши боту `/start` — должен ответить приветствием с версией.
4. Отправь YouTube-ссылку — бот должен извлечь субтитры (или аудио→Whisper).
