import asyncio, logging, os, random, uuid, json
from datetime import date
from io import BytesIO
from collections import defaultdict

import aiomysql
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, PreCheckoutQuery, Message, BufferedInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from PIL import Image, ImageDraw, ImageFont

from config import *

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
USER_TEMPLATES_DIR = os.path.join(BASE_DIR, "user_templates")
FONT_PATH = os.path.join(BASE_DIR, "font/impact.ttf")
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(USER_TEMPLATES_DIR, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
pool = None
admin_ids: set[int] = set()

# ---------- База данных ----------
async def create_db_pool():
    global pool
    pool = await aiomysql.create_pool(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, db=MYSQL_DB,
        autocommit=True, minsize=1, maxsize=5,
    )

async def init_db():
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    premium TINYINT DEFAULT 0,
                    last_reset DATE,
                    memes_today INT DEFAULT 0
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS memes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    file_id VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            try:
                await cur.execute("ALTER TABLE memes ADD COLUMN rating INT DEFAULT 0")
            except Exception:
                pass
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS meme_votes (
                    user_id BIGINT,
                    meme_id INT,
                    vote TINYINT,
                    PRIMARY KEY (user_id, meme_id)
                )
            """)
            for uid in ADMIN_IDS:
                await cur.execute("INSERT IGNORE INTO admins (user_id) VALUES (%s)", (uid,))

async def load_admins():
    global admin_ids
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id FROM admins")
            admin_ids = {row[0] for row in await cur.fetchall()}

async def is_admin(user: types.User) -> bool:
    return user.id in admin_ids or user.username == ADMIN_USERNAME

def admin_only(func):
    async def wrapper(message: Message):
        if not await is_admin(message.from_user):
            await message.answer("⛔ Нет доступа.")
            return
        await func(message)
    return wrapper

async def add_admin_to_db(user_id: int):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("INSERT IGNORE INTO admins (user_id) VALUES (%s)", (user_id,))
    admin_ids.add(user_id)

async def remove_admin_from_db(user_id: int):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM admins WHERE user_id = %s", (user_id,))
    admin_ids.discard(user_id)

async def get_user(user_id: int, username: str = None) -> dict:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    "INSERT INTO users (user_id, username, last_reset, memes_today) VALUES (%s,%s,%s,%s)",
                    (user_id, username, str(date.today()), 0))
                return {"user_id": user_id, "username": username, "premium": False,
                        "last_reset": str(date.today()), "memes_today": 0}
            if username and username != row[1]:
                await cur.execute("UPDATE users SET username=%s WHERE user_id=%s", (username, user_id))
            return {"user_id": row[0], "username": username or row[1],
                    "premium": bool(row[2]), "last_reset": str(row[3]), "memes_today": row[4]}

async def reset_daily_if_needed(user_id: int):
    user = await get_user(user_id)
    today = str(date.today())
    if user["last_reset"] != today:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE users SET memes_today=0, last_reset=%s WHERE user_id=%s", (today, user_id))
        user["memes_today"] = 0; user["last_reset"] = today
    return user

async def increment_meme_count(user_id: int):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE users SET memes_today = memes_today+1 WHERE user_id=%s", (user_id,))

async def set_premium(user_id: int, premium: bool):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE users SET premium=%s WHERE user_id=%s", (int(premium), user_id))

async def save_meme_file_id(user_id: int, file_id: str) -> int:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO memes (user_id, file_id) VALUES (%s,%s)", (user_id, file_id))
            return cur.lastrowid

async def get_user_memes(user_id: int, limit=10):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT file_id FROM memes WHERE user_id=%s ORDER BY id DESC LIMIT %s", (user_id, limit))
            return [row[0] for row in await cur.fetchall()]

async def resolve_user_id(identifier: str) -> int | None:
    identifier = identifier.strip().lstrip("@")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id FROM users WHERE username = %s", (identifier,))
            row = await cur.fetchone()
            if row:
                return row[0]
    try:
        return int(identifier)
    except ValueError:
        return None

# ---------- Проверка подписки ----------
async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logging.error(f"Sub check error {user_id}: {e}")
        return False

async def require_subscription(user_id: int) -> bool:
    if await check_subscription(user_id):
        return True
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
    ])
    await bot.send_message(user_id,
        "🔔 Чтобы пользоваться ботом, нужно подписаться на наш канал.\nПодпишись и нажми кнопку проверки.",
        reply_markup=kb)
    return False

# ---------- FSM ----------
class MemeCreation(StatesGroup):
    choosing_template = State()
    waiting_for_top_text = State()
    waiting_for_bottom_text = State()

class TemplateNaming(StatesGroup):
    waiting_for_name = State()

class AITextGeneration(StatesGroup):
    waiting_for_prompt = State()

# ---------- Клавиатуры ----------
def main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎨 Создать мем", callback_data="create_meme"),
        InlineKeyboardButton(text="🤖 AI мем", callback_data="ai_meme"))
    builder.row(
        InlineKeyboardButton(text="💎 Купить премиум", callback_data="buy_premium"),
        InlineKeyboardButton(text="🖼 Мои шаблоны", callback_data="my_templates"))
    builder.row(
        InlineKeyboardButton(text="📜 История", callback_data="history"),
        InlineKeyboardButton(text="📞 Поддержка", callback_data="support"))
    builder.row(
        InlineKeyboardButton(text="🎲 Случайный мем", callback_data="random_meme"),
        InlineKeyboardButton(text="🔔 Проверить подписку", callback_data="check_sub"))
    return builder.as_markup()

# ---------- Категории ----------
def get_categories():
    cats = defaultdict(list)
    for item in os.listdir(TEMPLATES_DIR):
        item_path = os.path.join(TEMPLATES_DIR, item)
        if os.path.isdir(item_path):
            for f in os.listdir(item_path):
                if os.path.isfile(os.path.join(item_path, f)):
                    cats[item].append(f)
        elif os.path.isfile(item_path):
            cats["Без категории"].append(item)
    return cats

def categories_keyboard():
    builder = InlineKeyboardBuilder()
    for cat in sorted(get_categories().keys()):
        builder.row(InlineKeyboardButton(text=cat, callback_data=f"cat_{cat}"))
    builder.row(InlineKeyboardButton(text="🏠 Главная", callback_data="back_main"))
    return builder.as_markup()

def templates_in_category_keyboard(category, page=0):
    cats = get_categories()
    if category not in cats:
        return None
    files = sorted(cats[category])
    per_page = 5
    total_pages = max(1, (len(files) + per_page - 1) // per_page)
    page = page % total_pages
    start = page * per_page
    chunk = files[start:start + per_page]
    builder = InlineKeyboardBuilder()
    for idx, f in enumerate(chunk, start=start):
        name = os.path.splitext(f)[0]
        full_path = os.path.join(TEMPLATES_DIR, category, f) if category != "Без категории" else os.path.join(TEMPLATES_DIR, f)
        relative = os.path.relpath(full_path, TEMPLATES_DIR)
        builder.row(InlineKeyboardButton(text=name, callback_data=f"tpl_{relative}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"catpage_{category}_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"catpage_{category}_{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(text="🔙 К категориям", callback_data="create_meme"),
        InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")
    )
    return builder.as_markup()

# --- Безопасное редактирование ---
async def safe_edit(callback: types.CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()

# --- Адаптивная генерация мема ---
def fit_text(draw, text, font_path, max_width, max_height, start_size=120, min_size=14):
    size = start_size
    while size >= min_size:
        try:
            font = ImageFont.truetype(font_path, size=size)
        except:
            font = ImageFont.load_default()
        words = text.split()
        lines = []
        current_line = ""
        for word in words:
            test_line = f"{current_line} {word}" if current_line else word
            bbox = draw.textbbox((0, 0), test_line, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        total_height = 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            total_height += bbox[3] - bbox[1] + 5
        if total_height <= max_height:
            return font, lines
        size -= 2
    return font, lines

async def generate_meme(template_path, top_text, bottom_text, is_premium):
    img = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size

    max_text_height = int(h * 0.25)
    max_text_width = int(w * 0.9)

    top = top_text.upper().strip() if top_text else ""
    if top:
        font, lines = fit_text(draw, top, FONT_PATH, max_text_width, max_text_height, start_size=int(h*0.15))
        y = int(h * 0.05)
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            x = (w - tw) / 2
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    if dx != 0 or dy != 0:
                        draw.text((x+dx, y+dy), line, font=font, fill="black")
            draw.text((x, y), line, font=font, fill="white")
            y += bbox[3] - bbox[1] + 5

    bottom = bottom_text.upper().strip() if bottom_text else ""
    if bottom:
        font, lines = fit_text(draw, bottom, FONT_PATH, max_text_width, max_text_height, start_size=int(h*0.15))
        heights = [draw.textbbox((0,0), line, font=font)[3] for line in lines]
        total_h = sum(heights) + (len(lines)-1)*5
        y = h - total_h - int(h * 0.05)
        for i, line in enumerate(lines):
            bbox = draw.textbbox((0,0), line, font=font)
            tw = bbox[2] - bbox[0]
            x = (w - tw) / 2
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    if dx != 0 or dy != 0:
                        draw.text((x+dx, y+dy), line, font=font, fill="black")
            draw.text((x, y), line, font=font, fill="white")
            y += heights[i] + 5

    watermark = BOT_USERNAME
    if watermark:
        wm_size = max(20, int(h * 0.15))
        try: wmfont = ImageFont.truetype(FONT_PATH, size=wm_size)
        except: wmfont = ImageFont.load_default()
        if not is_premium:
            overlay = Image.new("RGBA", img.size, (255,255,255,0))
            odraw = ImageDraw.Draw(overlay)
            bbox = odraw.textbbox((0,0), watermark, font=wmfont)
            twm, thm = bbox[2]-bbox[0], bbox[3]-bbox[1]
            x = (w - twm)/2; y = (h - thm)/2
            for dx in range(-3,4):
                for dy in range(-3,4):
                    if dx or dy:
                        odraw.text((x+dx, y+dy), watermark, font=wmfont, fill=(0,0,0,255))
            odraw.text((x, y), watermark, font=wmfont, fill=(255,255,255,180))
            img = img.convert("RGBA")
            img = Image.alpha_composite(img, overlay)
        else:
            small = max(15, int(h*0.04))
            try: sfont = ImageFont.truetype(FONT_PATH, size=small)
            except: sfont = ImageFont.load_default()
            tag = f"Создано {watermark}"
            bbox = draw.textbbox((0,0), tag, font=sfont)
            tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
            draw.text((w-tw-10, h-th-10), tag, font=sfont, fill=(0,0,0,200))
            draw.text((w-tw-11, h-th-11), tag, font=sfont, fill=(255,255,255,230))

    output = BytesIO()
    img.convert("RGB").save(output, format="JPEG", quality=95)
    output.seek(0)
    return output

# ---------- YandexGPT AI ----------
async def generate_ai_text(user_prompt: str):
    try:
        url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
        headers = {
            "Authorization": f"Api-Key {AI_API_KEY}",
            "x-folder-id": FOLDER_ID
        }
        payload = {
            "modelUri": f"gpt://{FOLDER_ID}/yandexgpt/latest",
            "completionOptions": {
                "stream": False,
                "temperature": 0.9,
                "maxTokens": "100"
            },
            "messages": [
                {
                    "role": "system",
                    "text": "You are a creative meme caption generator. Return ONLY a JSON object with keys 'top' and 'bottom' containing the funny captions."
                },
                {
                    "role": "user",
                    "text": user_prompt
                }
            ]
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    logging.error(f"YandexGPT API error {resp.status}: {await resp.text()}")
                    return None
                data = await resp.json()
                raw_text = data["result"]["alternatives"][0]["message"]["text"]
                start = raw_text.find('{')
                end = raw_text.rfind('}') + 1
                if start >= 0 and end > start:
                    json_str = raw_text[start:end]
                    parsed = json.loads(json_str)
                    return {"top": parsed.get("top", ""), "bottom": parsed.get("bottom", "")}
    except Exception as e:
        logging.error(f"YandexGPT AI error: {e}")
    return None

# ========== Обработчики ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await get_user(message.from_user.id, message.from_user.username)
    if not await require_subscription(message.from_user.id):
        return
    await message.answer(
        "🤖 Привет! Я — продвинутый мем‑генератор.\n\n"
        f"🎨 <b>Бесплатно:</b> {MAX_FREE_MEMES} мема в день с большим водяным знаком.\n"
        f"💎 <b>Премиум (всего {PREMIUM_PRICE_STARS}⭐ навсегда):</b>\n"
        "• безлимитное создание мемов\n"
        "• загрузка своих шаблонов\n"
        "• генерация AI‑мемов по текстовому описанию\n"
        "• водяной знак минимален\n\n"
        "Жми на кнопки ниже и начни творить!",
        reply_markup=main_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: types.CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="💎 Премиум‑доступ навсегда",
        description="Безлимитные мемы, свои шаблоны, AI‑мемы, минимальный водяной знак.",
        payload="premium_access", provider_token="", currency="XTR",
        prices=[LabeledPrice(label="Премиум навсегда", amount=PREMIUM_PRICE_STARS)],
        max_tip_amount=0, suggested_tip_amounts=[], start_parameter="premium",
        need_name=False, need_phone_number=False, need_email=False,
        need_shipping_address=False, is_flexible=False)

@dp.pre_checkout_query(lambda q: True)
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    if message.successful_payment.invoice_payload == "premium_access":
        await set_premium(message.from_user.id, True)
        await message.answer("🎉 Поздравляю! Премиум активирован навсегда. AI‑мемы и безлимит твои!", reply_markup=main_keyboard())

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_keyboard())

# --- Категории ---
@dp.callback_query(F.data == "create_meme")
async def show_categories(callback: types.CallbackQuery, state: FSMContext):
    if not await require_subscription(callback.from_user.id):
        await callback.answer()
        return
    cats = get_categories()
    if len(cats) == 1 and "Без категории" in cats:
        await safe_edit(callback, "Выбери шаблон:", reply_markup=templates_in_category_keyboard("Без категории"))
    else:
        await safe_edit(callback, "Выбери категорию:", reply_markup=categories_keyboard())
    await state.set_state(MemeCreation.choosing_template)

@dp.callback_query(F.data.startswith("cat_"))
async def show_templates_in_category(callback: types.CallbackQuery):
    category = callback.data[4:]
    markup = templates_in_category_keyboard(category)
    if markup:
        await safe_edit(callback, f"Шаблоны в категории «{category}»:", reply_markup=markup)
    else:
        await callback.answer("Категория пуста", show_alert=True)

@dp.callback_query(F.data.startswith("catpage_"))
async def page_category(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    category = parts[1]
    page = int(parts[2])
    markup = templates_in_category_keyboard(category, page)
    if markup:
        await safe_edit(callback, f"Шаблоны в категории «{category}»:", reply_markup=markup)

@dp.callback_query(F.data.startswith("tpl_"))
async def tpl_selected(callback: types.CallbackQuery, state: FSMContext):
    rel_path = callback.data[4:]
    full_path = os.path.join(TEMPLATES_DIR, rel_path)
    if not os.path.isfile(full_path):
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    await state.update_data(template_path=full_path)
    await callback.message.answer("✏️ Введи верхний текст (или `-`):")
    await state.set_state(MemeCreation.waiting_for_top_text)

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: types.CallbackQuery):
    if await check_subscription(callback.from_user.id):
        if callback.message.reply_markup and callback.message.reply_markup.inline_keyboard:
            row = callback.message.reply_markup.inline_keyboard[0]
            if row and row[0].callback_data == "check_sub":
                await callback.message.delete()
                await bot.send_message(callback.from_user.id, "✅ Подписка подтверждена!", reply_markup=main_keyboard())
                await callback.answer()
                return
        await callback.answer("✅ Подписка активна!", show_alert=False)
    else:
        await callback.answer("❌ Вы ещё не подписаны", show_alert=True)
    await callback.answer()

# --- AI мем (текстовый) ---
@dp.callback_query(F.data == "ai_meme")
async def ai_meme_start(callback: types.CallbackQuery, state: FSMContext):
    if not await require_subscription(callback.from_user.id): return
    user = await get_user(callback.from_user.id)
    if not user["premium"]:
        await callback.message.answer(
            "🤖 <b>AI‑мемы — только для премиум!</b>\n\n"
            f"За {PREMIUM_PRICE_STARS}⭐ навсегда ты получишь:\n"
            "• безлимитную генерацию\n"
            "• загрузку своих картинок\n"
            "• мемы без большого водяного знака\n\n"
            "Нажми кнопку ниже, чтобы купить.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить премиум", callback_data="buy_premium")],
                [InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")]
            ]))
        await callback.answer()
        return
    await callback.message.answer("📝 Опишите идею для мема (например: «кот, который смотрит в зеркало и видит льва»).")
    await state.set_state(AITextGeneration.waiting_for_prompt)

@dp.message(StateFilter(AITextGeneration.waiting_for_prompt))
async def ai_prompt_received(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user["premium"]:
        await message.answer("Только для премиум."); await state.clear(); return
    prompt = message.text.strip()
    await message.answer("🤖 AI Memer думает...")
    captions = await generate_ai_text(prompt)
    if captions:
        templates = []
        for root, dirs, files in os.walk(TEMPLATES_DIR):
            for f in files:
                if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                    templates.append(os.path.join(root, f))
        if templates:
            tpl = random.choice(templates)
            meme_io = await generate_meme(tpl, captions["top"], captions["bottom"], is_premium=True)
            msg = await message.answer_photo(BufferedInputFile(meme_io.read(), "ai_meme.jpg"),
                                            caption=f"AI-мем по идее: «{prompt}»")
            meme_id = await save_meme_file_id(message.from_user.id, msg.photo[-1].file_id)
            await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👍", callback_data=f"like_{meme_id}"),
                 InlineKeyboardButton(text="👎", callback_data=f"dislike_{meme_id}")]
            ]))
        else:
            await message.answer("❌ Нет шаблонов для создания мема.")
    else:
        await message.answer("❌ Не удалось сгенерировать текст.")
    await state.clear()
    await message.answer("Что дальше?", reply_markup=main_keyboard())

# --- Мои шаблоны (премиум) ---
@dp.callback_query(F.data == "my_templates")
async def my_templates(callback: types.CallbackQuery):
    if not await require_subscription(callback.from_user.id): return
    user = await get_user(callback.from_user.id, callback.from_user.username)
    if not user["premium"]:
        await callback.answer("🔒 Только для премиум", show_alert=True)
        return
    user_dir = os.path.join(USER_TEMPLATES_DIR, str(callback.from_user.id))
    os.makedirs(user_dir, exist_ok=True)
    files = sorted(os.listdir(user_dir))
    if not files:
        await callback.message.edit_text("Нет своих шаблонов. Отправьте картинку командой /upload.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")]]))
        return
    builder = InlineKeyboardBuilder()
    for idx, f in enumerate(files):
        builder.row(InlineKeyboardButton(text=f, callback_data=f"myusr_{idx}"))
    builder.row(InlineKeyboardButton(text="🗑 Удалить шаблоны", callback_data="delete_templates_menu"))
    builder.row(InlineKeyboardButton(text="🏠 Главная", callback_data="back_main"))
    await callback.message.edit_text("🖼 Твои шаблоны:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("myusr_"))
async def user_tpl_selected(callback: types.CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if not user["premium"]: return
    idx = int(callback.data.split("_")[1])
    user_dir = os.path.join(USER_TEMPLATES_DIR, str(callback.from_user.id))
    files = sorted(os.listdir(user_dir))
    if 0 <= idx < len(files):
        path = os.path.join(user_dir, files[idx])
        if os.path.isfile(path):
            await state.update_data(template_path=path)
            await callback.message.answer("✏️ Введи верхний текст (или `-`):")
            await state.set_state(MemeCreation.waiting_for_top_text)
    else:
        await callback.answer("Шаблон не найден", show_alert=True)

@dp.callback_query(F.data == "delete_templates_menu")
async def delete_tpl_menu(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user["premium"]: return
    user_dir = os.path.join(USER_TEMPLATES_DIR, str(callback.from_user.id))
    files = sorted(os.listdir(user_dir))
    if not files:
        await callback.answer("Нет шаблонов для удаления"); return
    builder = InlineKeyboardBuilder()
    for idx, f in enumerate(files):
        builder.row(InlineKeyboardButton(text=f"❌ {f}", callback_data=f"deltpl_{idx}"))
    builder.row(InlineKeyboardButton(text="🏠 Главная", callback_data="back_main"))
    await callback.message.edit_text("Выбери шаблон для удаления:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("deltpl_"))
async def delete_tpl(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user["premium"]: return
    idx = int(callback.data.split("_")[1])
    user_dir = os.path.join(USER_TEMPLATES_DIR, str(callback.from_user.id))
    files = sorted(os.listdir(user_dir))
    if 0 <= idx < len(files):
        os.remove(os.path.join(user_dir, files[idx]))
        await callback.answer(f"Шаблон {files[idx]} удалён")
        new_files = os.listdir(user_dir)
        if new_files:
            builder = InlineKeyboardBuilder()
            for i, f in enumerate(new_files):
                builder.row(InlineKeyboardButton(text=f"❌ {f}", callback_data=f"deltpl_{i}"))
            builder.row(InlineKeyboardButton(text="🏠 Главная", callback_data="back_main"))
            await callback.message.edit_text("Выбери шаблон для удаления:", reply_markup=builder.as_markup())
        else:
            await callback.message.edit_text("Шаблоны удалены.", reply_markup=main_keyboard())

# --- Загрузка с переименованием (премиум) ---
@dp.message(Command("upload"))
async def cmd_upload(message: Message):
    if not await require_subscription(message.from_user.id): return
    user = await get_user(message.from_user.id, message.from_user.username)
    if not user["premium"]:
        await message.answer("🔒 Загрузка своих шаблонов доступна только с премиумом.")
        return
    await message.answer("Отправьте мне картинку (желательно квадратную).")

@dp.message(F.photo)
async def photo_upload(message: Message, state: FSMContext):
    if not await require_subscription(message.from_user.id): return
    user = await get_user(message.from_user.id, message.from_user.username)
    if not user["premium"]: return
    photo = message.photo[-1]
    user_dir = os.path.join(USER_TEMPLATES_DIR, str(message.from_user.id))
    os.makedirs(user_dir, exist_ok=True)
    temp_name = f"{uuid.uuid4().hex}.jpg"
    path = os.path.join(user_dir, temp_name)
    await bot.download(photo, destination=path)
    await state.update_data(temp_path=path)
    await message.answer("✏️ Введи название для шаблона (или отправь `-` для автоимени):")
    await state.set_state(TemplateNaming.waiting_for_name)

@dp.message(StateFilter(TemplateNaming.waiting_for_name))
async def template_naming(message: Message, state: FSMContext):
    data = await state.get_data()
    temp_path = data["temp_path"]
    user_dir = os.path.dirname(temp_path)
    raw = message.text.strip()
    if raw in ("-", ""):
        final_name = os.path.basename(temp_path)
    else:
        safe = raw.replace("/", "").replace("\\", "")
        if not safe: safe = uuid.uuid4().hex
        final_name = f"{safe}.jpg"
        new_path = os.path.join(user_dir, final_name)
        if os.path.exists(new_path):
            await message.answer("⚠️ Такое имя уже существует. Придумай другое или отправь `-`.")
            return
        os.rename(temp_path, new_path)
    await state.clear()
    await message.answer(f"✅ Шаблон сохранён как `{final_name}`. Доступен в «Мои шаблоны».")
    await message.answer("Что дальше?", reply_markup=main_keyboard())

# --- История ---
@dp.callback_query(F.data == "history")
async def history(callback: types.CallbackQuery):
    if not await require_subscription(callback.from_user.id): return
    meme_ids = await get_user_memes(callback.from_user.id)
    if not meme_ids:
        await callback.message.edit_text("📭 У вас пока нет созданных мемов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")]]))
        return
    for file_id in meme_ids:
        await bot.send_photo(callback.from_user.id, file_id)
    await callback.message.answer("🔙 Вернуться в главное меню?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")]]))
    await callback.answer()

# --- Случайный мем (с показом остатка) ---
RANDOM_PHRASES = [
    "Когда дедлайн через час", "Я и мои планы на выходные",
    "Никто:\nЯ:", "Зачем ты так?", "Ну как так-то",
    "Потом расскажу", "Моё лицо, когда...", "Это фиаско, братан",
    "Всё идёт по плану", "Ожидание / Реальность", "Снова ты"
]

@dp.callback_query(F.data == "random_meme")
async def random_meme(callback: types.CallbackQuery):
    if not await require_subscription(callback.from_user.id): return
    templates = []
    for root, dirs, files in os.walk(TEMPLATES_DIR):
        for f in files:
            if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                templates.append(os.path.join(root, f))
    if not templates:
        await callback.answer("Нет шаблонов", show_alert=True); return
    user = await get_user(callback.from_user.id, callback.from_user.username)
    is_premium = user["premium"]
    if not is_premium and user["memes_today"] >= MAX_FREE_MEMES:
        await callback.message.answer(
            f"❌ Лимит {MAX_FREE_MEMES} мемов исчерпан.\nКупи премиум за {PREMIUM_PRICE_STARS}⭐ и создавай без ограничений!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить премиум", callback_data="buy_premium")],
                [InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")]
            ]))
        await callback.answer()
        return
    tpl = random.choice(templates)
    top = random.choice(RANDOM_PHRASES)
    bottom = random.choice(RANDOM_PHRASES) if random.random() > 0.5 else ""
    meme_io = await generate_meme(tpl, top, bottom, is_premium)
    msg = await bot.send_photo(callback.from_user.id, BufferedInputFile(meme_io.read(), "random.jpg"))
    meme_id = await save_meme_file_id(callback.from_user.id, msg.photo[-1].file_id)
    await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍", callback_data=f"like_{meme_id}"),
         InlineKeyboardButton(text="👎", callback_data=f"dislike_{meme_id}")]
    ]))
    if not is_premium:
        await increment_meme_count(callback.from_user.id)
        remain = MAX_FREE_MEMES - user["memes_today"] - 1
        await bot.send_message(callback.from_user.id,
            f"🎯 Осталось {remain} из {MAX_FREE_MEMES} бесплатных мемов.\n"
            f"💎 Купи премиум за {PREMIUM_PRICE_STARS}⭐ и забудь о лимитах!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить премиум", callback_data="buy_premium")],
                [InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")]
            ]))
    else:
        await bot.send_message(callback.from_user.id, "Что дальше?", reply_markup=main_keyboard())
    await callback.answer("Готово!")

# --- Текстовые шаги (ручной мем) с показом остатка ---
@dp.message(StateFilter(MemeCreation.waiting_for_top_text))
async def enter_top(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "-": text = ""
    await state.update_data(top_text=text)
    await message.answer("✏️ Теперь введи нижний текст (или `-`):")
    await state.set_state(MemeCreation.waiting_for_bottom_text)

@dp.message(StateFilter(MemeCreation.waiting_for_bottom_text))
async def enter_bottom(message: Message, state: FSMContext):
    if not await require_subscription(message.from_user.id): return
    text = message.text.strip()
    if text == "-": text = ""
    data = await state.get_data()
    user = await get_user(message.from_user.id, message.from_user.username)
    is_premium = user["premium"]
    if not is_premium and user["memes_today"] >= MAX_FREE_MEMES:
        await message.answer(
            f"❌ Лимит {MAX_FREE_MEMES} мемов исчерпан.\nКупи премиум за {PREMIUM_PRICE_STARS}⭐ и создавай без ограничений!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить премиум", callback_data="buy_premium")],
                [InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")]
            ]))
        await state.clear()
        return
    meme_io = await generate_meme(data["template_path"], data["top_text"], text, is_premium)
    msg = await message.answer_photo(BufferedInputFile(meme_io.read(), "meme.jpg"), caption="Готово!")
    meme_id = await save_meme_file_id(message.from_user.id, msg.photo[-1].file_id)
    await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍", callback_data=f"like_{meme_id}"),
         InlineKeyboardButton(text="👎", callback_data=f"dislike_{meme_id}")]
    ]))
    if not is_premium:
        await increment_meme_count(message.from_user.id)
        remain = MAX_FREE_MEMES - user["memes_today"] - 1
        await message.answer(
            f"🎯 Осталось {remain} из {MAX_FREE_MEMES} бесплатных мемов.\n"
            f"💎 Купи премиум за {PREMIUM_PRICE_STARS}⭐ и забудь о лимитах!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить премиум", callback_data="buy_premium")],
                [InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")]
            ]))
    else:
        await message.answer("Что дальше?", reply_markup=main_keyboard())
    await state.clear()

# --- Голосование ---
async def process_vote(user_id, meme_id, vote, callback):
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id FROM memes WHERE id=%s", (meme_id,))
                if not await cur.fetchone():
                    await callback.answer("Мем не найден"); return
                await cur.execute("SELECT vote FROM meme_votes WHERE user_id=%s AND meme_id=%s", (user_id, meme_id))
                row = await cur.fetchone()
                if row:
                    old_vote = row[0]
                    if old_vote == vote:
                        await callback.answer("Вы уже проголосовали так же"); return
                    await cur.execute("UPDATE meme_votes SET vote=%s WHERE user_id=%s AND meme_id=%s",
                                      (vote, user_id, meme_id))
                    delta = vote - old_vote
                    await cur.execute("UPDATE memes SET rating = rating + %s WHERE id=%s", (delta, meme_id))
                else:
                    await cur.execute("INSERT INTO meme_votes (user_id, meme_id, vote) VALUES (%s,%s,%s)",
                                      (user_id, meme_id, vote))
                    await cur.execute("UPDATE memes SET rating = rating + %s WHERE id=%s", (vote, meme_id))
                await cur.execute("SELECT rating FROM memes WHERE id=%s", (meme_id,))
                new_rating = (await cur.fetchone())[0]
        await callback.answer(f"Рейтинг: {new_rating}")
    except Exception as e:
        logging.error(f"process_vote error: {e}")
        await callback.answer("Ошибка голосования")

@dp.callback_query(F.data.startswith("like_"))
async def like_meme(callback: types.CallbackQuery):
    meme_id = int(callback.data.split("_")[1])
    await process_vote(callback.from_user.id, meme_id, 1, callback)

@dp.callback_query(F.data.startswith("dislike_"))
async def dislike_meme(callback: types.CallbackQuery):
    meme_id = int(callback.data.split("_")[1])
    await process_vote(callback.from_user.id, meme_id, -1, callback)

# --- Статистика ---
@dp.message(Command("mystats"))
async def my_stats(message: Message):
    user_id = message.from_user.id
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM memes WHERE user_id=%s", (user_id,))
            total = (await cur.fetchone())[0]
            await cur.execute("SELECT COALESCE(SUM(rating),0) FROM memes WHERE user_id=%s", (user_id,))
            total_rating = (await cur.fetchone())[0]
            await cur.execute("SELECT COUNT(*) FROM meme_votes WHERE meme_id IN (SELECT id FROM memes WHERE user_id=%s) AND vote=1", (user_id,))
            likes = (await cur.fetchone())[0]
            await cur.execute("SELECT COUNT(*) FROM meme_votes WHERE meme_id IN (SELECT id FROM memes WHERE user_id=%s) AND vote=-1", (user_id,))
            dislikes = (await cur.fetchone())[0]
    await message.answer(
        f"📊 <b>Ваша статистика</b>\nСоздано мемов: {total}\nРейтинг: {total_rating}\n👍 {likes} | 👎 {dislikes}",
        parse_mode="HTML")

@dp.message(Command("topmemes"))
async def top_memes(message: Message):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT file_id, rating FROM memes ORDER BY rating DESC LIMIT 5")
            top = await cur.fetchall()
    if not top: await message.answer("Пока нет мемов с рейтингом."); return
    for file_id, rating in top:
        try: await bot.send_photo(message.from_user.id, file_id, caption=f"Рейтинг: {rating}")
        except: await message.answer(f"Мем удалён, рейтинг: {rating}")

# --- Поддержка ---
@dp.callback_query(F.data == "support")
async def support_callback(callback: types.CallbackQuery):
    await callback.message.answer(f"📞 <b>Техническая поддержка</b>\n\nСвяжитесь с администратором: {SUPPORT_CONTACT}", parse_mode="HTML")
    await callback.answer()

@dp.message(Command("support"))
async def support_command(message: Message):
    await message.answer(f"📞 <b>Техническая поддержка</b>\n\nСвяжитесь с администратором: {SUPPORT_CONTACT}", parse_mode="HTML")

# --- Помощь ---
@dp.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    txt = (
        "🤖 <b>Мемогенератор — инструкция</b>\n\n"
        "1. Нажми «Создать мем».\n2. Выбери категорию и шаблон.\n3. Введи верхний/нижний текст.\n"
        "4. Получи мем и оцени.\n\n"
        f"📌 Бесплатно: {MAX_FREE_MEMES} мема/день с водяным знаком.\n"
        f"💎 Премиум ({PREMIUM_PRICE_STARS} ⭐): безлимит, загрузка своих картинок, AI-мемы.\n\n"
        "Команды: /upload, /mystats, /topmemes, /support, /help, /adminhelp"
    )
    if await is_admin(callback.from_user): txt += "\n🔧 Вы администратор. Используйте /adminhelp."
    await callback.message.edit_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главная", callback_data="back_main")]]))

@dp.message(Command("help"))
async def cmd_help(message: Message):
    txt = (
        "🤖 <b>Мемогенератор — инструкция</b>\n\n"
        "1. Нажми «Создать мем».\n2. Выбери категорию и шаблон.\n3. Введи верхний/нижний текст.\n"
        "4. Получи мем и оцени.\n\n"
        f"Бесплатно: {MAX_FREE_MEMES} мема/день.\n"
        f"Премиум ({PREMIUM_PRICE_STARS} ⭐): безлимит, загрузка своих картинок, AI-мемы.\n\n"
        "Команды: /upload, /mystats, /topmemes, /support, /help, /adminhelp"
    )
    if await is_admin(message.from_user): txt += "\n🔧 Вы администратор. См. /adminhelp"
    await message.answer(txt, parse_mode="HTML")

@dp.message(Command("adminhelp"))
@admin_only
async def admin_help(message: Message):
    txt = (
        "🔧 <b>Команды администратора</b>\n\n"
        "/grant <code>@username</code> — выдать премиум\n"
        "/grantuser <code>@username</code> — то же самое\n"
        "/revoke <code>@username</code> — отозвать премиум\n"
        "/testpayment — тестовый платёж\n"
        "/addadmin <code>@username</code> — добавить админа\n"
        "/removeadmin <code>@username</code> — удалить админа\n"
        "/broadcast <code>текст</code> или reply + /broadcast — рассылка\n"
        "/stats — статистика пользователей\n"
        "/adminhelp — эта справка"
    )
    await message.answer(txt, parse_mode="HTML")

# --- Админские команды ---
@dp.message(Command("grant"))
@admin_only
async def grant_premium(message: Message):
    try: raw = message.text.split()[1]
    except: await message.answer("Использование: /grant @username"); return
    target = await resolve_user_id(raw)
    if target is None: await message.answer("❌ Пользователь не найден."); return
    await set_premium(target, True)
    await bot.send_message(target, "🎉 Вам выдан премиум!")
    await message.answer(f"✅ Премиум выдан пользователю {target}")

@dp.message(Command("grantuser"))
@admin_only
async def grant_user(message: Message):
    try: raw = message.text.split()[1]
    except: await message.answer("Использование: /grantuser @username"); return
    target = await resolve_user_id(raw)
    if target is None: await message.answer("❌ Пользователь не найден."); return
    await set_premium(target, True)
    await bot.send_message(target, "🎉 Вам выдан премиум!")
    await message.answer(f"✅ Премиум выдан @{raw.lstrip('@')}")

@dp.message(Command("revoke"))
@admin_only
async def revoke_premium(message: Message):
    try: raw = message.text.split()[1]
    except: await message.answer("Использование: /revoke @username"); return
    target = await resolve_user_id(raw)
    if target is None: await message.answer("❌ Пользователь не найден."); return
    await set_premium(target, False)
    await bot.send_message(target, "❌ Премиум отозван.")
    await message.answer(f"Премиум отозван у {target}")

@dp.message(Command("addadmin"))
@admin_only
async def add_admin_cmd(message: Message):
    try: raw = message.text.split()[1]
    except: await message.answer("Использование: /addadmin @username"); return
    target = await resolve_user_id(raw)
    if target is None: await message.answer("❌ Пользователь не найден."); return
    await add_admin_to_db(target)
    await message.answer(f"✅ Пользователь {target} добавлен в администраторы.")

@dp.message(Command("removeadmin"))
@admin_only
async def remove_admin_cmd(message: Message):
    try: raw = message.text.split()[1]
    except: await message.answer("Использование: /removeadmin @username"); return
    target = await resolve_user_id(raw)
    if target is None: await message.answer("❌ Пользователь не найден."); return
    await remove_admin_from_db(target)
    await message.answer(f"❌ Пользователь {target} удалён из администраторов.")

@dp.message(Command("stats"))
@admin_only
async def stats(message: Message):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*), SUM(premium) FROM users")
            total, prem = await cur.fetchone()
    await message.answer(f"👥 Пользователей: {total}, 💎 премиум: {prem or 0}")

@dp.message(Command("testpayment"))
@admin_only
async def test_payment(message: Message):
    await set_premium(message.from_user.id, True)
    await message.answer("🧪 Тестовый платёж выполнен! Премиум активирован для вас.")

@dp.message(Command("broadcast"))
@admin_only
async def broadcast(message: Message):
    if message.reply_to_message:
        target_msg = message.reply_to_message
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT user_id FROM users")
                rows = await cur.fetchall()
        succ = 0
        for uid in [r[0] for r in rows]:
            try:
                await bot.copy_message(uid, target_msg.chat.id, target_msg.message_id)
                succ += 1
            except: pass
        await message.answer(f"✅ Рассылка завершена: {succ}/{len(rows)}")
    else:
        text = message.text.split(maxsplit=1)
        if len(text) < 2:
            await message.answer("❌ Укажите текст после команды."); return
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT user_id FROM users")
                rows = await cur.fetchall()
        succ = 0
        for uid in [r[0] for r in rows]:
            try: await bot.send_message(uid, text[1]); succ += 1
            except: pass
        await message.answer(f"✅ Рассылка завершена: {succ}/{len(rows)}")

async def main():
    await create_db_pool()
    await init_db()
    await load_admins()
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
