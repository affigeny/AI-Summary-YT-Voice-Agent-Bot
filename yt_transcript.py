"""Многоходовой движок получения транскриптов YouTube (v1.0).

Цепочка методов — каждый следующий включается, когда предыдущий не сработал:

  1. innertube — InnerTube API через youtube-transcript-api (быстрый, чистый HTTP);
  2. ytdlp     — yt-dlp, клиент по умолчанию (web);
  3. tv        — yt-dlp с player_client=tv (TV-клиент не требует PO-токен —
                 основной современный обход bot-check для датацентровых IP);
  4. android   — yt-dlp с player_client=android_vr (аналогично без PO-токена);
  5. invidious — публичные Invidious-зеркала (проксируют YouTube);
  6. piped     — публичные Piped-зеркала;
  7. cookies   — yt-dlp с куками (YT_COOKIES — содержимое cookies.txt прямо
                 в env, или YT_COOKIES_FILE — путь к файлу).

Прокси для всех методов: env YT_PROXY (http/https/socks5).

Куки удобно задавать на Render через переменную YT_COOKIES: экспортируйте
локально `yt-dlp --cookies-from-browser chrome --cookies cookies.txt URL`
и вставьте содержимое файла в env (файлы на Free-тариф монтировать сложнее).

Все функции синхронные (yt-dlp/requests блокирующие) — из event loop
вызывать через run_in_executor (см. MediaBot._run_sync).
"""

import html
import logging
import os
import re
import shutil
import tempfile

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Конфигурация из окружения.
# ---------------------------------------------------------------------------
YT_PROXY = os.getenv("YT_PROXY", "").strip()

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

INVIDIOUS_INSTANCES = [
    i.strip().rstrip("/")
    for i in os.getenv(
        "INVIDIOUS_INSTANCES",
        # Проверены 2026-08-20 (HTTP 200 на captions API):
        "https://inv.nadeko.net, https://invidious.nerdvpn.de, "
        "https://yewtu.be",
    ).split(",")
    if i.strip()
]

PIPED_INSTANCES = [
    i.strip().rstrip("/")
    for i in os.getenv(
        "PIPED_INSTANCES",
        # Проверены 2026-08-20 (HTTP 200 на streams API):
        "https://pipedapi.in.projectsegfau.lt, https://watchapi.whatever.social",
    ).split(",")
    if i.strip()
]

# Признаки того, что видео не существует (не путать с блокировкой).
_VIDEO_GONE_MARKERS = (
    "video unavailable",
    "private video",
    "members-only",
    "member-only",
    "has been removed",
    "does not exist",
    "not a valid url",
    "invalid request",
    "no video data",
)

# Признаки блокировки/bot-check.
_BLOCKING_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "confirm you",
    "403",
    "429",
    "too many requests",
    "forbidden",
    "blocked",
    "requires authentication",
    "cookies",
)


class TranscriptFetchError(Exception):
    """Все методы цепочки не сработали.

    Атрибуты:
        report  — список строк «метод: причина» для диагностики;
        video_gone — True, если похоже, что видео удалено/приватное
                     (тогда fallback на аудио не имеет смысла).
    """

    def __init__(self, report, video_gone=False):
        self.report = report
        self.video_gone = video_gone
        super().__init__("; ".join(report))


class TranscriptResult:
    """Результат успешного получения транскрипта."""

    __slots__ = ("text", "method", "label", "title", "duration")

    def __init__(self, text, method, label, title=None, duration=None):
        self.text = text
        self.method = method
        self.label = label
        self.title = title
        self.duration = duration


# ---------------------------------------------------------------------------
# Общие утилиты.
# ---------------------------------------------------------------------------
def _headers():
    return {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "ru,en;q=0.8",
        "Referer": "https://www.youtube.com/",
    }


def _proxies():
    if YT_PROXY:
        return {"http": YT_PROXY, "https": YT_PROXY}
    return None


def is_blocking_error(err) -> bool:
    s = str(err).lower()
    return any(marker in s for marker in _BLOCKING_MARKERS)


def is_video_gone_error(err) -> bool:
    s = str(err).lower()
    return any(marker in s for marker in _VIDEO_GONE_MARKERS)


def get_cookies_file():
    """Путь к файлу куки.

    YT_COOKIES — содержимое cookies.txt в env (актуально для Render),
    YT_COOKIES_FILE — путь к файлу на диске.
    """
    content = os.getenv("YT_COOKIES", "").strip()
    if content:
        path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path
    path = os.getenv("YT_COOKIES_FILE", "").strip()
    if path and os.path.exists(path):
        return path
    return None


def process_vtt(vtt_text: str) -> str:
    """Чистит raw VTT: убирает таймкоды, теги, служебные строки и дубли."""
    lines = []
    for raw_line in vtt_text.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or "-->" in line:
            continue
        if line.isdigit():  # индексы кубиков
            continue
        if line.startswith(("NOTE", "Kind:", "Language:")):
            continue
        # Инлайн-теги авто-субтитров (<c>, <00:00:00.000> и т.п.)
        line = re.sub(r"<[^>]+>", "", line)
        if not line:
            continue
        lines.append(html.unescape(line))
    deduped = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    text = " ".join(deduped)
    return re.sub(r"\s+", " ", text).strip()


def _clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _lang_score(lang: str) -> int:
    lang = (lang or "").lower()
    if lang.startswith("ru"):
        return 0
    if lang.startswith("en"):
        return 1
    return 2


# ---------------------------------------------------------------------------
# Метод 1: youtube-transcript-api (InnerTube API).
# ---------------------------------------------------------------------------
def _try_innertube(video_id: str):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise RuntimeError("модуль youtube-transcript-api не установлен") from exc

    langs = ["ru", "en"]
    extra_kwargs = {}
    if YT_PROXY:
        extra_kwargs["proxies"] = {"http": YT_PROXY, "https": YT_PROXY}

    # Новый API (v1.x): экземпляр + .fetch(...).
    if hasattr(YouTubeTranscriptApi, "fetch"):
        for kwargs in (extra_kwargs, {}):
            try:
                api = YouTubeTranscriptApi()
                fetched = api.fetch(video_id, languages=langs, **kwargs)
                text = _clean_text(" ".join(snippet.text for snippet in fetched))
                if text:
                    return text, None, None
            except TypeError:
                continue  # неподдерживаемые kwargs — пробуем без них
            except Exception:
                if kwargs is extra_kwargs and kwargs:
                    continue  # повторяем без прокси
                break

    # Старый API (v0.6.x): статический .get_transcript(...).
    try:
        data = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
        text = _clean_text(" ".join(row.get("text", "") for row in data))
        if text:
            return text, None, None
    except (AttributeError, TypeError):
        pass

    raise RuntimeError("субтитры не найдены через InnerTube")


# ---------------------------------------------------------------------------
# Метод 2: публичный API kome.ai (внешний сервис).
# ---------------------------------------------------------------------------
# kome.ai тянет транскрипт со СВОИХ серверов, поэтому метод работает даже с
# датацентровых IP (Render), которые YouTube блокирует. Особенность: при
# отсутствии субтитров возвращает HTTP 200 с текстом-извинением — детектим.
_KOME_UNAVAILABLE_MARKERS = (
    "transcripts aren't available",
    "aren't available for this video",
)


def _try_kome(video_id: str):
    """Транскрипт через публичный API kome.ai (внешние серверы)."""
    try:
        resp = requests.post(
            "https://kome.ai/api/transcript",
            json={"video_id": video_id, "format": True},
            headers={
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://kome.ai",
                "Referer": "https://kome.ai/tools/youtube-transcript-generator",
            },
            # kome.ai — не YouTube-эндпоинт, YT_PROXY тут не нужен.
            timeout=45,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"kome.ai недоступен ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"kome.ai: HTTP {resp.status_code}")
    try:
        text = (resp.json() or {}).get("transcript") or ""
    except ValueError as exc:
        raise RuntimeError("kome.ai: некорректный JSON") from exc
    lowered = text.lower()
    if any(marker in lowered for marker in _KOME_UNAVAILABLE_MARKERS):
        raise RuntimeError("нет субтитров (kome.ai)")
    text = _clean_text(text)
    if not text:
        raise RuntimeError("kome.ai: пустой транскрипт")
    return text, None, None


# ---------------------------------------------------------------------------
# yt-dlp: субтитры из info-dict (ручные + автогенерация).
# ---------------------------------------------------------------------------
def _ydl_base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 15,
        "retries": 2,
        "extractor_retries": 1,
        "http_headers": _headers(),
    }
    if YT_PROXY:
        opts["proxy"] = YT_PROXY
    return opts


def _pick_subtitle_url(info: dict):
    """Выбирает лучшую дорожку субтитров: ручные ru/en, затем авто ru/en."""
    candidates = []
    for source_rank, key in enumerate(("subtitles", "automatic_captions")):
        tracks = info.get(key) or {}
        for lang, formats in tracks.items():
            if not formats:
                continue
            chosen = None
            for fmt in formats:
                if fmt.get("ext") == "vtt":
                    chosen = fmt
                    break
            if chosen is None:
                chosen = formats[0]
            url = chosen.get("url")
            if not url:
                continue
            if chosen.get("ext") != "vtt":
                # timedtext-ссылки умеют отдавать vtt через параметр fmt.
                url = re.sub(r"([?&])fmt=[^&]*", r"\1fmt=vtt", url)
                if "fmt=" not in url:
                    url += ("&" if "?" in url else "?") + "fmt=vtt"
            candidates.append((_lang_score(lang), source_rank, url))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


def _try_ytdlp(video_id: str, client=None, use_cookies=False):
    import yt_dlp

    opts = _ydl_base_opts()
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    if use_cookies:
        cookies = get_cookies_file()
        if not cookies:
            raise RuntimeError("куки не настроены (YT_COOKIES / YT_COOKIES_FILE)")
        opts["cookiefile"] = cookies

    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("yt-dlp не вернул данные о видео")

    sub_url = _pick_subtitle_url(info)
    if not sub_url:
        raise RuntimeError("у видео нет субтитров")

    resp = requests.get(sub_url, headers=_headers(), proxies=_proxies(), timeout=20)
    resp.raise_for_status()
    body = resp.text
    if "<html" in body[:300].lower():
        raise RuntimeError("YouTube вернул HTML вместо субтитров (блокировка)")
    text = process_vtt(body)
    if not text:
        raise RuntimeError("дорожка субтитров пуста")
    return text, info.get("title"), info.get("duration")


# ---------------------------------------------------------------------------
# Метод 5: Invidious-зеркала.
# ---------------------------------------------------------------------------
def _try_invidious(video_id: str):
    last_error = "зеркала не ответили"
    for base in INVIDIOUS_INSTANCES:
        try:
            resp = requests.get(
                f"{base}/api/v1/captions/{video_id}",
                headers=_headers(),
                proxies=_proxies(),
                timeout=8,
            )
            if resp.status_code != 200:
                last_error = f"{base}: HTTP {resp.status_code}"
                continue
            captions = (resp.json() or {}).get("captions") or []
            if not captions:
                # Зеркало ответило, но субтитров нет — это не блокировка.
                raise RuntimeError("субтитров нет (Invidious)")
            caption = min(
                captions,
                key=lambda c: _lang_score(c.get("languageCode") or c.get("label") or ""),
            )
            cap_url = caption.get("url") or ""
            if cap_url.startswith("/"):
                cap_url = base + cap_url
            if not cap_url:
                continue
            sub = requests.get(cap_url, headers=_headers(), proxies=_proxies(), timeout=15)
            sub.raise_for_status()
            text = process_vtt(sub.text)
            if text:
                return text, None, None
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = f"{base}: {type(exc).__name__}"
            continue
    raise RuntimeError(f"Invidious: {last_error}")


# ---------------------------------------------------------------------------
# Метод 6: Piped-зеркала.
# ---------------------------------------------------------------------------
def _try_piped(video_id: str):
    last_error = "зеркала не ответили"
    for base in PIPED_INSTANCES:
        try:
            resp = requests.get(
                f"{base}/streams/{video_id}",
                headers=_headers(),
                proxies=_proxies(),
                timeout=8,
            )
            if resp.status_code != 200:
                last_error = f"{base}: HTTP {resp.status_code}"
                continue
            data = resp.json() or {}
            subtitles = data.get("subtitles") or []
            if not subtitles:
                raise RuntimeError("субтитров нет (Piped)")
            sub_info = min(subtitles, key=lambda s: _lang_score(s.get("code") or ""))
            sub_url = sub_info.get("url")
            if not sub_url:
                continue
            sub = requests.get(sub_url, headers=_headers(), proxies=_proxies(), timeout=15)
            sub.raise_for_status()
            text = process_vtt(sub.text)
            if text:
                return text, data.get("title"), data.get("duration")
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = f"{base}: {type(exc).__name__}"
            continue
    raise RuntimeError(f"Piped: {last_error}")


# ---------------------------------------------------------------------------
# Реестр методов и общая цепочка.
# Порядок выверен живыми тестами (yt-dlp 2026.07): innertube/android_vr/piped
# стабильно работают, tv-клиент в ряде версий yt-dlp падает, web-клиент часто
# ловит 429 на timedtext, Invidious-зеркала нестабильны — они в конце.
# ---------------------------------------------------------------------------
FETCH_METHODS = [
    ("innertube", "⚡ InnerTube API", _try_innertube),
    ("kome", "🛰️ kome.ai API", _try_kome),
    ("android", "🤖 yt-dlp Android VR", lambda vid: _try_ytdlp(vid, client="android_vr")),
    ("piped", "💧 Piped-зеркала", _try_piped),
    ("ytdlp", "🔧 yt-dlp (web)", lambda vid: _try_ytdlp(vid)),
    ("tv", "📺 yt-dlp TV-клиент", lambda vid: _try_ytdlp(vid, client="tv")),
    ("mweb", "📱 yt-dlp mWeb", lambda vid: _try_ytdlp(vid, client="mweb")),
    ("invidious", "🪞 Invidious-зеркала", _try_invidious),
    ("cookies", "🍪 yt-dlp + куки", lambda vid: _try_ytdlp(vid, use_cookies=True)),
]

METHOD_LABELS = {key: label for key, label, _fn in FETCH_METHODS}


def fetch_transcript_sync(video_id: str, preferred: str = "auto") -> TranscriptResult:
    """Перебирает методы цепочкой и возвращает первый успешный результат.

    preferred — ключ метода, который пробовать первым (настройка /bypass);
    "auto" — стандартный порядок. Если несколько методов подряд сообщают,
    что субтитров нет вообще, цепочка завершается раньше (быстрый fallback
    бота на аудио + Whisper).
    """
    order = list(FETCH_METHODS)
    if preferred and preferred != "auto":
        order.sort(key=lambda m: 0 if m[0] == preferred else 1)

    report = []
    no_subs_votes = 0
    video_gone = False
    for key, label, fn in order:
        try:
            text, title, duration = fn(video_id)
            if text and text.strip():
                logger.info("Транскрипт %s получен методом %s", video_id, key)
                return TranscriptResult(text, key, label, title, duration)
            report.append(f"{label}: пустой результат")
        except Exception as exc:
            err_text = str(exc)
            lowered = err_text.lower()
            if is_video_gone_error(exc):
                video_gone = True
                report.append(f"{label}: видео недоступно")
            elif "нет субтитров" in lowered or "не найдены" in lowered:
                no_subs_votes += 1
                report.append(f"{label}: нет субтитров")
            elif is_blocking_error(exc):
                report.append(f"{label}: блокировка YouTube")
            else:
                report.append(f"{label}: {err_text[:80]}")
            logger.debug("Метод %s для %s не сработал: %s", key, video_id, err_text[:120])
            # Три независимых источника говорят, что субтитров нет —
            # дальше цепочку не гоняем, бот уйдёт в Whisper-fallback.
            if no_subs_votes >= 3:
                break

    raise TranscriptFetchError(report, video_gone=video_gone)


def test_methods_sync(video_id: str) -> dict:
    """Прогоняет все методы по очереди — для /bypass → «Тест методов»."""
    results = {}
    for key, label, fn in FETCH_METHODS:
        if key == "cookies" and not get_cookies_file():
            results[key] = {"label": label, "ok": False, "note": "куки не настроены"}
            continue
        try:
            text, _title, _dur = fn(video_id)
            results[key] = {
                "label": label,
                "ok": bool(text and text.strip()),
                "note": f"{len(text)} симв." if text else "пусто",
            }
        except Exception as exc:
            note = str(exc)[:60]
            if is_video_gone_error(exc):
                note = "видео недоступно"
            elif is_blocking_error(exc):
                note = "блокировка YouTube"
            results[key] = {"label": label, "ok": False, "note": note}
    return results


def test_audio_sync(video_id: str) -> dict:
    """Проверяет скачивание аудио — путь Whisper-fallback (/bypass → Тест).

    На датацентровых IP субтитры часто заблокированы, но аудио отдельными
    клиентами качается: тест показывает, заработает ли транскрипция ссылок
    через локальное распознавание. Возвращает {"ok", "note", "client"}.
    """
    import yt_dlp

    attempts = [("android_vr", False), ("tv", False), ("mweb", False), (None, False)]
    if get_cookies_file():
        attempts.append((None, True))

    out_dir = tempfile.mkdtemp(prefix="bypasstest_")
    last_note = "не пробовалось"
    try:
        for client, use_cookies in attempts:
            label = (client or "web") + ("+куки" if use_cookies else "")
            opts = _ydl_base_opts()
            opts.update(
                {
                    "skip_download": False,
                    "format": "bestaudio/best",
                    "noplaylist": True,
                    "outtmpl": os.path.join(out_dir, "test.%(ext)s"),
                }
            )
            if client:
                opts["extractor_args"] = {"youtube": {"player_client": [client]}}
            if use_cookies:
                opts["cookiefile"] = get_cookies_file()
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(
                        f"https://www.youtube.com/watch?v={video_id}", download=True
                    )
                    path = ydl.prepare_filename(info)
                if path and os.path.exists(path) and os.path.getsize(path) > 1024:
                    size_kb = os.path.getsize(path) // 1024
                    return {
                        "ok": True,
                        "note": f"{size_kb} КБ · клиент {label}",
                        "client": label,
                    }
                last_note = f"{label}: файл не появился"
            except Exception as exc:
                note = str(exc)[:70]
                if is_blocking_error(exc):
                    note = "блокировка YouTube"
                elif is_video_gone_error(exc):
                    note = "видео недоступно"
                last_note = f"{label}: {note}"
            # чистим недокачанное перед следующей попыткой
            for fname in os.listdir(out_dir):
                try:
                    os.unlink(os.path.join(out_dir, fname))
                except OSError:
                    pass
        return {"ok": False, "note": last_note[:120], "client": ""}
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
