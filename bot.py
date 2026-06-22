#!/usr/bin/env python3
"""
Telegram bot for tracking power outage schedules in Ukraine (DTEK).

Usage:  python bot.py
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION — FILL IN YOUR VALUES
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN: str = "8605911372:AAEkziSN0cDeyofceD6wWwKNbuSn_bI9A30"

# Admin user IDs that receive startup messages and scheduled alerts.
# Regular users who set up their group also receive alerts for their group.
ADMIN_USERS: List[int] = [5904811454]

# ═══════════════════════════════════════════════════════════════
#  REGION & GROUP DEFINITIONS
# ═══════════════════════════════════════════════════════════════
# Mapping of region_id → (display name, raw JSON URL)
REGIONS: Dict[str, Tuple[str, str]] = {
    "kyiv-region": (
        "🏙 Київська область",
        "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json",
    ),
    "kyiv": (
        "🏛 Місто Київ",
        "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv.json",
    ),
    "dnipro": (
        "🏭 Дніпропетровська обл.",
        "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/dnipro.json",
    ),
    "odesa": (
        "⚓ Одеська область",
        "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/odesa.json",
    ),
    "zaporizhzhia": (
        "⚡ Запорізька область",
        "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/zaporizhzhia.json",
    ),
    "donetsk": (
        "🔩 Донецька область",
        "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/donetsk.json",
    ),
}

# Available groups (DTEK standard schedule groups)
ALL_GROUPS: List[str] = [
    "1.1", "1.2",
    "2.1", "2.2",
    "3.1", "3.2",
    "4.1", "4.2",
    "5.1", "5.2",
    "6.1", "6.2",
]

# ═══════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════
import zoneinfo
KYIV_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
FETCH_INTERVAL_MIN = 20      # minutes between data fetches
PRE_ALERT_MINUTES = 10       # minutes before a state change to send a pre-alert

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("outage_bot")


# ═══════════════════════════════════════════════════════════════
#  USER PREFERENCES  (in-memory, per user)
# ═══════════════════════════════════════════════════════════════
class UserPrefs:
    """Stores the selected region and group for one Telegram user."""

    def __init__(self, region_id: str, group: str) -> None:
        self.region_id = region_id   # key from REGIONS dict
        self.group = group           # e.g. "6.2"


# user_id → UserPrefs
user_prefs: Dict[int, UserPrefs] = {}


# ═══════════════════════════════════════════════════════════════
#  GLOBAL APPLICATION STATE
# ═══════════════════════════════════════════════════════════════
class AppState:
    """Mutable state shared between background tasks."""

    def __init__(self) -> None:
        # Nested schedule cache: region_id → date_str → list of 48 half-hour slots
        self.schedules: Dict[str, Dict[str, List[int]]] = {}
        # MD5 hash of last fetched raw JSON per region (to detect changes)
        self.data_hashes: Dict[str, str] = {}
        self.last_fetch: Optional[datetime] = None
        # Dates for which the "tomorrow schedule" notification was already sent per user
        self.published_tomorrow: Dict[int, Set[str]] = {}
        # Keys of already-sent event notifications per user
        self.notified_events: Dict[int, Set[str]] = {}


state = AppState()


# ═══════════════════════════════════════════════════════════════
#  AIOGRAM + APSCHEDULER SETUP
# ═══════════════════════════════════════════════════════════════
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router(name="main")

# No user filter — all Telegram users are allowed to use the bot
dp.include_router(router)
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")


# ═══════════════════════════════════════════════════════════════
#  ZONE METADATA  (0 = white / 1 = grey / 2 = black)
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
#  DATA FETCHING AND PARSING
# ═══════════════════════════════════════════════════════════════
async def fetch_raw_json(url: str) -> Optional[dict]:
    """Download JSON from the given URL. Returns None on error."""
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        # Cache-Control header prevents ISP/proxy caching
        headers = {"Cache-Control": "no-cache"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.error("HTTP %d when fetching %s", resp.status, url)
                    return None
                return json.loads(await resp.text())
    except Exception as exc:
        log.error("Error fetching %s: %s", url, exc)
    return None


def _to_states(v: Any) -> List[int]:
    """
    Convert a raw JSON value to a list of TWO states (30 min each).
    0 = White (power on), 1 = Grey (possible outage), 2 = Black (outage)
    """
    if v is None:
        return [0, 0]
    val = str(v).lower().strip()

    # Mapping table based on the data repository spec
    m = {
        "yes": [0, 0], "0": [0, 0], "white": [0, 0], "on": [0, 0],
        "no": [2, 2], "2": [2, 2], "black": [2, 2], "off": [2, 2],
        "maybe": [1, 1], "1": [1, 1], "gray": [1, 1], "grey": [1, 1],
        "first": [2, 0],    # outage in the first 30 min
        "second": [0, 2],   # outage in the second 30 min
        "mfirst": [1, 0],   # possible outage in the first 30 min
        "msecond": [0, 1],  # possible outage in the second 30 min
    }
    return m.get(val, [0, 0])


def _extract_hours(v: Any) -> Optional[List[int]]:
    """Extract a list of 48 states (one per 30-minute slot) from a dict."""
    if isinstance(v, dict):
        res = []
        for h in range(24):
            # Try keys 1-24 first, fall back to 0-23
            val = v.get(str(h + 1), v.get(str(h), v.get(h + 1, v.get(h))))
            if val is None:
                break
            res.extend(_to_states(val))
        if len(res) == 48:
            return res
    return None


def _normalize_date(key: str) -> Optional[str]:
    """Normalise a date string to YYYY-MM-DD format."""
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


def parse_group_schedule(raw: dict, group: str) -> Dict[str, List[int]]:
    """
    Parse the GitHub JSON for a specific group (e.g. '6.2').
    Returns a dict mapping date strings to 48-slot state lists.
    """
    result: Dict[str, List[int]] = {}

    # Try to find data under raw["fact"]["data"] or raw["data"]
    data = raw.get("fact", {}).get("data", {})
    if not data and isinstance(raw.get("data"), dict):
        data = raw["data"]

    if not data:
        log.warning("No 'data' key found in JSON")
        return {}

    # Build alternative group key aliases
    aliases = [group, f"group_{group}", f"GPV{group}"]

    for key, content in data.items():
        # Resolve the date string
        date_str: Optional[str] = None
        if key.isdigit() and len(key) >= 10:
            # Unix timestamp
            dt = datetime.fromtimestamp(int(key), tz=KYIV_TZ)
            date_str = dt.strftime("%Y-%m-%d")
        else:
            date_str = _normalize_date(key)

        if not date_str or not isinstance(content, dict):
            continue

        # Search for the requested group under any alias
        for alias in aliases:
            if alias in content:
                hours = _extract_hours(content[alias])
                if hours:
                    result[date_str] = hours
                    break

    if result:
        log.info(
            "Parsed %d dates for group %s: %s",
            len(result), group, ", ".join(sorted(result.keys()))
        )
    return result


# ═══════════════════════════════════════════════════════════════
#  MESSAGE FORMATTING
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
    """Convert YYYY-MM-DD to a human-readable Ukrainian date string."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{_DAYS_UA[dt.weekday()]}, {dt.day} {_MONTHS_UA[dt.month]} {dt.year}"
    except ValueError:
        return date_str


def format_day_schedule(date_str: str, slots: List[int], region_name: str, group: str) -> str:
    """Format a full-day schedule (48 half-hour slots) as an HTML message."""
    lines = [
        f"📅 <b>Графік відключень — Група {group}</b>",
        f"📍 {region_name}",
        f"🗓 {_ua_date(date_str)}",
        "",
        "⏰ <b>Погодинний розклад:</b>",
        "",
    ]

    def get_time(idx: int) -> str:
        h, m = divmod(idx, 2)
        return f"{h:02d}:{m*30:02d}"

    i = 0
    total_slots = len(slots)  # Should be 48
    while i < total_slots:
        s = slots[i]
        j = i + 1
        # Merge consecutive slots with the same state into one range
        while j < total_slots and slots[j] == s:
            j += 1

        start_t = get_time(i)
        end_t = "00:00" if j == total_slots else get_time(j)
        lines.append(f"  {zone_emoji(s)}  {start_t} – {end_t}  —  {zone_label(s)}")
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


def format_status_change(
    slot_idx: int,
    new_state: int,
    region_name: str,
    group: str,
    next_change: Optional[Tuple[int, int]] = None,
) -> str:
    """Format a state-change notification message."""
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
        f"🏘 Група {group} ({region_name})",
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


def format_pre_alert(
    slot_idx: int,
    upcoming_state: int,
    minutes: int,
    region_name: str,
    group: str,
) -> str:
    """Format a pre-alert notification (sent N minutes before a state change)."""
    if upcoming_state == 2:
        h = f"⏳ Через ~{minutes} хв — заплановане відключення!"
    elif upcoming_state == 1:
        h = f"⏳ Через ~{minutes} хв — можливе відключення!"
    else:
        h = f"⏳ Через ~{minutes} хв — світло має з'явитися!"

    hour, min_ = divmod(slot_idx, 2)
    return (
        f"<b>{h}</b>\n\n"
        f"🏘 Група {group} ({region_name})\n"
        f"🕐 О {hour:02d}:{min_*30:02d} — {zone_emoji(upcoming_state)} {zone_label(upcoming_state)}"
    )


# ═══════════════════════════════════════════════════════════════
#  SAFE SEND HELPERS
# ═══════════════════════════════════════════════════════════════
async def send_to_user(user_id: int, text: str) -> None:
    """Send a message to a single user, logging any errors."""
    try:
        await bot.send_message(chat_id=user_id, text=text)
        log.info("✉️  Message sent to user %s", user_id)
    except Exception as exc:
        log.error("Failed to send message to %s: %s", user_id, exc)


async def send_to_admins(text: str) -> None:
    """Send a message to all admin users."""
    for uid in ADMIN_USERS:
        await send_to_user(uid, text)


# ═══════════════════════════════════════════════════════════════
#  INLINE KEYBOARD BUILDERS
# ═══════════════════════════════════════════════════════════════
def build_region_keyboard() -> InlineKeyboardMarkup:
    """Build an inline keyboard with one button per available region."""
    buttons = []
    for region_id, (display_name, _) in REGIONS.items():
        buttons.append(
            [InlineKeyboardButton(
                text=display_name,
                callback_data=f"region:{region_id}",
            )]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_group_keyboard(region_id: str) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard with all groups in a 2-column grid,
    plus a 'back' button to return to region selection.
    """
    # Arrange groups in rows of 2
    rows = []
    for i in range(0, len(ALL_GROUPS), 2):
        pair = ALL_GROUPS[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Група {g}",
                    callback_data=f"group:{region_id}:{g}",
                )
                for g in pair
            ]
        )
    # Back button
    rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:regions")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════
async def task_fetch_data() -> None:
    """Periodically download and cache outage data for all configured regions."""
    log.info("🔄 Fetching data from GitHub…")

    # Collect all region_ids that are actually needed
    needed_regions: Set[str] = set(
        prefs.region_id for prefs in user_prefs.values()
    )
    # Always keep the kyiv-region in cache (for admins / default)
    needed_regions.add("kyiv-region")

    for region_id in needed_regions:
        if region_id not in REGIONS:
            continue

        _, url = REGIONS[region_id]
        raw = await fetch_raw_json(url)
        if raw is None:
            log.warning("No data received for region '%s'", region_id)
            continue

        raw_hash = hashlib.md5(
            json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

        data_changed = raw_hash != state.data_hashes.get(region_id, "")
        state.data_hashes[region_id] = raw_hash
        state.last_fetch = datetime.now(KYIV_TZ)

        # Re-parse for every unique group used in this region
        groups_for_region: Set[str] = {
            prefs.group
            for prefs in user_prefs.values()
            if prefs.region_id == region_id
        }
        # Always cache admin default group as well (6.2 for kyiv-region)
        if region_id == "kyiv-region":
            groups_for_region.add("6.2")

        if region_id not in state.schedules:
            state.schedules[region_id] = {}

        for group in groups_for_region:
            sched = parse_group_schedule(raw, group)
            # Merge into the region cache using a compound key
            for date_str, slots in sched.items():
                state.schedules[region_id][f"{group}|{date_str}"] = slots

        log.info("✅ Cache updated for region '%s'", region_id)

        if data_changed:
            await _maybe_publish_tomorrow(region_id)


def _get_slots(region_id: str, group: str, date_str: str) -> Optional[List[int]]:
    """Retrieve cached 48-slot list for a given region, group and date."""
    region_cache = state.schedules.get(region_id, {})
    return region_cache.get(f"{group}|{date_str}")


async def _maybe_publish_tomorrow(region_id: str) -> None:
    """If a new tomorrow schedule became available, notify relevant users."""
    tomorrow = (datetime.now(KYIV_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    # Notify every user whose region matches and who hasn't been notified yet
    for uid, prefs in user_prefs.items():
        if prefs.region_id != region_id:
            continue
        slots = _get_slots(region_id, prefs.group, tomorrow)
        if slots is None:
            continue

        published = state.published_tomorrow.setdefault(uid, set())
        if tomorrow in published:
            continue

        region_name = REGIONS[region_id][0]
        log.info(
            "📅 New tomorrow schedule (%s) for user %s — publishing", tomorrow, uid
        )
        msg = (
            "🆕 <b>Опубліковано розклад на завтра!</b>\n\n"
            + format_day_schedule(tomorrow, slots, region_name, prefs.group)
        )
        await send_to_user(uid, msg)
        published.add(tomorrow)


async def task_check_alerts() -> None:
    """
    Every-minute check: send pre-alerts and state-change notifications
    to all users who have configured their group preferences.
    """
    now = datetime.now(KYIV_TZ)
    today_str = now.strftime("%Y-%m-%d")

    for uid, prefs in user_prefs.items():
        slots = _get_slots(prefs.region_id, prefs.group, today_str)
        if slots is None:
            continue

        region_name = REGIONS.get(prefs.region_id, ("?", ""))[0]
        # Current 30-minute slot index (0-47)
        cur_slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
        cur_m_in_slot = now.minute % 30
        cur_state = slots[cur_slot]

        notified = state.notified_events.setdefault(uid, set())

        # ── 1. Pre-alert: warn N minutes before a state change ──
        if cur_m_in_slot >= (30 - PRE_ALERT_MINUTES):
            next_slot = (cur_slot + 1) % 48
            if next_slot == 0:
                tom_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                tom_slots = _get_slots(prefs.region_id, prefs.group, tom_str)
                next_state = tom_slots[0] if tom_slots else slots[0]
            else:
                next_state = slots[next_slot]

            if next_state != cur_state:
                key = f"pre|{today_str}|{next_slot}"
                if key not in notified:
                    await send_to_user(
                        uid,
                        format_pre_alert(
                            next_slot, next_state,
                            30 - cur_m_in_slot,
                            region_name, prefs.group,
                        ),
                    )
                    notified.add(key)

        # ── 2. State-change notification ──
        key_change = f"chg|{today_str}|{cur_slot}"
        if key_change not in notified:
            if cur_slot == 0:
                yest = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                yest_slots = _get_slots(prefs.region_id, prefs.group, yest)
                prev_state = yest_slots[47] if yest_slots else None
            else:
                prev_state = slots[cur_slot - 1]

            if prev_state is not None and prev_state != cur_state:
                # Find the next upcoming state change
                nxt: Optional[Tuple[int, int]] = None
                for f_idx in range(cur_slot + 1, 48):
                    if slots[f_idx] != cur_state:
                        nxt = (f_idx, slots[f_idx])
                        break
                await send_to_user(
                    uid,
                    format_status_change(
                        cur_slot, cur_state, region_name, prefs.group, nxt
                    ),
                )
                notified.add(key_change)

        # ── 3. Prune notification keys older than 2 days ──
        cutoff = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        state.notified_events[uid] = {
            e for e in notified if _event_date(e) >= cutoff
        }
        published = state.published_tomorrow.get(uid, set())
        state.published_tomorrow[uid] = {d for d in published if d >= cutoff}


def _event_date(event_key: str) -> str:
    """Extract the date portion from a notification key (type|YYYY-MM-DD|slot)."""
    parts = event_key.split("|")
    return parts[1] if len(parts) >= 2 else "9999-99-99"


# ═══════════════════════════════════════════════════════════════
#  BOT COMMANDS
# ═══════════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(msg: types.Message) -> None:
    """Welcome message with group setup button."""
    uid = msg.from_user.id if msg.from_user else None
    prefs = user_prefs.get(uid) if uid else None

    if prefs:
        region_name = REGIONS.get(prefs.region_id, ("невідомий", ""))[0]
        config_line = f"⚙️ Ваша група: <b>Група {prefs.group}</b> ({region_name})"
    else:
        config_line = "⚙️ Натисніть /setgroup, щоб обрати ваш регіон та групу."

    # Build the command list
    commands = (
        "/setgroup — обрати регіон та групу\n"
        "/today — графік на сьогодні\n"
        "/tomorrow — графік на завтра\n"
        "/now — поточний статус"
    )

    await msg.answer(
        "👋 <b>Вітаю!</b>\n\n"
        "Я бот для відстеження графіків відключень електроенергії в Україні (ДТЕК).\n\n"
        f"{config_line}\n\n"
        f"📋 <b>Доступні команди:</b>\n"
        f"{commands}\n\n"
        "🔔 <b>Автоматичні сповіщення:</b>\n"
        "• Розклад на завтра (щойно з'явиться)\n"
        f"• Попередження за {PRE_ALERT_MINUTES} хв до зміни\n"
        "• Повідомлення при зміні стану"
    )


@router.message(Command("setgroup"))
async def cmd_setgroup(msg: types.Message) -> None:
    """Show the region selection keyboard."""
    await msg.answer(
        "🗺 <b>Оберіть ваш регіон:</b>\n\n"
        "Дізнатися свою групу можна на офіційному сайті ДТЕК (dtek.com).",
        reply_markup=build_region_keyboard(),
    )


@router.callback_query(F.data.startswith("region:"))
async def cb_region_selected(cb: CallbackQuery) -> None:
    """Handle region selection — show the group keyboard for that region."""
    region_id = cb.data.split(":", 1)[1]
    if region_id not in REGIONS:
        await cb.answer("Невідомий регіон.", show_alert=True)
        return

    region_name = REGIONS[region_id][0]
    await cb.message.edit_text(
        f"✅ Регіон: <b>{region_name}</b>\n\n"
        "📋 <b>Оберіть свою групу:</b>\n\n"
        "Свою групу можна дізнатися на офіційному сайті ДТЕК (dtek.com).",
        reply_markup=build_group_keyboard(region_id),
    )
    await cb.answer()


@router.callback_query(F.data == "back:regions")
async def cb_back_to_regions(cb: CallbackQuery) -> None:
    """Handle the 'Back' button — return to region selection."""
    await cb.message.edit_text(
        "🗺 <b>Оберіть ваш регіон:</b>\n\n"
        "Дізнатися свою групу можна на офіційному сайті ДТЕК (dtek.com).",
        reply_markup=build_region_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("group:"))
async def cb_group_selected(cb: CallbackQuery) -> None:
    """Handle group selection — save preference and confirm to the user."""
    _, region_id, group = cb.data.split(":", 2)
    if region_id not in REGIONS or group not in ALL_GROUPS:
        await cb.answer("Невірні дані.", show_alert=True)
        return

    uid = cb.from_user.id
    user_prefs[uid] = UserPrefs(region_id=region_id, group=group)
    region_name = REGIONS[region_id][0]

    log.info("User %s selected region=%s group=%s", uid, region_id, group)

    await cb.message.edit_text(
        f"✅ <b>Налаштування збережено!</b>\n\n"
        f"📍 Регіон: <b>{region_name}</b>\n"
        f"🎯 Група: <b>{group}</b>\n\n"
        "Свою групу можна дізнатися на офіційному сайті ДТЕК (dtek.com).\n\n"
        "Тепер використовуйте:\n"
        "/today — графік на сьогодні\n"
        "/now — поточний статус\n"
        "/tomorrow — графік на завтра",
    )
    await cb.answer("✅ Групу збережено!")

    # Trigger immediate data fetch so the user can use commands right away
    asyncio.create_task(task_fetch_data())


@router.message(Command("today"))
async def cmd_today(msg: types.Message) -> None:
    """Send today's schedule for the user's selected group."""
    uid = msg.from_user.id if msg.from_user else None
    prefs = user_prefs.get(uid) if uid else None

    if not prefs:
        await msg.answer(
            "⚙️ Спочатку оберіть свій регіон та групу командою /setgroup."
        )
        return

    d = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
    slots = _get_slots(prefs.region_id, prefs.group, d)
    region_name = REGIONS.get(prefs.region_id, ("?", ""))[0]

    if slots:
        await msg.answer(format_day_schedule(d, slots, region_name, prefs.group))
    else:
        await msg.answer(
            "😔 Розклад на сьогодні ще недоступний.\n"
            "Спробуйте пізніше."
        )


@router.message(Command("tomorrow"))
async def cmd_tomorrow(msg: types.Message) -> None:
    """Send tomorrow's schedule for the user's selected group."""
    uid = msg.from_user.id if msg.from_user else None
    prefs = user_prefs.get(uid) if uid else None

    if not prefs:
        await msg.answer(
            "⚙️ Спочатку оберіть свій регіон та групу командою /setgroup."
        )
        return

    d = (datetime.now(KYIV_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    slots = _get_slots(prefs.region_id, prefs.group, d)
    region_name = REGIONS.get(prefs.region_id, ("?", ""))[0]

    if slots:
        await msg.answer(format_day_schedule(d, slots, region_name, prefs.group))
    else:
        await msg.answer(
            "😔 Розклад на завтра ще не опубліковано.\n"
            "Зазвичай він з'являється ввечері. "
            "Бот надішле його автоматично!"
        )


@router.message(Command("now"))
async def cmd_now(msg: types.Message) -> None:
    """Send the current outage status and nearby schedule for the user's group."""
    uid = msg.from_user.id if msg.from_user else None
    prefs = user_prefs.get(uid) if uid else None

    if not prefs:
        await msg.answer(
            "⚙️ Спочатку оберіть свій регіон та групу командою /setgroup."
        )
        return

    now = datetime.now(KYIV_TZ)
    d = now.strftime("%Y-%m-%d")
    slots = _get_slots(prefs.region_id, prefs.group, d)
    region_name = REGIONS.get(prefs.region_id, ("?", ""))[0]

    if not slots:
        await msg.answer("😔 Розклад на сьогодні недоступний.")
        return

    # Current 30-minute slot index (0-47)
    cur_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
    cur = slots[cur_idx]

    def get_time(idx: int) -> str:
        h, m = divmod(idx, 2)
        return f"{h:02d}:{m*30:02d}"

    lines = [
        f"🕐 <b>Зараз {now.strftime('%H:%M')}</b>",
        f"🏘 Група {prefs.group} ({region_name})",
        "",
        f"Поточний статус: {zone_emoji(cur)} <b>{zone_label(cur)}</b>",
        "",
        "<b>Найближчий розклад:</b>",
    ]

    # Show the next 10 half-hour slots (5 hours)
    for i in range(cur_idx, min(cur_idx + 10, 48)):
        ptr = " 👉 " if i == cur_idx else "       "
        start_t = get_time(i)
        end_t = get_time(i + 1) if i + 1 < 48 else "00:00"
        lines.append(
            f"{ptr}{zone_emoji(slots[i])}  {start_t}–{end_t}  —  {zone_label(slots[i])}"
        )

    # Show the next upcoming state change
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
    """Show bot diagnostics (admin-only)."""
    uid = msg.from_user.id if msg.from_user else None
    if uid not in ADMIN_USERS:
        return  # silently ignore non-admin users
    now = datetime.now(KYIV_TZ)
    lf = (
        state.last_fetch.strftime("%H:%M:%S")
        if state.last_fetch
        else "not yet"
    )
    uid = msg.from_user.id if msg.from_user else None
    prefs = user_prefs.get(uid) if uid else None

    group_line = (
        f"🎯 Ваша група: {prefs.group} ({REGIONS.get(prefs.region_id, ('?',''))[0]})"
        if prefs
        else "⚙️ Група не обрана — скористайтеся /setgroup"
    )

    await msg.answer(
        "🤖 <b>Діагностика бота</b>\n\n"
        f"⏰ Час (Kyiv): {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📡 Останнє оновлення: {lf}\n"
        f"👥 Активних користувачів: {len(user_prefs)}\n"
        f"🔄 Інтервал: кожні {FETCH_INTERVAL_MIN} хв\n"
        f"{group_line}"
    )


# ═══════════════════════════════════════════════════════════════
#  STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════════════
async def on_startup() -> None:
    """Initialize the scheduler, fetch initial data, and notify admins."""
    log.info("🚀 Bot starting…")

    # Initial data fetch
    await task_fetch_data()

    # Schedule periodic data fetch
    scheduler.add_job(
        task_fetch_data,
        IntervalTrigger(minutes=FETCH_INTERVAL_MIN),
        id="fetch",
        replace_existing=True,
    )
    # Schedule per-minute alert checker
    scheduler.add_job(
        task_check_alerts,
        IntervalTrigger(minutes=1),
        id="alerts",
        replace_existing=True,
    )
    scheduler.start()

    await send_to_admins(
        "🤖 <b>Бот запущено!</b>\n"
        f"🔄 Оновлення кожні {FETCH_INTERVAL_MIN} хв\n"
        "Надішліть /start для списку команд.\n"
        "Надішліть /setgroup для вибору регіону та групи."
    )
    log.info("✅ Bot started successfully")


async def on_shutdown() -> None:
    """Graceful shutdown: stop scheduler and close the bot session."""
    log.info("🛑 Bot shutting down…")
    scheduler.shutdown(wait=False)
    await bot.session.close()


async def main() -> None:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
