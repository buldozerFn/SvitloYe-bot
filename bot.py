#!/usr/bin/env python3
"""
Telegram-бот для відстеження графіків відключень електроенергії.
Київська область (ДТЕК), Група 6.2

Запуск:  python bot.py
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ═══════════════════════════════════════════════════════════════
#  КОНФІГУРАЦІЯ  —  ВСТАВТЕ СВОЇ ЗНАЧЕННЯ
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN: str = "8605911372:AAEkziSN0cDeyofceD6wWwKNbuSn_bI9A30"
# Список ID користувачів, яким дозволено користуватися ботом
# (Можна вказувати декілька через кому)
ALLOWED_USERS: List[int] = [5904811454, 311498924]

# ═══════════════════════════════════════════════════════════════
#  КОНСТАНТИ
# ═══════════════════════════════════════════════════════════════
DATA_URL = (
    "https://raw.githubusercontent.com/"
    "Baskerville42/outage-data-ua/main/data/kyiv-region.json"
)
TARGET_GROUP = "6.2"

# Гнучка робота з часовим поясом (автоматично враховує літній/зимовий час)
import zoneinfo
KYIV_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
FETCH_INTERVAL_MIN = 20                            # хвилин між запитами
PRE_ALERT_MINUTES = 10                             # попередження до зміни

# ═══════════════════════════════════════════════════════════════
#  ЛОГУВАННЯ
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("outage_bot")


# ═══════════════════════════════════════════════════════════════
#  ГЛОБАЛЬНИЙ СТАН ДОДАТКУ
# ═══════════════════════════════════════════════════════════════
class AppState:
    """Мутабельний стан, що розділяється між задачами."""

    def __init__(self) -> None:
        self.schedule: Dict[str, List[int]] = {}       # {"2025-06-15": [0,1,2,…×24]}
        self.data_hash: str = ""
        self.last_fetch: Optional[datetime] = None
        self.published_tomorrow: Set[str] = set()      # дати, для яких вже надіслано розклад
        self.notified_events: Set[str] = set()          # ключі надісланих сповіщень


state = AppState()

# ═══════════════════════════════════════════════════════════════
#  AIOGRAM  +  APSCHEDULER
# ═══════════════════════════════════════════════════════════════
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router(name="main")

# Фільтр: дозволяємо команди лише користувачам із білого списку
@router.message.filter
async def check_allowed(msg: types.Message) -> bool:
    return msg.from_user.id in ALLOWED_USERS if msg.from_user else False

dp.include_router(router)
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

# ═══════════════════════════════════════════════════════════════
#  ЗОНИ  (0 = біла / 1 = сіра / 2 = чорна)
# ═══════════════════════════════════════════════════════════════
ZONE_META = {
    0: ("⬜", "Світло є"),
    1: ("🟧", "Можливе відключення"),
    2: ("⬛", "Відключення"),
}


def zone_emoji(v: int) -> str:
    return ZONE_META.get(v, ("❓", "Невідомо"))[0]


def zone_label(v: int) -> str:
    return ZONE_META.get(v, ("❓", "Невідомо"))[1]


# ═══════════════════════════════════════════════════════════════
#  ЗАВАНТАЖЕННЯ  ТА  ПАРСИНГ  JSON
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
#  ЗАВАНТАЖЕННЯ  ТА  ПАРСИНГ  JSON
# ═══════════════════════════════════════════════════════════════
async def fetch_raw_json() -> Optional[dict]:
    """Завантажити JSON із GitHub. Повертає ``None`` при помилці."""
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        # Додаємо хедер, щоб уникнути кешування на рівні ISP/Proxy
        headers = {"Cache-Control": "no-cache"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(DATA_URL) as resp:
                if resp.status != 200:
                    log.error("HTTP %d при завантаженні даних", resp.status)
                    return None
                return json.loads(await resp.text())
    except Exception as exc:
        log.error("Помилка при завантаженні: %s", exc)
    return None


def _to_states(v: Any) -> List[int]:
    """
    Привести значення до списку з ДВОХ станів (по 30 хв кожен).
    0 = Біла (Світло є), 1 = Сіра (Можливо), 2 = Чорна (Немає)
    """
    if v is None:
        return [0, 0]
    val = str(v).lower().strip()
    
    # Словник відображення (на основі репозиторію)
    m = {
        "yes": [0, 0], "0": [0, 0], "white": [0, 0], "on": [0, 0],
        "no": [2, 2], "2": [2, 2], "black": [2, 2], "off": [2, 2],
        "maybe": [1, 1], "1": [1, 1], "gray": [1, 1], "grey": [1, 1],
        "first": [2, 0],    # Відключення перші 30 хв
        "second": [0, 2],   # Відключення другі 30 хв
        "mfirst": [1, 0],   # Можливе перші 30 хв
        "msecond": [0, 1],  # Можливе другі 30 хв
    }
    return m.get(val, [0, 0])


def _extract_hours(v: Any) -> Optional[List[int]]:
    """Витягти список із 48 станів (кожні 30 хв)."""
    if isinstance(v, dict):
        res = []
        for h in range(24):
            # Шукаємо 1-24 або 0-23
            val = v.get(str(h + 1), v.get(str(h), v.get(h + 1, v.get(h))))
            if val is None:
                break
            res.extend(_to_states(val))
        if len(res) == 48:
            return res
    return None


def _normalize_date(key: str) -> Optional[str]:
    """Привести рядок-дату до ``YYYY-MM-DD``."""
    key = key.strip()
    now_kyiv = datetime.now(KYIV_TZ)
    low = key.lower()
    if low in ("today", "сьогодні"):
        return now_kyiv.strftime("%Y-%m-%d")
    if low in ("tomorrow", "завтра"):
        return (now_kyiv + timedelta(days=1)).strftime("%Y-%m-%d")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(key, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    if len(key) == 10 and key[4] == "-":
        return key
    return None


def parse_group_schedule(raw: dict) -> Dict[str, List[int]]:
    """Парсинг нового формату JSON з GitHub."""
    result: Dict[str, List[int]] = {}
    
    # Спробуємо знайти дані в raw["fact"]["data"]
    data = raw.get("fact", {}).get("data", {})
    if not data and isinstance(raw.get("data"), dict):
        data = raw["data"]
        
    if not data:
        log.warning("Дані (data) не знайдено в JSON")
        return {}

    # Можливі префікси груп
    aliases = [TARGET_GROUP, f"group_{TARGET_GROUP}", f"GPV{TARGET_GROUP}"]
    
    for key, content in data.items():
        # Визначаємо дату
        date_str = None
        if key.isdigit() and len(key) >= 10:
            # Це Unix timestamp
            dt = datetime.fromtimestamp(int(key), tz=KYIV_TZ)
            date_str = dt.strftime("%Y-%m-%d")
        else:
            # Спробуємо нормалізувати як рядок
            date_str = _normalize_date(key)
            
        if not date_str or not isinstance(content, dict):
            continue
            
        # Шукаємо потрібну групу
        for a in aliases:
            if a in content:
                hours = _extract_hours(content[a])
                if hours:
                    result[date_str] = hours
                    break
                    
    if result:
        log.info("Оброблено %d дат: %s", len(result), ", ".join(sorted(result.keys())))
    return result


# ═══════════════════════════════════════════════════════════════
#  ФОРМАТУВАННЯ ПОВІДОМЛЕНЬ
# ═══════════════════════════════════════════════════════════════
_MONTHS_UA = [
    "", "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]
_DAYS_UA = [
    "Понеділок", "Вівторок", "Середа",
    "Четвер", "П'ятниця", "Субота", "Неділя",
]


def _ua_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{_DAYS_UA[dt.weekday()]}, {dt.day} {_MONTHS_UA[dt.month]} {dt.year}"
    except ValueError:
        return date_str


def format_day_schedule(date_str: str, slots: List[int]) -> str:
    """Відформатувати добовий графік (48 слотів по 30 хв)."""
    lines = [
        f"📅 <b>Графік відключень — Група {TARGET_GROUP}</b>",
        "📍 Київська область (ДТЕК)",
        f"🗓 {_ua_date(date_str)}",
        "",
        "⏰ <b>Погодинний розклад:</b>",
        "",
    ]
    
    def get_time(idx: int) -> str:
        h, m = divmod(idx, 2)
        return f"{h:02d}:{m*30:02d}"

    i = 0
    total_slots = len(slots)  # Має бути 48
    while i < total_slots:
        s = slots[i]
        j = i + 1
        while j < total_slots and slots[j] == s:
            j += 1
        
        start_t = get_time(i)
        end_t = "00:00" if j == total_slots else get_time(j)
        
        lines.append(
            f"  {zone_emoji(s)}  {start_t} – {end_t}  —  {zone_label(s)}"
        )
        i = j

    on_h = slots.count(0) / 2
    gray_h = slots.count(1) / 2
    black_h = slots.count(2) / 2
    
    lines += [
        "",
        "📊 <b>Підсумок:</b>",
        f"  ⬜ Світло є: {on_h:g} год",
        f"  🟧 Можливо: {gray_h:g} год",
        f"  ⬛ Відключення: {black_h:g} год",
        "",
        "<i>⬜ біла зона — світло є\n"
        "🟧 сіра зона — можливе відключення\n"
        "⬛ чорна зона — відключення заплановано</i>",
    ]
    return "\n".join(lines)


def format_status_change(slot_idx: int, new_state: int,
                         next_change: Optional[Tuple[int, int]] = None) -> str:
    headers = {
        0: "💡 Світло має з'явитися!",
        1: "⚠️ Увага: можливе відключення!",
        2: "🔌 Увага: заплановане відключення!",
    }
    
    h, m = divmod(slot_idx, 2)
    time_str = f"{h:02d}:{m*30:02d}"
    
    lines = [
        f"<b>{headers.get(new_state, '❓ Зміна стану')}</b>",
        "",
        f"🏘 Група {TARGET_GROUP} (Київська обл., ДТЕК)",
        f"🕐 З {time_str} — {zone_emoji(new_state)} {zone_label(new_state)}",
    ]
    if next_change:
        ns_idx, ns = next_change
        nh, nm = divmod(ns_idx, 2)
        lines.append(
            f"➡️ Наступна зміна о {nh:02d}:{nm*30:02d} — "
            f"{zone_emoji(ns)} {zone_label(ns)}"
        )
    return "\n".join(lines)


def format_pre_alert(slot_idx: int, upcoming_state: int, minutes: int) -> str:
    if upcoming_state == 2:
        h = f"⏳ Через ~{minutes} хв — заплановане відключення!"
    elif upcoming_state == 1:
        h = f"⏳ Через ~{minutes} хв — можливе відключення!"
    else:
        h = f"⏳ Через ~{minutes} хв — світло має з'явитися!"
        
    hour, min = divmod(slot_idx, 2)
    return (
        f"<b>{h}</b>\n\n"
        f"🏘 Група {TARGET_GROUP} (Київська обл., ДТЕК)\n"
        f"🕐 О {hour:02d}:{min*30:02d} — {zone_emoji(upcoming_state)} {zone_label(upcoming_state)}"
    )


# ═══════════════════════════════════════════════════════════════
#  БЕЗПЕЧНА ВІДПРАВКА
# ═══════════════════════════════════════════════════════════════
async def send_safe(text: str) -> None:
    """Надіслати повідомлення всім дозволеним користувачам."""
    for user_id in ALLOWED_USERS:
        try:
            await bot.send_message(chat_id=user_id, text=text)
            log.info("✉️  Повідомлення надіслано користувачу %s", user_id)
        except Exception as exc:
            log.error("Не вдалося надіслати повідомлення %s: %s", user_id, exc)


# ═══════════════════════════════════════════════════════════════
#  ФОНОВІ ЗАДАЧІ
# ═══════════════════════════════════════════════════════════════
async def task_fetch_data() -> None:
    """Періодичне завантаження та кешування даних."""
    log.info("🔄 Завантаження даних з GitHub…")
    raw = await fetch_raw_json()
    if raw is None:
        log.warning("Дані не отримано — спробуємо пізніше")
        return

    raw_hash = hashlib.md5(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    data_changed = raw_hash != state.data_hash
    state.data_hash = raw_hash
    state.last_fetch = datetime.now(KYIV_TZ)

    new_sched = parse_group_schedule(raw)
    if not new_sched:
        log.warning("Розклад для групи %s не знайдено", TARGET_GROUP)
        return

    state.schedule = new_sched
    log.info("✅ Кеш оновлено (%d дат)", len(new_sched))

    if data_changed:
        await _maybe_publish_tomorrow()


async def _maybe_publish_tomorrow() -> None:
    """Якщо з'явився розклад на завтра — надіслати."""
    tomorrow = (datetime.now(KYIV_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    if tomorrow in state.schedule and tomorrow not in state.published_tomorrow:
        log.info("📅 Новий розклад на завтра (%s) — публікую", tomorrow)
        msg = (
            "🆕 <b>Опубліковано розклад на завтра!</b>\n\n"
            + format_day_schedule(tomorrow, state.schedule[tomorrow])
        )
        await send_safe(msg)
        state.published_tomorrow.add(tomorrow)


async def task_check_alerts() -> None:
    """Щомінутна перевірка: чи є зміна стану або наближається зміна."""
    now = datetime.now(KYIV_TZ)
    today_str = now.strftime("%Y-%m-%d")
    if today_str not in state.schedule:
        return

    slots = state.schedule[today_str]
    # Розраховуємо індекс поточного 30-хвилинного слоту (0-47)
    cur_slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
    cur_m_in_slot = now.minute % 30
    cur_state = slots[cur_slot]

    # ── 1. Попередження за PRE_ALERT_MINUTES до зміни ──
    # Перевіряємо, чи наступний слот має інший стан
    if cur_m_in_slot >= (30 - PRE_ALERT_MINUTES):
        next_slot = (cur_slot + 1) % 48
        if next_slot == 0:
            tom_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            next_state = state.schedule.get(tom_str, slots)[0]
        else:
            next_state = slots[next_slot]

        if next_state != cur_state:
            key = f"pre|{today_str}|{next_slot}"
            if key not in state.notified_events:
                await send_safe(
                    format_pre_alert(next_slot, next_state, 30 - cur_m_in_slot)
                )
                state.notified_events.add(key)

    # ── 2. Сповіщення при фактичній зміні стану ──
    key_change = f"chg|{today_str}|{cur_slot}"
    if key_change not in state.notified_events:
        if cur_slot == 0:
            yest = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            prev_state = (
                state.schedule[yest][47]
                if yest in state.schedule
                else None
            )
        else:
            prev_state = slots[cur_slot - 1]

        if prev_state is not None and prev_state != cur_state:
            # знайти наступну зміну
            nxt: Optional[Tuple[int, int]] = None
            for f_idx in range(cur_slot + 1, 48):
                if slots[f_idx] != cur_state:
                    nxt = (f_idx, slots[f_idx])
                    break
            await send_safe(format_status_change(cur_slot, cur_state, nxt))
            state.notified_events.add(key_change)

    # ── 3. Очищення старих ключів (старше 2 днів) ──
    cutoff = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    state.notified_events = {
        e for e in state.notified_events
        if _event_date(e) >= cutoff
    }
    state.published_tomorrow = {
        d for d in state.published_tomorrow if d >= cutoff
    }


def _event_date(event_key: str) -> str:
    """Дістати дату з ключа ``type|YYYY-MM-DD|hour``."""
    parts = event_key.split("|")
    return parts[1] if len(parts) >= 2 else "9999-99-99"


# ═══════════════════════════════════════════════════════════════
#  КОМАНДИ БОТА
# ═══════════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(msg: types.Message) -> None:
    await msg.answer(
        "👋 <b>Вітаю!</b>\n\n"
        f"Я бот для відстеження графіків відключень "
        f"електроенергії.\n"
        f"📍 <b>Київська область (ДТЕК), Група {TARGET_GROUP}</b>\n\n"
        "📋 <b>Доступні команди:</b>\n"
        "/today — графік на сьогодні\n"
        "/tomorrow — графік на завтра\n"
        "/now — поточний статус\n"
        "/status — діагностика бота\n\n"
        "🔔 <b>Автоматичні сповіщення:</b>\n"
        "• Розклад на завтра (щойно з'явиться)\n"
        f"• Попередження за {PRE_ALERT_MINUTES} хв до зміни\n"
        "• Повідомлення при зміні стану"
    )


@router.message(Command("today"))
async def cmd_today(msg: types.Message) -> None:
    d = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
    if d in state.schedule:
        await msg.answer(format_day_schedule(d, state.schedule[d]))
    else:
        await msg.answer(
            "😔 Розклад на сьогодні ще недоступний.\n"
            "Спробуйте пізніше або перевірте /status."
        )


@router.message(Command("tomorrow"))
async def cmd_tomorrow(msg: types.Message) -> None:
    d = (datetime.now(KYIV_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    if d in state.schedule:
        await msg.answer(format_day_schedule(d, state.schedule[d]))
    else:
        await msg.answer(
            "😔 Розклад на завтра ще не опубліковано.\n"
            "Зазвичай він з'являється ввечері. "
            "Бот надішле його автоматично!"
        )


@router.message(Command("now"))
async def cmd_now(msg: types.Message) -> None:
    now = datetime.now(KYIV_TZ)
    d = now.strftime("%Y-%m-%d")
    if d not in state.schedule:
        await msg.answer("😔 Розклад на сьогодні недоступний.")
        return

    slots = state.schedule[d]
    # Поточний індекс 30-хв слоту
    cur_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
    cur = slots[cur_idx]

    def get_time(idx: int) -> str:
        h, m = divmod(idx, 2)
        return f"{h:02d}:{m*30:02d}"

    lines = [
        f"🕐 <b>Зараз {now.strftime('%H:%M')}</b>",
        f"🏘 Група {TARGET_GROUP} (Київська обл., ДТЕК)",
        "",
        f"Поточний статус: {zone_emoji(cur)} <b>{zone_label(cur)}</b>",
        "",
        "<b>Найближчий розклад:</b>",
    ]
    
    # Виводимо наступні 8 слотів (4 години)
    for i in range(cur_idx, min(cur_idx + 10, 48)):
        ptr = " 👉 " if i == cur_idx else "       "
        start_t = get_time(i)
        end_t = get_time(i + 1) if i + 1 < 48 else "00:00"
        lines.append(
            f"{ptr}{zone_emoji(slots[i])}  {start_t}–{end_t}  —  {zone_label(slots[i])}"
        )

    for i in range(cur_idx + 1, 48):
        if slots[i] != cur:
            nh, nm = divmod(i, 2)
            lines.append(
                f"\n➡️ Наступна зміна о <b>{nh:02d}:{nm*30:02d}</b> — "
                f"{zone_emoji(slots[i])} {zone_label(slots[i])}"
            )
            break

    await msg.answer("\n".join(lines))


@router.message(Command("status"))
async def cmd_status(msg: types.Message) -> None:
    now = datetime.now(KYIV_TZ)
    lf = (
        state.last_fetch.strftime("%H:%M:%S")
        if state.last_fetch
        else "ще не було"
    )
    dates = ", ".join(sorted(state.schedule)) if state.schedule else "немає"
    await msg.answer(
        "🤖 <b>Діагностика бота</b>\n\n"
        f"⏰ Час (Kyiv): {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📡 Останнє оновлення: {lf}\n"
        f"📅 Дати в кеші: {dates}\n"
        f"🔔 Надіслано сповіщень: {len(state.notified_events)}\n"
        f"🔄 Інтервал: кожні {FETCH_INTERVAL_MIN} хв\n"
        f"🎯 Група: {TARGET_GROUP}"
    )


# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК  /  ЗУПИНКА
# ═══════════════════════════════════════════════════════════════
async def on_startup() -> None:
    log.info("🚀 Бот запускається…")

    # перше завантаження
    await task_fetch_data()

    # планувальник
    scheduler.add_job(
        task_fetch_data,
        IntervalTrigger(minutes=FETCH_INTERVAL_MIN),
        id="fetch",
        replace_existing=True,
    )
    scheduler.add_job(
        task_check_alerts,
        IntervalTrigger(minutes=1),
        id="alerts",
        replace_existing=True,
    )
    scheduler.start()

    await send_safe(
        "🤖 <b>Бот запущено!</b>\n"
        f"📍 Група {TARGET_GROUP}, Київська обл. (ДТЕК)\n"
        f"🔄 Оновлення кожні {FETCH_INTERVAL_MIN} хв\n"
        "Надішліть /start для списку команд."
    )
    log.info("✅ Бот успішно запущено")


async def on_shutdown() -> None:
    log.info("🛑 Бот зупиняється…")
    scheduler.shutdown(wait=False)
    await bot.session.close()


async def main() -> None:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
