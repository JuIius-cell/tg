import asyncio
import glob
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from playwright.async_api import async_playwright

DEFAULT_TOKEN = ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("outage_bot")

BASE_URL = "https://energy-ua.info/cherga"
QUEUE_NUMS = [
    "1.1", "1.2",
    "2.1", "2.2",
    "3.1", "3.2",
    "4.1", "4.2",
    "5.1", "5.2",
    "6.1", "6.2",
]
QUEUES = {q: f"{BASE_URL}/{q.replace('.', '-')}" for q in QUEUE_NUMS}

TIMER_RE = re.compile(r"через\s*(\d{1,2})\s*год\s*(\d{1,2})\s*хв", re.IGNORECASE)
SLOT_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*[–—\-]\s*(\d{1,2}:\d{2})\s*\n\s*(Немає світла|Є світло)",
    re.IGNORECASE,
)
CHALLENGE_MARKERS = (
    "выполнение проверки безопасности",
    "проверки безопасности",
    "перевірка безпеки",
    "just a moment",
)


@dataclass
class Config:
    token: str
    user_data_dir: str
    cache_ttl: int
    timeout_ms: int
    challenge_wait_sec: int
    opera_path: str | None


@dataclass
class OutageState:
    queue: str
    status: str
    is_off: bool
    eta: str | None
    periods: list[tuple[str, str]]


def find_opera() -> str | None:
    env_path = os.getenv("OPERA_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        return None

    candidates = glob.glob(os.path.join(local_app_data, "Programs", "Opera GX", "*", "opera.exe"))
    return max(candidates, key=os.path.getmtime) if candidates else None


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip() or DEFAULT_TOKEN.strip()
    if not token:
        raise RuntimeError("Missing BOT_TOKEN")

    return Config(
        token=token,
        user_data_dir=os.getenv("PLAYWRIGHT_USER_DATA", "./user_data"),
        cache_ttl=int(os.getenv("CACHE_TTL_SEC", "120")),
        timeout_ms=int(os.getenv("PAGE_TIMEOUT_MS", "45000")),
        challenge_wait_sec=int(os.getenv("CHALLENGE_WAIT_SEC", "25")),
        opera_path=find_opera(),
    )


def has_challenge(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in CHALLENGE_MARKERS)


class PageFetcher:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, str]] = {}

    async def _scrape(self, url: str) -> str | None:
        if not self.cfg.opera_path:
            log.error("Opera binary not found; set OPERA_PATH")
            return None

        async with self._lock:
            try:
                async with async_playwright() as pw:
                    ctx = await pw.chromium.launch_persistent_context(
                        user_data_dir=self.cfg.user_data_dir,
                        executable_path=self.cfg.opera_path,
                        headless=False,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    try:
                        page = await ctx.new_page()
                        await page.goto(url, timeout=self.cfg.timeout_ms, wait_until="domcontentloaded")

                        body_text = ""
                        for _ in range(self.cfg.challenge_wait_sec):
                            body_text = await page.inner_text("body")
                            if body_text.strip() and not has_challenge(body_text):
                                break
                            await page.wait_for_timeout(1000)

                        try:
                            await page.get_by_text("Allow ads", exact=False).click(timeout=2000)
                        except Exception:
                            pass

                        return await page.inner_text("body")
                    finally:
                        await ctx.close()
            except Exception:
                log.exception("Scrape failed for %s", url)
                return None

    async def get_text(self, queue: str, force: bool = False) -> str | None:
        url = QUEUES.get(queue)
        if not url:
            return None

        now = time.monotonic()
        cached = self._cache.get(queue)
        if not force and cached and (now - cached[0]) < self.cfg.cache_ttl:
            return cached[1]

        text = await self._scrape(url)
        if text and not has_challenge(text):
            self._cache[queue] = (now, text)
        return text


def to_hhmm(raw: str) -> str:
    h, m = map(int, raw.strip().split(":"))
    return f"{h:02d}:{m:02d}"


def to_mins(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def parse_page(queue: str, text: str, now_hhmm: str) -> OutageState:
    if has_challenge(text):
        return OutageState(
            queue=queue,
            status="Blocked by Cloudflare",
            is_off=False,
            eta=None,
            periods=[],
        )

    low = text.lower()
    if "зараз за графіком немає світла" in low or "немає світла\n\nвідключення за графіком" in low:
        status, is_off = "Power OFF", True
    elif "зараз за графіком є світло" in low or "електроенергія присутня" in low:
        status, is_off = "Power ON", False
    else:
        status, is_off = "Unknown", False

    eta = None
    timer_m = TIMER_RE.search(text)
    if timer_m:
        h, m = map(int, timer_m.groups())
        eta = f"{h}h {m}m"

    now_m = to_mins(now_hhmm)
    upcoming: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for raw_start, raw_end, state_label in SLOT_RE.findall(text):
        if "немає" not in state_label.lower():
            continue

        start = to_hhmm(raw_start)
        end = to_hhmm(raw_end)
        pair = (start, end)
        if pair in seen:
            continue

        end_m = to_mins(end)
        if end_m == 0:
            end_m = 24 * 60

        if end_m > now_m:
            seen.add(pair)
            upcoming.append(pair)

    return OutageState(
        queue=queue,
        status=status,
        is_off=is_off,
        eta=eta,
        periods=upcoming,
    )


def main_kb() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(QUEUE_NUMS), 2):
        left = InlineKeyboardButton(text=f"Queue {QUEUE_NUMS[i]}", callback_data=f"q:{QUEUE_NUMS[i]}")
        right = InlineKeyboardButton(text=f"Queue {QUEUE_NUMS[i + 1]}", callback_data=f"q:{QUEUE_NUMS[i + 1]}")
        rows.append([left, right])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_kb(queue: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Refresh", callback_data=f"q:{queue}:1")],
            [InlineKeyboardButton(text="Queues", callback_data="menu")],
        ]
    )


def render_state(st: OutageState) -> str:
    parts = [f"<b>Queue {st.queue}</b>", f"Status: <b>{st.status}</b>"]

    if st.eta:
        label = "Back in" if st.is_off else "Next off in"
        parts.append(f"{label}: <b>{st.eta}</b>")

    parts.append("")
    if st.periods:
        parts.append("<b>Upcoming today:</b>")
        for a, b in st.periods:
            parts.append(f"• <code>{a} - {b}</code>")
    else:
        parts.append("No more outages left today.")

    return "\n".join(parts)


def parse_queue_arg(text: str) -> str | None:
    s = text.strip().lower()
    s = re.sub(r"^/?q(?:ueue)?[\s._-]*", "", s)
    s = s.replace("-", ".").replace("_", ".")
    if re.fullmatch(r"\d{2}", s):
        s = f"{s[0]}.{s[1]}"
    return s if s in QUEUES else None


def setup_router(dp: Dispatcher, fetcher: PageFetcher) -> None:
    @dp.message(Command("start", "power"))
    async def on_start(msg: types.Message) -> None:
        await msg.answer("Select a queue or send its number (e.g. <code>1.1</code>, <code>5-1</code>):", reply_markup=main_kb())

    @dp.callback_query(F.data == "menu")
    async def on_menu(cb: types.CallbackQuery) -> None:
        if cb.message:
            await cb.message.edit_text("Select a queue:", reply_markup=main_kb())
        await cb.answer()

    @dp.callback_query(F.data.startswith("q:"))
    async def on_queue_cb(cb: types.CallbackQuery) -> None:
        parts = cb.data.split(":")
        q = parts[1] if len(parts) > 1 else ""
        force = len(parts) > 2 and parts[2] == "1"

        if q not in QUEUES:
            await cb.answer("Unknown queue", show_alert=True)
            return

        await cb.answer()
        if cb.message:
            await cb.message.edit_text(f"Checking {q}...")
            raw = await fetcher.get_text(q, force=force)
            if not raw:
                await cb.message.edit_text("Failed to fetch data, try again later.", reply_markup=item_kb(q))
                return
            state = parse_page(q, raw, datetime.now().strftime("%H:%M"))
            await cb.message.edit_text(render_state(state), reply_markup=item_kb(q))

    @dp.message(F.text)
    async def on_text(msg: types.Message) -> None:
        q = parse_queue_arg(msg.text or "")
        if not q:
            return

        log.info("queue=%s user=%s", q, msg.from_user.id if msg.from_user else None)
        wait_msg = await msg.answer(f"Checking {q}...")
        raw = await fetcher.get_text(q)
        if not raw:
            await wait_msg.edit_text("Failed to fetch data, try again later.", reply_markup=item_kb(q))
            return

        state = parse_page(q, raw, datetime.now().strftime("%H:%M"))
        await wait_msg.edit_text(render_state(state), reply_markup=item_kb(q))


async def run() -> None:
    cfg = load_config()
    bot = Bot(token=cfg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    setup_router(dp, PageFetcher(cfg))

    log.info("Bot polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
