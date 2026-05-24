import asyncio
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
EMPLOYEE_PHONE = os.getenv("EMPLOYEE_PHONE", "+998 XX XXX XX XX")

UZUM_AUTHORIZATION = os.getenv("UZUM_AUTHORIZATION")
UZUM_COOKIE = os.getenv("UZUM_COOKIE")

PAYMENT_CARD = os.getenv("PAYMENT_CARD", "0000 0000 0000 0000")
PAYMENT_OWNER = os.getenv("PAYMENT_OWNER", "Ism Familiya")

SUBSCRIPTION_PRICE = int(os.getenv("SUBSCRIPTION_PRICE", "20000"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_NAME = "review_bot.db"
UZ_TZ = timezone(timedelta(hours=5))

pending_payments = {}
pending_reviews = {}


# =========================
# DATABASE
# =========================

def now_dt():
    return datetime.now(UZ_TZ)


def now_text():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def db():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            store_id TEXT,
            store_name TEXT,
            subscription_until TEXT,
            auto_reply_enabled INTEGER DEFAULT 0,
            reply_mode TEXT DEFAULT 'template_confirm',
            template_1 TEXT,
            template_2 TEXT,
            template_3 TEXT,
            template_4 TEXT,
            template_5 TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS review_logs (
            review_id TEXT PRIMARY KEY,
            telegram_id INTEGER,
            store_id TEXT,
            rating INTEGER,
            status TEXT,
            reply_text TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            telegram_id INTEGER,
            store_row_id INTEGER,
            amount INTEGER,
            days INTEGER,
            status TEXT,
            created_at TEXT,
            approved_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def add_user(user):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (user.id,))
    exists = cur.fetchone()

    if exists:
        cur.execute(
            "UPDATE users SET full_name = ?, username = ? WHERE telegram_id = ?",
            (user.full_name or "", user.username or "", user.id)
        )
    else:
        cur.execute(
            """
            INSERT INTO users (telegram_id, full_name, username, is_blocked, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user.id, user.full_name or "", user.username or "", 0, now_text())
        )

    conn.commit()
    conn.close()


def is_blocked(telegram_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT is_blocked FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0] == 1)


def save_store(telegram_id, store_id, store_name):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM stores WHERE telegram_id = ? AND store_id = ?",
        (telegram_id, store_id)
    )
    exists = cur.fetchone()

    if exists:
        cur.execute(
            "UPDATE stores SET store_name = ? WHERE telegram_id = ? AND store_id = ?",
            (store_name, telegram_id, store_id)
        )
    else:
        cur.execute(
            """
            INSERT INTO stores (
                telegram_id, store_id, store_name, subscription_until,
                auto_reply_enabled, reply_mode, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                store_id,
                store_name,
                None,
                0,
                "template_confirm",
                now_text()
            )
        )

    conn.commit()
    conn.close()


def store_used_by_other_user(store_id, telegram_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT telegram_id FROM stores
        WHERE store_id = ? AND telegram_id != ?
        LIMIT 1
        """,
        (store_id, telegram_id)
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_user_stores(telegram_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, store_id, store_name, subscription_until,
               auto_reply_enabled, reply_mode
        FROM stores
        WHERE telegram_id = ?
        ORDER BY id DESC
        """,
        (telegram_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_store(row_id, telegram_id=None):
    conn = db()
    cur = conn.cursor()

    if telegram_id:
        cur.execute(
            """
            SELECT id, telegram_id, store_id, store_name, subscription_until,
                   auto_reply_enabled, reply_mode,
                   template_1, template_2, template_3, template_4, template_5
            FROM stores
            WHERE id = ? AND telegram_id = ?
            """,
            (row_id, telegram_id)
        )
    else:
        cur.execute(
            """
            SELECT id, telegram_id, store_id, store_name, subscription_until,
                   auto_reply_enabled, reply_mode,
                   template_1, template_2, template_3, template_4, template_5
            FROM stores
            WHERE id = ?
            """,
            (row_id,)
        )

    row = cur.fetchone()
    conn.close()
    return row


def get_store_by_shop_id(store_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, telegram_id, store_id, store_name, subscription_until,
               auto_reply_enabled, reply_mode,
               template_1, template_2, template_3, template_4, template_5
        FROM stores
        WHERE store_id = ?
        LIMIT 1
        """,
        (str(store_id),)
    )
    row = cur.fetchone()
    conn.close()
    return row


def update_store_name(row_id, store_name):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE stores SET store_name = ? WHERE id = ?", (store_name, row_id))
    conn.commit()
    conn.close()


def update_auto_reply(row_id, enabled):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE stores SET auto_reply_enabled = ? WHERE id = ?", (1 if enabled else 0, row_id))
    conn.commit()
    conn.close()


def update_reply_mode(row_id, mode):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE stores SET reply_mode = ? WHERE id = ?", (mode, row_id))
    conn.commit()
    conn.close()


def update_template(row_id, rating, text):
    conn = db()
    cur = conn.cursor()
    cur.execute(f"UPDATE stores SET template_{rating} = ? WHERE id = ?", (text, row_id))
    conn.commit()
    conn.close()


def extend_subscription(row_id):
    store = get_store(row_id)
    if not store:
        return None

    subscription_until = store[4]

    if subscription_until:
        try:
            old_until = datetime.fromisoformat(subscription_until)
        except Exception:
            old_until = now_dt()
    else:
        old_until = now_dt()

    start = old_until if old_until > now_dt() else now_dt()
    new_until = start + timedelta(days=SUBSCRIPTION_DAYS)

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE stores SET subscription_until = ? WHERE id = ?",
        (new_until.isoformat(), row_id)
    )
    conn.commit()
    conn.close()

    return new_until


def subscription_active(subscription_until):
    if not subscription_until:
        return False

    try:
        until = datetime.fromisoformat(subscription_until)
        return until > now_dt()
    except Exception:
        return False


def log_review(review_id, telegram_id, store_id, rating, status, reply_text=""):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO review_logs
        (review_id, telegram_id, store_id, rating, status, reply_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (str(review_id), telegram_id, str(store_id), rating, status, reply_text, now_text())
    )
    conn.commit()
    conn.close()


def review_already_logged(review_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT review_id FROM review_logs WHERE review_id = ?", (str(review_id),))
    row = cur.fetchone()
    conn.close()
    return bool(row)


def save_payment(payment_id, telegram_id, store_row_id, amount, days, status):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payments
        (payment_id, telegram_id, store_row_id, amount, days, status, created_at, approved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (payment_id, telegram_id, store_row_id, amount, days, status, now_text(), None)
    )
    conn.commit()
    conn.close()


def update_payment_status(payment_id, status):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE payments SET status = ?, approved_at = ? WHERE payment_id = ?",
        (status, now_text(), payment_id)
    )
    conn.commit()
    conn.close()


def get_stats():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM stores")
    stores_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM stores WHERE auto_reply_enabled = 1")
    active_auto = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM review_logs WHERE status = 'sent'")
    sent_count = cur.fetchone()[0]

    conn.close()
    return users_count, stores_count, active_auto, sent_count


# =========================
# UZUM API
# =========================

def uzum_headers():
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    }

    if UZUM_AUTHORIZATION:
        headers["Authorization"] = UZUM_AUTHORIZATION

    if UZUM_COOKIE:
        headers["Cookie"] = UZUM_COOKIE

    return headers


async def read_response(response):
    try:
        return await response.json(content_type=None)
    except Exception:
        text = await response.text()
        return {"raw": text[:1000]}


async def uzum_get_no_reply_reviews(page=0, size=20):
    url = f"https://api-seller.uzum.uz/api/seller/product-reviews?page={page}&size={size}"
    payload = {"filter": "NO_REPLY"}

    async with aiohttp.ClientSession(headers=uzum_headers()) as session:
        async with session.post(url, json=payload) as response:
            data = await read_response(response)

            if response.status != 200:
                raise Exception(f"Sharhlar ro‘yxatini olishda xato: {response.status}. Javob: {data}")

            return data.get("payload", [])


async def uzum_get_review_detail(review_id):
    url = f"https://api-seller.uzum.uz/api/seller/product-reviews/review/{review_id}"

    async with aiohttp.ClientSession(headers=uzum_headers()) as session:
        async with session.get(url) as response:
            data = await read_response(response)

            if response.status != 200:
                raise Exception(f"Sharh tafsilotini olishda xato: {response.status}. Javob: {data}")

            return data


async def uzum_reply_review(review_id, content):
    url = "https://api-seller.uzum.uz/api/seller/product-reviews/reply/create"

    payload = [
        {
            "reviewId": int(review_id),
            "content": content
        }
    ]

    async with aiohttp.ClientSession(headers=uzum_headers()) as session:
        async with session.post(url, json=payload) as response:
            data = await read_response(response)

            if response.status != 200:
                raise Exception(f"Sharhga javob yuborishda xato: {response.status}. Javob: {data}")

            return data


# =========================
# REVIEW HELPERS
# =========================

def get_nested(obj, *keys):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def extract_review_id(review):
    return (
        review.get("reviewId")
        or review.get("id")
        or review.get("review", {}).get("id")
    )


def extract_rating(review):
    return int(review.get("rating") or 5)


def extract_shop_id(review):
    shop = review.get("shop") or {}
    return shop.get("id") or review.get("shopId")


def extract_shop_title(review):
    shop = review.get("shop") or {}
    return shop.get("title") or shop.get("name")


def extract_product_name(review):
    product = review.get("product") or {}
    return (
        product.get("title")
        or product.get("name")
        or product.get("productTitle")
        or "mahsulot"
    )


def extract_review_text(review):
    parts = []

    for key in ["content", "text", "comment", "reviewText", "message"]:
        if review.get(key):
            parts.append(str(review.get(key)))

    if review.get("pros"):
        parts.append("Afzalliklari: " + str(review.get("pros")))

    if review.get("cons"):
        parts.append("Kamchiliklari: " + str(review.get("cons")))

    if review.get("commentText"):
        parts.append(str(review.get("commentText")))

    return "\n".join(parts).strip() or "Matnsiz sharh"


def default_template(rating):
    if rating >= 5:
        return "Xaridingiz va iliq fikringiz uchun rahmat! Sizga xizmat qilishdan mamnunmiz 😊"
    if rating == 4:
        return "Fikringiz uchun rahmat! Keyingi safar yanada yaxshiroq xizmat ko‘rsatishga harakat qilamiz."
    if rating == 3:
        return "Fikringiz uchun rahmat. Bildirgan mulohazalaringizni albatta inobatga olamiz."
    if rating == 2:
        return "Fikringiz uchun rahmat. Noqulaylik uchun uzr so‘raymiz, kamchiliklarni bartaraf etishga harakat qilamiz."
    return "Fikringiz uchun rahmat. Noqulaylik uchun uzr so‘raymiz. Holatni albatta inobatga olamiz."


def apply_variables(template, review, store_name):
    rating = extract_rating(review)
    product = extract_product_name(review)

    return (
        template
        .replace("{product}", product)
        .replace("{rating}", str(rating))
        .replace("{shop}", store_name)
    )


def template_for_store(store, review):
    rating = extract_rating(review)

    templates = {
        1: store[7],
        2: store[8],
        3: store[9],
        4: store[10],
        5: store[11],
    }

    template = templates.get(rating) or default_template(rating)
    return apply_variables(template, review, store[3])


async def ai_generate_reply(review, store_name):
    rating = extract_rating(review)
    text = extract_review_text(review)
    product = extract_product_name(review)

    if not OPENAI_API_KEY:
        return default_template(rating)

    prompt = (
        "Siz Uzum Market seller do‘koni nomidan xaridor sharhiga javob yozasiz.\n"
        "Javob o‘zbek tilida, muloyim, qisqa va professional bo‘lsin.\n"
        "Juda uzun yozmang. Emoji ko‘p ishlatmang.\n"
        "Agar sharh salbiy bo‘lsa, uzr so‘rang va mulohaza inobatga olinishini ayting.\n\n"
        f"Do‘kon: {store_name}\n"
        f"Mahsulot: {product}\n"
        f"Baholash: {rating} yulduz\n"
        f"Sharh: {text}\n"
    )

    url = "https://api.openai.com/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "Siz mijoz sharhlariga sotuvchi nomidan javob yozadigan yordamchisiz."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 180
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as response:
                data = await read_response(response)

                if response.status != 200:
                    return default_template(rating)

                return data["choices"][0]["message"]["content"].strip()

    except Exception:
        return default_template(rating)


async def build_reply_text(store, review):
    mode = store[6]

    if mode.startswith("ai"):
        return await ai_generate_reply(review, store[3])

    return template_for_store(store, review)


def needs_confirmation(mode):
    return mode.endswith("_confirm")


# =========================
# KEYBOARDS
# =========================

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🏬 Do‘konlarim"),
                KeyboardButton(text="➕ Do‘kon qo‘shish"),
            ],
            [
                KeyboardButton(text="💳 Obuna"),
                KeyboardButton(text="🔍 Javobsiz sharhlar"),
            ],
            [
                KeyboardButton(text="▶️ Avto-javobni yoqish"),
                KeyboardButton(text="⏸ Avto-javobni to‘xtatish"),
            ],
            [
                KeyboardButton(text="⚙️ Javob rejimi"),
                KeyboardButton(text="📝 Shablonlar"),
            ],
            [
                KeyboardButton(text="📊 Statistika"),
            ],
        ],
        resize_keyboard=True
    )


def stores_keyboard(stores, prefix):
    kb = InlineKeyboardBuilder()

    for row in stores:
        row_id, store_id, store_name, subscription_until, auto_enabled, mode = row
        sub = "✅" if subscription_active(subscription_until) else "❌"
        auto = "🟢" if auto_enabled else "⚪"
        kb.button(
            text=f"{sub} {auto} {store_name}",
            callback_data=f"{prefix}:{row_id}"
        )

    kb.adjust(1)
    return kb.as_markup()


def mode_keyboard(row_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Shablon + avtomatik", callback_data=f"set_mode:{row_id}:template_auto")
    kb.button(text="👀 Shablon + tasdiqlash", callback_data=f"set_mode:{row_id}:template_confirm")
    kb.button(text="🤖 AI + tasdiqlash", callback_data=f"set_mode:{row_id}:ai_confirm")
    kb.button(text="🤖 AI + avtomatik", callback_data=f"set_mode:{row_id}:ai_auto")
    kb.adjust(1)
    return kb.as_markup()


def payment_admin_keyboard(payment_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data=f"pay_ok:{payment_id}")
    kb.button(text="❌ Rad etish", callback_data=f"pay_no:{payment_id}")
    kb.adjust(2)
    return kb.as_markup()


def review_confirm_keyboard(pending_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Yuborish", callback_data=f"review_send:{pending_id}")
    kb.button(text="✏️ O‘zgartirish", callback_data=f"review_edit:{pending_id}")
    kb.button(text="❌ Yubormaslik", callback_data=f"review_skip:{pending_id}")
    kb.adjust(1)
    return kb.as_markup()


# =========================
# STATES
# =========================

class AddStoreState(StatesGroup):
    waiting_store_id = State()


class PaymentState(StatesGroup):
    waiting_receipt = State()


class TemplateState(StatesGroup):
    waiting_text = State()


class EditReviewState(StatesGroup):
    waiting_text = State()


# =========================
# START
# =========================

START_TEXT = (
    "⭐ Uzum Sharh Bot\n\n"
    "Uzum Seller sharhlariga avtomatik javob beruvchi bot.\n\n"
    "✅ Har bir do‘kon uchun alohida obuna\n"
    "✅ Har xil yulduzli sharhlar uchun alohida shablon\n"
    "✅ Xohlasangiz AI javob yozadi\n"
    "✅ Xohlasangiz javobni avval siz tasdiqlaysiz\n\n"
    "💰 1 ta do‘kon uchun 30 kunlik obuna: 20 000 so‘m"
)


@dp.message(CommandStart())
async def start(message: Message):
    add_user(message.from_user)

    if is_blocked(message.from_user.id):
        await message.answer("Siz botdan foydalanishdan bloklangansiz.")
        return

    await message.answer(START_TEXT, reply_markup=main_menu())


@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Sizning Telegram ID: {message.from_user.id}")


@dp.message(Command("admin"))
async def admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Siz admin emassiz.")
        return

    users_count, stores_count, active_auto, sent_count = get_stats()

    await message.answer(
        "📊 Admin panel\n\n"
        f"👥 Userlar: {users_count}\n"
        f"🏪 Do‘konlar: {stores_count}\n"
        f"🟢 Avto-javob yoqilgan: {active_auto}\n"
        f"✅ Yuborilgan javoblar: {sent_count}"
    )


# =========================
# STORE
# =========================

@dp.message(F.text == "➕ Do‘kon qo‘shish")
async def add_store_start(message: Message, state: FSMContext):
    add_user(message.from_user)

    await message.answer(
        "🏪 Do‘kon qo‘shish\n\n"
        "Avval Uzum Seller’da bot xodimini do‘koningizga qo‘shing.\n\n"
        f"📞 Xodim telefon raqami: {EMPLOYEE_PHONE}\n\n"
        "Keyin do‘kon ID raqamini yuboring."
    )

    await state.set_state(AddStoreState.waiting_store_id)


@dp.message(AddStoreState.waiting_store_id)
async def add_store_id(message: Message, state: FSMContext):
    store_id = message.text.strip()

    if not store_id.isdigit():
        await message.answer("Do‘kon ID faqat raqam bo‘lishi kerak. Qayta yuboring:")
        return

    used = store_used_by_other_user(store_id, message.from_user.id)

    if used:
        await message.answer(
            "❌ Bu do‘kon boshqa Telegram accountga ulangan.\n\n"
            "Agar bu sizning do‘koningiz bo‘lsa, admin bilan bog‘laning."
        )
        await state.clear()
        return

    store_name = f"Do‘kon {store_id}"
    save_store(message.from_user.id, store_id, store_name)

    await message.answer(
        "✅ Do‘kon qo‘shildi!\n\n"
        f"🏪 {store_name}\n"
        f"🆔 {store_id}\n\n"
        "Endi shu do‘kon uchun obuna sotib oling.",
        reply_markup=main_menu()
    )

    await state.clear()


@dp.message(F.text == "🏬 Do‘konlarim")
async def my_stores(message: Message):
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Sizda hali do‘kon yo‘q.", reply_markup=main_menu())
        return

    text = "🏬 Do‘konlaringiz:\n\n"

    for row in stores:
        row_id, store_id, store_name, subscription_until, auto_enabled, mode = row

        if subscription_active(subscription_until):
            until = datetime.fromisoformat(subscription_until).strftime("%d.%m.%Y")
            sub_text = f"✅ Faol, {until} gacha"
        else:
            sub_text = "❌ Obuna faol emas"

        auto_text = "🟢 Yoqilgan" if auto_enabled else "⚪ O‘chirilgan"

        text += (
            f"🏪 {store_name}\n"
            f"🆔 {store_id}\n"
            f"💳 Obuna: {sub_text}\n"
            f"▶️ Avto-javob: {auto_text}\n"
            f"⚙️ Rejim: {mode}\n\n"
        )

    await message.answer(text, reply_markup=main_menu())


# =========================
# SUBSCRIPTION PAYMENT
# =========================

@dp.message(F.text == "💳 Obuna")
async def subscription_menu(message: Message):
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Avval do‘kon qo‘shing.", reply_markup=main_menu())
        return

    await message.answer(
        "Qaysi do‘kon uchun 30 kunlik obuna olamiz?\n\n"
        f"💰 Narx: {SUBSCRIPTION_PRICE:,} so‘m".replace(",", " "),
        reply_markup=stores_keyboard(stores, "sub_store")
    )


@dp.callback_query(F.data.startswith("sub_store:"))
async def subscription_store_selected(callback: CallbackQuery, state: FSMContext):
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    await state.update_data(store_row_id=row_id)

    price_text = f"{SUBSCRIPTION_PRICE:,}".replace(",", " ")

    await callback.message.answer(
        "💳 To‘lov ma’lumotlari:\n\n"
        f"🏪 Do‘kon: {store[3]}\n"
        f"🆔 Do‘kon ID: {store[2]}\n"
        f"📅 Muddat: {SUBSCRIPTION_DAYS} kun\n"
        f"💰 Summa: {price_text} so‘m\n\n"
        f"💳 Karta: {PAYMENT_CARD}\n"
        f"👤 Karta egasi: {PAYMENT_OWNER}\n\n"
        "To‘lovdan so‘ng chek rasmini shu yerga yuboring."
    )

    await state.set_state(PaymentState.waiting_receipt)
    await callback.answer()


@dp.message(PaymentState.waiting_receipt)
async def receive_payment_receipt(message: Message, state: FSMContext):
    if not (message.photo or message.document):
        await message.answer("Iltimos, chek rasmini yoki faylini yuboring.")
        return

    data = await state.get_data()
    store_row_id = data.get("store_row_id")
    store = get_store(store_row_id, message.from_user.id)

    if not store:
        await message.answer("Do‘kon topilmadi. Qayta urinib ko‘ring.")
        await state.clear()
        return

    payment_id = uuid.uuid4().hex[:8]

    pending_payments[payment_id] = {
        "telegram_id": message.from_user.id,
        "store_row_id": store_row_id,
        "store_name": store[3],
        "store_id": store[2],
    }

    save_payment(
        payment_id,
        message.from_user.id,
        store_row_id,
        SUBSCRIPTION_PRICE,
        SUBSCRIPTION_DAYS,
        "pending"
    )

    await message.answer(
        "✅ Chek adminga yuborildi.\n"
        "Tasdiqlangandan so‘ng obuna faollashadi.",
        reply_markup=main_menu()
    )

    if ADMIN_ID:
        price_text = f"{SUBSCRIPTION_PRICE:,}".replace(",", " ")

        await bot.send_message(
            ADMIN_ID,
            "🧾 Yangi obuna to‘lovi\n\n"
            f"Payment ID: {payment_id}\n"
            f"👤 User: {message.from_user.full_name}\n"
            f"🆔 User ID: {message.from_user.id}\n"
            f"🏪 Do‘kon: {store[3]}\n"
            f"🆔 Do‘kon ID: {store[2]}\n"
            f"💰 Summa: {price_text} so‘m\n"
            f"📅 Muddat: {SUBSCRIPTION_DAYS} kun",
            reply_markup=payment_admin_keyboard(payment_id)
        )

        try:
            await bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
        except Exception:
            pass

    await state.clear()


@dp.callback_query(F.data.startswith("pay_ok:"))
async def payment_ok(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return

    payment_id = callback.data.split(":")[1]
    payment = pending_payments.pop(payment_id, None)

    if not payment:
        await callback.answer("To‘lov topilmadi yoki allaqachon ko‘rilgan.", show_alert=True)
        return

    new_until = extend_subscription(payment["store_row_id"])
    update_payment_status(payment_id, "approved")

    until_text = new_until.strftime("%d.%m.%Y") if new_until else "-"

    await callback.message.answer(
        "✅ To‘lov tasdiqlandi.\n\n"
        f"🏪 Do‘kon: {payment['store_name']}\n"
        f"📅 Obuna muddati: {until_text} gacha"
    )

    try:
        await bot.send_message(
            payment["telegram_id"],
            "✅ Obuna faollashtirildi!\n\n"
            f"🏪 Do‘kon: {payment['store_name']}\n"
            f"📅 Amal qilish muddati: {until_text} gacha",
            reply_markup=main_menu()
        )
    except Exception:
        pass

    await callback.answer()


@dp.callback_query(F.data.startswith("pay_no:"))
async def payment_no(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return

    payment_id = callback.data.split(":")[1]
    payment = pending_payments.pop(payment_id, None)

    if not payment:
        await callback.answer("To‘lov topilmadi yoki allaqachon ko‘rilgan.", show_alert=True)
        return

    update_payment_status(payment_id, "rejected")

    await callback.message.answer("❌ To‘lov rad etildi.")

    try:
        await bot.send_message(
            payment["telegram_id"],
            "❌ To‘lov rad etildi.\n\n"
            "Agar xato bo‘lsa, qayta chek yuboring.",
            reply_markup=main_menu()
        )
    except Exception:
        pass

    await callback.answer()


# =========================
# REPLY MODE
# =========================

@dp.message(F.text == "⚙️ Javob rejimi")
async def reply_mode_menu(message: Message):
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Avval do‘kon qo‘shing.", reply_markup=main_menu())
        return

    await message.answer(
        "Qaysi do‘kon uchun javob rejimini o‘zgartiramiz?",
        reply_markup=stores_keyboard(stores, "mode_store")
    )


@dp.callback_query(F.data.startswith("mode_store:"))
async def mode_store(callback: CallbackQuery):
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    await callback.message.answer(
        f"⚙️ Javob rejimi\n\n"
        f"🏪 Do‘kon: {store[3]}\n\n"
        "Rejimni tanlang:",
        reply_markup=mode_keyboard(row_id)
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("set_mode:"))
async def set_mode(callback: CallbackQuery):
    _, row_id, mode = callback.data.split(":")
    row_id = int(row_id)

    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    update_reply_mode(row_id, mode)

    mode_names = {
        "template_auto": "✍️ Shablon + avtomatik",
        "template_confirm": "👀 Shablon + tasdiqlash",
        "ai_confirm": "🤖 AI + tasdiqlash",
        "ai_auto": "🤖 AI + avtomatik",
    }

    await callback.message.answer(
        "✅ Javob rejimi o‘zgartirildi.\n\n"
        f"🏪 Do‘kon: {store[3]}\n"
        f"⚙️ Rejim: {mode_names.get(mode, mode)}",
        reply_markup=main_menu()
    )

    await callback.answer()


# =========================
# TEMPLATES
# =========================

@dp.message(F.text == "📝 Shablonlar")
async def templates_menu(message: Message):
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Avval do‘kon qo‘shing.", reply_markup=main_menu())
        return

    await message.answer(
        "Qaysi do‘kon uchun shablonlarni sozlaymiz?",
        reply_markup=stores_keyboard(stores, "template_store")
    )


@dp.callback_query(F.data.startswith("template_store:"))
async def template_store(callback: CallbackQuery, state: FSMContext):
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    await state.update_data(store_row_id=row_id, rating=5)

    await callback.message.answer(
        f"📝 Shablon sozlash\n\n"
        f"🏪 Do‘kon: {store[3]}\n\n"
        "⭐ 5 yulduzli sharhlar uchun qanday javob yuboramiz?\n\n"
        "Masalan:\n"
        "Xaridingiz uchun rahmat! Sizga xizmat qilishdan mamnunmiz 😊\n\n"
        "O‘zgaruvchilar:\n"
        "{product} — mahsulot nomi\n"
        "{rating} — yulduz soni\n"
        "{shop} — do‘kon nomi"
    )

    await state.set_state(TemplateState.waiting_text)
    await callback.answer()


@dp.message(TemplateState.waiting_text)
async def template_text(message: Message, state: FSMContext):
    data = await state.get_data()
    row_id = data.get("store_row_id")
    rating = data.get("rating")

    update_template(row_id, rating, message.text.strip())

    next_rating = rating - 1

    if next_rating >= 1:
        await state.update_data(rating=next_rating)
        await message.answer(
            f"✅ {rating} yulduz uchun shablon saqlandi.\n\n"
            f"⭐ {next_rating} yulduzli sharhlar uchun qanday javob yuboramiz?"
        )
        return

    await message.answer(
        "✅ Barcha shablonlar saqlandi.\n\n"
        "Endi bot ratingga qarab shu shablonlarni ishlatadi.",
        reply_markup=main_menu()
    )
    await state.clear()


# =========================
# AUTO REPLY ON/OFF
# =========================

@dp.message(F.text == "▶️ Avto-javobni yoqish")
async def enable_auto_menu(message: Message):
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Avval do‘kon qo‘shing.", reply_markup=main_menu())
        return

    await message.answer(
        "Qaysi do‘kon uchun avto-javobni yoqamiz?",
        reply_markup=stores_keyboard(stores, "auto_on")
    )


@dp.callback_query(F.data.startswith("auto_on:"))
async def auto_on(callback: CallbackQuery):
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    if not subscription_active(store[4]):
        await callback.message.answer(
            "❌ Bu do‘kon uchun obuna faol emas.\n\n"
            "Avval obuna sotib oling.",
            reply_markup=main_menu()
        )
        await callback.answer()
        return

    update_auto_reply(row_id, True)

    await callback.message.answer(
        "✅ Avto-javob yoqildi.\n\n"
        f"🏪 Do‘kon: {store[3]}",
        reply_markup=main_menu()
    )
    await callback.answer()


@dp.message(F.text == "⏸ Avto-javobni to‘xtatish")
async def disable_auto_menu(message: Message):
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Avval do‘kon qo‘shing.", reply_markup=main_menu())
        return

    await message.answer(
        "Qaysi do‘kon uchun avto-javobni to‘xtatamiz?",
        reply_markup=stores_keyboard(stores, "auto_off")
    )


@dp.callback_query(F.data.startswith("auto_off:"))
async def auto_off(callback: CallbackQuery):
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    update_auto_reply(row_id, False)

    await callback.message.answer(
        "⏸ Avto-javob to‘xtatildi.\n\n"
        f"🏪 Do‘kon: {store[3]}",
        reply_markup=main_menu()
    )
    await callback.answer()


# =========================
# MANUAL CHECK REVIEWS
# =========================

@dp.message(F.text == "🔍 Javobsiz sharhlar")
async def manual_no_reply_reviews(message: Message):
    await message.answer("🔄 Javobsiz sharhlar tekshirilmoqda...")

    try:
        reviews = await uzum_get_no_reply_reviews(page=0, size=20)
    except Exception as e:
        await message.answer(f"❌ Xato: {e}")
        return

    user_store_ids = {row[1]: row for row in get_user_stores(message.from_user.id)}

    found = 0

    for review in reviews:
        review_id = extract_review_id(review)
        shop_id = str(extract_shop_id(review))

        if shop_id not in user_store_ids:
            continue

        store = get_store_by_shop_id(shop_id)

        if not store:
            continue

        rating = extract_rating(review)
        text = extract_review_text(review)
        product = extract_product_name(review)

        found += 1

        await message.answer(
            f"⭐ {rating} yulduzli javobsiz sharh\n\n"
            f"🏪 Do‘kon: {store[3]}\n"
            f"📦 Mahsulot: {product}\n"
            f"🆔 Review ID: {review_id}\n\n"
            f"💬 Sharh:\n{text}"
        )

        if found >= 5:
            break

    if found == 0:
        await message.answer("Hozircha sizning do‘konlaringizda javobsiz sharh topilmadi.")


# =========================
# REVIEW CONFIRM CALLBACKS
# =========================

@dp.callback_query(F.data.startswith("review_send:"))
async def review_send(callback: CallbackQuery):
    pending_id = callback.data.split(":")[1]
    item = pending_reviews.pop(pending_id, None)

    if not item:
        await callback.answer("Bu javob topilmadi yoki muddati tugagan.", show_alert=True)
        return

    try:
        await uzum_reply_review(item["review_id"], item["reply_text"])
        log_review(
            item["review_id"],
            item["telegram_id"],
            item["store_id"],
            item["rating"],
            "sent",
            item["reply_text"]
        )

        await callback.message.answer(
            "✅ Javob Uzum Seller’ga yuborildi.\n\n"
            f"🆔 Review ID: {item['review_id']}"
        )
    except Exception as e:
        await callback.message.answer(f"❌ Javob yuborishda xato: {e}")

    await callback.answer()


@dp.callback_query(F.data.startswith("review_skip:"))
async def review_skip(callback: CallbackQuery):
    pending_id = callback.data.split(":")[1]
    item = pending_reviews.pop(pending_id, None)

    if item:
        log_review(
            item["review_id"],
            item["telegram_id"],
            item["store_id"],
            item["rating"],
            "skipped",
            item["reply_text"]
        )

    await callback.message.answer("❌ Javob yuborilmadi.")
    await callback.answer()


@dp.callback_query(F.data.startswith("review_edit:"))
async def review_edit(callback: CallbackQuery, state: FSMContext):
    pending_id = callback.data.split(":")[1]

    if pending_id not in pending_reviews:
        await callback.answer("Bu javob topilmadi yoki muddati tugagan.", show_alert=True)
        return

    await state.update_data(pending_id=pending_id)

    await callback.message.answer(
        "✏️ Yangi javob matnini yuboring.\n\n"
        "Bot shu matnni Uzum Seller’ga yuboradi."
    )

    await state.set_state(EditReviewState.waiting_text)
    await callback.answer()


@dp.message(EditReviewState.waiting_text)
async def edit_review_text(message: Message, state: FSMContext):
    data = await state.get_data()
    pending_id = data.get("pending_id")

    item = pending_reviews.get(pending_id)

    if not item:
        await message.answer("Bu javob topilmadi yoki muddati tugagan.")
        await state.clear()
        return

    item["reply_text"] = message.text.strip()

    await message.answer(
        "Yangi javob:\n\n"
        f"{item['reply_text']}\n\n"
        "Yuboramizmi?",
        reply_markup=review_confirm_keyboard(pending_id)
    )

    await state.clear()


# =========================
# STATISTICS
# =========================

@dp.message(F.text == "📊 Statistika")
async def user_stats(message: Message):
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Sizda hali do‘kon yo‘q.", reply_markup=main_menu())
        return

    text = "📊 Sizning statistikangiz:\n\n"

    conn = db()
    cur = conn.cursor()

    for row in stores:
        row_id, store_id, store_name, subscription_until, auto_enabled, mode = row

        cur.execute(
            "SELECT COUNT(*) FROM review_logs WHERE telegram_id = ? AND store_id = ? AND status = 'sent'",
            (message.from_user.id, str(store_id))
        )
        sent = cur.fetchone()[0]

        sub = "✅ Faol" if subscription_active(subscription_until) else "❌ Faol emas"
        auto = "🟢 Yoqilgan" if auto_enabled else "⚪ O‘chirilgan"

        text += (
            f"🏪 {store_name}\n"
            f"💳 Obuna: {sub}\n"
            f"▶️ Avto-javob: {auto}\n"
            f"✅ Yuborilgan javoblar: {sent}\n\n"
        )

    conn.close()

    await message.answer(text, reply_markup=main_menu())


# =========================
# BACKGROUND WATCHER
# =========================

async def process_one_review(review):
    review_id = extract_review_id(review)

    if not review_id:
        return

    if review_already_logged(review_id):
        return

    shop_id = extract_shop_id(review)

    if not shop_id:
        return

    store = get_store_by_shop_id(str(shop_id))

    if not store:
        return

    row_id = store[0]
    telegram_id = store[1]
    store_id = store[2]
    store_name = store[3]
    subscription_until = store[4]
    auto_enabled = store[5]
    mode = store[6]

    if not subscription_active(subscription_until):
        if auto_enabled:
            update_auto_reply(row_id, False)
            try:
                await bot.send_message(
                    telegram_id,
                    "⛔ Obuna muddati tugadi.\n\n"
                    f"🏪 Do‘kon: {store_name}\n"
                    "Avto-javob to‘xtatildi.",
                    reply_markup=main_menu()
                )
            except Exception:
                pass
        return

    if not auto_enabled:
        return

    detail = review

    try:
        detail = await uzum_get_review_detail(review_id)
    except Exception:
        pass

    shop_title = extract_shop_title(detail)
    if shop_title and store_name.startswith("Do‘kon "):
        update_store_name(row_id, shop_title)
        store = get_store(row_id)

    rating = extract_rating(detail)
    review_text = extract_review_text(detail)
    product = extract_product_name(detail)
    reply_text = await build_reply_text(store, detail)

    if needs_confirmation(mode):
        pending_id = uuid.uuid4().hex[:8]

        pending_reviews[pending_id] = {
            "review_id": review_id,
            "telegram_id": telegram_id,
            "store_id": store_id,
            "rating": rating,
            "reply_text": reply_text,
        }

        log_review(review_id, telegram_id, store_id, rating, "pending", reply_text)

        try:
            await bot.send_message(
                telegram_id,
                "🆕 Yangi javobsiz sharh topildi\n\n"
                f"🏪 Do‘kon: {store[3]}\n"
                f"📦 Mahsulot: {product}\n"
                f"⭐ Baho: {rating}\n"
                f"🆔 Review ID: {review_id}\n\n"
                f"💬 Sharh:\n{review_text}\n\n"
                f"✍️ Bot javobi:\n{reply_text}",
                reply_markup=review_confirm_keyboard(pending_id)
            )
        except Exception:
            pass

        return

    try:
        await uzum_reply_review(review_id, reply_text)

        log_review(review_id, telegram_id, store_id, rating, "sent", reply_text)

        await bot.send_message(
            telegram_id,
            "✅ Sharhga javob avtomatik yuborildi\n\n"
            f"🏪 Do‘kon: {store[3]}\n"
            f"⭐ Baho: {rating}\n"
            f"🆔 Review ID: {review_id}\n\n"
            f"✍️ Javob:\n{reply_text}"
        )

    except Exception as e:
        log_review(review_id, telegram_id, store_id, rating, "error", str(e))


async def check_reviews_once():
    reviews = await uzum_get_no_reply_reviews(page=0, size=20)

    for review in reviews:
        await process_one_review(review)
        await asyncio.sleep(0.3)


async def review_watcher():
    await asyncio.sleep(5)

    while True:
        try:
            await check_reviews_once()
        except Exception as e:
            print("review_watcher error:", e)

        await asyncio.sleep(CHECK_INTERVAL)


# =========================
# RUN
# =========================

async def main():
    init_db()
    asyncio.create_task(review_watcher())
    print("Uzum Sharh Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
