"""
YT_Bot_Sum v4.3.1 — YouTube Bypass Interface (исправленная)
Улучшена обработка ошибок YouTube blocking
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
import yt_dlp
import asyncio
import os
import logging

logger = logging.getLogger(__name__)

# ==================== КОНФИГ ====================
YOUTUBE_BYPASS_METHODS = {
    "no_cookies": "🚫 Без куки",
    "cookie_file": "📁 Файл куки",
    "browser_chrome": "🌐 Chrome",
    "browser_firefox": "🦊 Firefox",
    "test_all": "🧪 Тест всех"
}

# Текущий метод для каждого пользователя
user_bypass_methods = {}

# Callback-data prefixes, owned by youtube_bypass (used for handler routing).
BYPASS_CALLBACK_PREFIXES = ("bypass_", "stats", "help")


# ==================== ИСКЛЮЧЕНИЯ ====================
class YouTubeBlockingError(Exception):
    """Исключение при блокировке YouTube"""
    pass


# ==================== КНОПКИ ====================
def get_bypass_keyboard():
    """Создать клавиатуру с методами обхода"""
    keyboard = [
        [
            InlineKeyboardButton("🚫 Без куки", callback_data="bypass_no_cookies"),
            InlineKeyboardButton("📁 Файл куки", callback_data="bypass_cookie_file"),
        ],
        [
            InlineKeyboardButton("🌐 Chrome", callback_data="bypass_browser_chrome"),
            InlineKeyboardButton("🦊 Firefox", callback_data="bypass_browser_firefox"),
        ],
        [
            InlineKeyboardButton("🧪 Тест всех методов", callback_data="bypass_test_all"),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
            InlineKeyboardButton("ℹ️ Помощь", callback_data="help"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# ==================== ОБРАБОТЧИКИ ====================
async def cmd_bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /bypass — выбор метода обхода"""
    user_id = update.effective_user.id
    
    current = user_bypass_methods.get(user_id, "no_cookies")
    
    await update.message.reply_text(
        f"🔧 *Выбор метода обхода YouTube*\n\n"
        f"Текущий: **{current}**\n\n"
        "Выберите метод ниже:",
        parse_mode="Markdown",
        reply_markup=get_bypass_keyboard()
    )


async def cb_bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора метода"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data.startswith("bypass_"):
        method = data.replace("bypass_", "")
        user_bypass_methods[user_id] = method
        
        await query.edit_message_text(
            f"✅ Выбран метод: **{method}**\n\n"
            f"Теперь отправьте ссылку на YouTube для транскрибации."
        )
    
    elif data == "stats":
        await query.edit_message_text(
            f"📊 *Статистика*\n\n"
            f"Всего пользователей: 1\n"
            f"Текущий метод: {user_bypass_methods.get(user_id, 'no_cookies')}"
        )
    
    elif data == "help":
        await query.edit_message_text(
            "📖 *Помощь:*\n\n"
            "🚫 **Без куки** — стандартный метод\n"
            "📁 **Файл куки** — если есть cookies.txt\n"
            "🌐 **Chrome** — куки из браузера Chrome\n"
            "🦊 **Firefox** — куки из браузера Firefox\n"
            "🧪 **Тест всех** — проверка всех методов\n\n"
            "После выбора отправьте ссылку на YouTube."
        )


# ==================== YOUTUBE SERVICE ====================
def create_ytdlp_opts(method: str) -> dict:
    """Создать опции yt-dlp для указанного метода"""
    opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'timeout': 30,
        'http_timeout': 15,
        'socket_timeout': 10,
    }
    
    if method == "cookie_file":
        cookies_file = os.getenv("YT_COOKIES_FILE", "")
        if cookies_file and os.path.exists(cookies_file):
            opts['cookiefile'] = cookies_file
            logger.info(f"Using cookie file: {cookies_file}")
        else:
            logger.warning("Cookie file not found")
    
    elif method.startswith("browser_"):
        browser = method.replace("browser_", "")
        opts['cookiesfrombrowser'] = (browser,)
        logger.info(f"Using browser cookies: {browser}")
    
    return opts


def detect_blocking(error: Exception) -> bool:
    """Определить, является ли ошибка блокировкой YouTube"""
    error_str = str(error).lower()
    
    blocking_indicators = [
        'sign in to confirm you are not a bot',
        'video unavailable',
        'private video',
        'member-only video',
        'has been removed',
        'has been blocked',
        'age-restricted',
        'youtube.exceptions.VideoUnavailable',
    ]
    
    return any(indicator in error_str for indicator in blocking_indicators)


async def test_all_methods(url: str, user_id: int) -> dict:
    """Протестировать все методы обхода"""
    results = {}
    
    methods = ["no_cookies", "browser_chrome", "browser_firefox"]
    
    # Добавляем cookie_file если есть файл
    cookies_file = os.getenv("YT_COOKIES_FILE", "")
    if cookies_file and os.path.exists(cookies_file):
        methods.append("cookie_file")
    
    for method in methods:
        try:
            opts = create_ytdlp_opts(method)
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                results[method] = True
                logger.info(f"Test {method}: OK")
                
        except Exception as e:
            is_blocking = detect_blocking(e)
            results[method] = False
            logger.warning(f"Test {method}: FAILED - {str(e)[:100]}")
            
            if is_blocking:
                logger.warning(f"YouTube blocking detected for method: {method}")
    
    return results


async def download_with_retry(url: str, method: str, max_retries: int = 3) -> dict:
    """Скачать аудио с попытками использовать разные методы"""
    last_error = None
    
    # Если выбран конкретный метод, пробуем его
    if method != "test_all":
        try:
            return await _download_single_method(url, method)
        except Exception as e:
            last_error = e
            logger.warning(f"Method {method} failed: {e}")
    
    # Если не получилось, пробует все методы
    methods_to_try = ["browser_chrome", "browser_firefox", "no_cookies"]
    
    # Добавляем cookie_file если есть
    cookies_file = os.getenv("YT_COOKIES_FILE", "")
    if cookies_file and os.path.exists(cookies_file):
        methods_to_try.insert(0, "cookie_file")
    
    for try_method in methods_to_try:
        if try_method == method:
            continue  # Уже пробовали
        
        try:
            result = await _download_single_method(url, try_method)
            result['used_method'] = try_method
            logger.info(f"Successfully downloaded using method: {try_method}")
            return result
        except Exception as e:
            last_error = e
            logger.warning(f"Method {try_method} failed: {e}")
    
    # Все методы не сработали
    raise YouTubeBlockingError(f"Все методы обхода не сработали. Последняя ошибка: {str(last_error)[:200]}")


async def _download_single_method(url: str, method: str) -> dict:
    """Скачать аудио одним методом"""
    opts = create_ytdlp_opts(method)
    
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        audio_path = ydl.prepare_filename(info)
        
        # Конвертируем в mp3 если нужно
        if not audio_path.endswith('.mp3'):
            audio_path = audio_path.rsplit('.', 1)[0] + '.mp3'
            if os.path.exists(ydl.prepare_filename(info)):
                os.rename(ydl.prepare_filename(info), audio_path)
        
        return {
            'success': True,
            'path': audio_path,
            'info': info,
            'method': method
        }


async def handle_youtube_bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка YouTube ссылки с обходом"""
    user_id = update.effective_user.id
    url = update.message.text.strip()
    
    # Определяем метод
    method = user_bypass_methods.get(user_id, "no_cookies")
    
    progress_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")
    
    try:
        if method == "test_all":
            # Тестируем все методы
            await progress_msg.edit_text("🧪 Тестирую все методы обхода...")
            
            results = await test_all_methods(url, user_id)
            
            # Находим лучший метод
            best_method = "no_cookies"
            for m, success in results.items():
                if success:
                    best_method = m
                    break
            
            # Сохраняем лучший метод
            user_bypass_methods[user_id] = best_method
            
            # Формируем результат
            result_text = "✅ *Тест завершён!*\n\n"
            result_text += f"🚫 Без куки: {'✅' if results.get('no_cookies') else '❌'}\n"
            result_text += f"📁 Файл куки: {'✅' if results.get('cookie_file') else '❌'}\n"
            result_text += f"🌐 Chrome: {'✅' if results.get('browser_chrome') else '❌'}\n"
            result_text += f"🦊 Firefox: {'✅' if results.get('browser_firefox') else '❌'}\n\n"
            result_text += f"🎯 Лучший метод: **{best_method}**\n\n"
            result_text += "Теперь отправьте ссылку ещё раз для транскрибации."
            
            await progress_msg.edit_text(result_text, parse_mode="Markdown")
            
        else:
            # Скачиваем с попыткой обхода
            await progress_msg.edit_text("🎵 Скачиваю аудио...")
            
            result = await download_with_retry(url, method)
            
            await progress_msg.edit_text("✅ Аудио скачано! Распознаю речь...")
            
            # Отправляем инфо о видео
            info = result['info']
            text = f"📝 *Видео загружено!*\n\n"
            text += f"🎬 Название: {info.get('title', 'N/A')}\n"
            text += f"⏱ Длительность: {info.get('duration', 0)} сек\n"
            text += f"🔧 Метод: {result['method']}\n\n"
            text += f"📁 Файл готов для транскрибации."
            
            await progress_msg.edit_text(text, parse_mode="Markdown")
            
            # Отправляем файл
            if os.path.exists(result['path']):
                with open(result['path'], 'rb') as f:
                    await update.message.reply_document(
                        f,
                        caption="🎵 Аудиофайл"
                    )
                os.unlink(result['path'])
            
    except YouTubeBlockingError as e:
        await progress_msg.edit_text(
            f"❌ *YouTube блокирует загрузку*\n\n"
            f"Ошибка: {str(e)[:100]}\n\n"
            f"💡 *Решения:*\n"
            f"1. Напишите /bypass и выберите '📁 Файл куки'\n"
            f"2. Создайте файл куки: `yt-dlp --cookies-from-browser chrome -o cookies.txt URL`\n"
            f"3. Загрузите файл боту или настройте YT_COOKIES_FILE\n\n"
            f"Или попробуйте '🧪 Тест всех методов' для диагностики."
        )
        
    except Exception as e:
        logger.error(f"Error processing YouTube: {e}", exc_info=True)
        await progress_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")


# ==================== РЕГИСТРАЦИЯ ====================
def get_user_method(user_id: int) -> str:
    """Вернуть текущий метод обхода пользователя (для bot.py)."""
    return user_bypass_methods.get(user_id, "no_cookies")


def register_youtube_handlers(dp):
    """Регистрация обработчиков в боте.

    Важно: CallbackQueryHandler регистрируется с pattern-фильтром, чтобы
    перехватывать ТОЛЬКО bypass-кнопки и не конфликтовать с основным
    обработчиком bot.py, который слушает callback'и шаблонов/скачивания.
    """
    from telegram.ext import CommandHandler, CallbackQueryHandler
    import re

    dp.add_handler(CommandHandler("bypass", cmd_bypass))
    # Маршрутизируем только bypass-кнопки: bypass_*, stats, help
    dp.add_handler(
        CallbackQueryHandler(
            cb_bypass,
            pattern=re.compile(r'^(bypass_|stats$|help$)'),
        )
    )

    logger.info("✓ YouTube bypass handlers registered")
    logger.info("✓ Commands: /bypass, callback buttons")
    logger.info("✓ Callback queries registered (pattern-filtered)")
