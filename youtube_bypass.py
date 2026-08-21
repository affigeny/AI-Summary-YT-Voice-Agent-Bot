"""Интерфейс обхода блокировок YouTube (/bypass) — v5.0.

Что здесь:
  - /bypass — меню выбора предпочтительного метода получения транскрипта;
  - кнопка «🧪 Тест методов» — прогон всех методов на видео и отчёт.

Сами методы реализованы в yt_transcript.py, хранение выбора — в BotDatabase
(таблица user_settings). Этот модуль — только UI.

Важно: колбэки имеют префикс `bypass:` и маршрутизируются единым
диспетчером MediaBot.handle_callback_query (раньше отдельный
CallbackQueryHandler конкурировал с catch-all из bot.py и кнопки
не отвечали).
"""

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes

import yt_transcript

logger = logging.getLogger(__name__)

YT_TEST_VIDEO = os.getenv("YT_TEST_VIDEO", "jNQXAC9IVRw")  # "Me at the zoo", 19 сек


def _keyboard(current: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                ("✅ " if current == "auto" else "") + "🤖 Авто (вся цепочка)",
                callback_data="bypass:auto",
            )
        ]
    ]
    for key, label in yt_transcript.METHOD_LABELS.items():
        if key == "cookies" and not yt_transcript.get_cookies_file():
            continue  # куки не настроены — не показываем
        mark = "✅ " if current == key else ""
        rows.append(
            [InlineKeyboardButton(mark + label, callback_data=f"bypass:{key}")]
        )
    rows.append(
        [InlineKeyboardButton("🧪 Тест всех методов", callback_data="bypass:test")]
    )
    return InlineKeyboardMarkup(rows)


async def show_bypass_menu(message, db, user_id: int):
    """Показывает/обновляет меню выбора метода (message — Message или CallbackQuery)."""
    current = db.get_bypass_method(user_id)
    current_label = (
        "🤖 Авто (вся цепочка)" if current == "auto"
        else yt_transcript.METHOD_LABELS.get(current, current)
    )
    text = (
        "⚙️ Обход блокировок YouTube\n\n"
        "Транскрипт добывается цепочкой: InnerTube → kome.ai → Android VR → "
        "Piped → yt-dlp web → TV → mWeb → Invidious → куки. kome.ai тянет "
        "транскрипт со своих серверов — работает даже там, где YouTube "
        "блокирует наш IP. Если субтитры не прошли — бот скачает аудио и "
        "распознает речь (Whisper).\n\n"
        f"Текущий режим: <b>{current_label}</b>\n\n"
        "Можно закрепить конкретный метод — он будет пробоваться первым.\n"
        "🤖 Авто — рекомендуется: пробуются все по порядку."
    )
    keyboard = _keyboard(current)
    if hasattr(message, "edit_text"):  # Message
        await message.reply_text(text, reply_markup=keyboard)
    else:  # CallbackQuery
        await message.edit_message_text(text, reply_markup=keyboard)


async def handle_bypass_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 media_bot):
    """Обрабатывает колбэки bypass:* (вызывается из MediaBot.handle_callback_query)."""
    query = update.callback_query
    user_id = query.from_user.id
    action = (query.data or "").split(":", 1)[1]

    if action == "test":
        await run_test_and_report(query, media_bot, user_id)
        return

    media_bot.db.set_bypass_method(user_id, action)
    label = (
        "🤖 Авто (вся цепочка)" if action == "auto"
        else yt_transcript.METHOD_LABELS.get(action, action)
    )
    await query.edit_message_text(
        f"✅ Метод закреплён: <b>{label}</b>\n\n"
        "Теперь пришлите ссылку на YouTube (или просто ID видео) — "
        "попробую этим методом первым.",
        parse_mode="HTML",
    )


async def run_test_and_report(query, media_bot, user_id: int):
    """Прогоняет все методы (+аудио-fallback) на видео и шлёт отчёт."""
    session = media_bot.user_sessions.get(user_id) or {}
    video_id = session.get("video_id") or YT_TEST_VIDEO
    try:
        await query.edit_message_text(
            f"🧪 Тестирую все методы на видео {video_id}…\n(до двух минут, подождите)"
        )
    except Exception:  # noqa: BLE001 — старое сообщение могли удалить
        pass

    results = await media_bot._run_sync(
        yt_transcript.test_methods_sync, video_id, timeout=180
    )

    lines = ["🧪 Тест методов обхода (субтитры):\n"]
    first_ok = None
    for key, res in results.items():
        icon = "✅" if res["ok"] else "❌"
        lines.append(f"{icon} {res['label']} — {res['note']}")
        if res["ok"] and first_ok is None:
            first_ok = key

    # Аудио-fallback: на датацентровых IP субтитры часто заблокированы,
    # но аудио качается — тогда бот транскрибирует ссылки через Whisper.
    try:
        audio = await media_bot._run_sync(
            yt_transcript.test_audio_sync, video_id, timeout=240
        )
    except Exception as exc:  # noqa: BLE001
        audio = {"ok": False, "note": str(exc)[:60], "client": ""}
    icon = "✅" if audio.get("ok") else "❌"
    lines.append(f"\n{icon} 🎧 Аудио для Whisper — {audio.get('note', 'пропущен')}")

    if first_ok:
        media_bot.db.set_bypass_method(user_id, first_ok)
        lines.append(
            f"\n🎯 Рекомендую: <b>{yt_transcript.METHOD_LABELS[first_ok]}</b> "
            "(уже закреплён)."
        )
    elif audio.get("ok"):
        lines.append(
            "\n🎯 Субтитры заблокированы, но аудио качается: ссылки будут "
            "транскрибированы через Whisper (медленнее, но работает)."
        )
    else:
        lines.append(
            "\n⚠️ Ни субтитры, ни аудио не проходят с этого сервера.\n"
            "Надёжное решение — куки. Локально выполните:\n"
            "<code>yt-dlp --cookies-from-browser chrome --cookies cookies.txt \"URL любого видео\"</code>\n"
            "и вставьте содержимое cookies.txt в переменную YT_COOKIES "
            "в настройках Render (Env).\n"
            "Альтернативы: прокси (YT_PROXY) или присылать аудиофайлы напрямую."
        )
    try:
        await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await query.message.reply_text("\n".join(lines))


def register_bypass_command(app, media_bot):
    """Регистрирует /bypass. Колбэки идёт через общий диспетчер MediaBot."""

    async def cmd_bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await show_bypass_menu(update.effective_message, media_bot.db,
                               update.effective_user.id)

    app.add_handler(CommandHandler("bypass", cmd_bypass))
    logger.info("✓ /bypass menu registered")
