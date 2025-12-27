import os
import re
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import aiosqlite
from dotenv import load_dotenv

# -------------------- CONFIG --------------------
load_dotenv()  # локально .env, на Fly просто игнорируется

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/data/shop.db").strip()  # для Fly правильно так

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN пуст. На Fly: flyctl secrets set BOT_TOKEN=...")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop_bot")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()
router = Router()
dp.include_router(router)


# -------------------- DB HELPERS --------------------
async def db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    await conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = aiosqlite.Row
    return conn


DEFAULT_TEXTS: Dict[str, str] = {
    # Пользовательские тексты
    "start_text": (
        "👋 Привет!\n\n"
        "Здесь ты можешь выбрать категорию и купить товар.\n"
        "Если нужна помощь — нажми <b>Поддержка</b>."
    ),
    "support_text": "🆘 Поддержка\nНапишите менеджеру: @your_manager_username",
    "group_welcome_text": (
        "👋 Добро пожаловать!\n"
        "Чтобы открыть магазин — нажмите кнопку ниже 👇"
    ),
    "group_welcome_button": "🛒 Открыть магазин",

    # Заголовки/подсказки
    "choose_category": "📦 Выберите категорию:",
    "choose_subcategory": "📁 Выберите подкатегорию:",
    "choose_product": "🛍️ Выберите товар:",
    "no_items": "Пока ничего нет 🙃",
    "buy_btn": "✅ Купить",
    "back_btn": "⬅️ Назад",
    "home_btn": "🏠 В меню",
    "admin_btn": "⚙️ Админ панель",
}

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MOD = "mod"
ROLE_USER = "user"


async def init_db():
    conn = await db()
    try:
        await conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS staff(
            user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL CHECK(role IN ('owner','admin','mod'))
        );

        CREATE TABLE IF NOT EXISTS categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            pos INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS subcategories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            pos INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subcategory_id INTEGER NOT NULL REFERENCES subcategories(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price TEXT DEFAULT '',
            media_type TEXT DEFAULT '',   -- 'photo'/'video'/''
            media_file_id TEXT DEFAULT '',
            pos INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS buy_methods(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            pos INTEGER NOT NULL DEFAULT 0
        );
        """)
        await conn.commit()

        # settings defaults
        for k, v in DEFAULT_TEXTS.items():
            await conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)",
                (k, v)
            )
        await conn.commit()

        # owner default
        if OWNER_ID:
            await conn.execute(
                "INSERT OR IGNORE INTO staff(user_id, role) VALUES(?,?)",
                (OWNER_ID, ROLE_OWNER)
            )
            await conn.commit()
    finally:
        await conn.close()


async def get_setting(key: str) -> str:
    conn = await db()
    try:
        cur = await conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else DEFAULT_TEXTS.get(key, "")
    finally:
        await conn.close()


async def set_setting(key: str, value: str):
    conn = await db()
    try:
        await conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_staff_role(user_id: int) -> str:
    if OWNER_ID and user_id == OWNER_ID:
        return ROLE_OWNER
    conn = await db()
    try:
        cur = await conn.execute("SELECT role FROM staff WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row["role"] if row else ROLE_USER
    finally:
        await conn.close()


def role_at_least(role: str, min_role: str) -> bool:
    order = {ROLE_USER: 0, ROLE_MOD: 1, ROLE_ADMIN: 2, ROLE_OWNER: 3}
    return order.get(role, 0) >= order.get(min_role, 0)


# -------------------- UI HELPERS --------------------
def kb_home(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛍️ Открыть магазин", callback_data="shop:home")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")]
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="⚙️ Админ панель", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back(to: str = "shop:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=to)],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="home")]
    ])


def kb_only_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В меню", callback_data="home")]
    ])


async def safe_edit_text(msg: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await msg.answer(text, reply_markup=reply_markup)


# -------------------- SHOP QUERIES --------------------
async def list_categories() -> List[aiosqlite.Row]:
    conn = await db()
    try:
        cur = await conn.execute("SELECT id, name FROM categories ORDER BY pos, id")
        return await cur.fetchall()
    finally:
        await conn.close()


async def list_subcategories(category_id: int) -> List[aiosqlite.Row]:
    conn = await db()
    try:
        cur = await conn.execute(
            "SELECT id, name FROM subcategories WHERE category_id=? ORDER BY pos, id",
            (category_id,)
        )
        return await cur.fetchall()
    finally:
        await conn.close()


async def list_products(subcategory_id: int) -> List[aiosqlite.Row]:
    conn = await db()
    try:
        cur = await conn.execute(
            "SELECT id, name FROM products WHERE subcategory_id=? ORDER BY pos, id",
            (subcategory_id,)
        )
        return await cur.fetchall()
    finally:
        await conn.close()


async def get_product(product_id: int) -> Optional[aiosqlite.Row]:
    conn = await db()
    try:
        cur = await conn.execute("SELECT * FROM products WHERE id=?", (product_id,))
        return await cur.fetchone()
    finally:
        await conn.close()


async def list_buy_methods(product_id: int) -> List[aiosqlite.Row]:
    conn = await db()
    try:
        cur = await conn.execute(
            "SELECT id, title, url FROM buy_methods WHERE product_id=? ORDER BY pos, id",
            (product_id,)
        )
        return await cur.fetchall()
    finally:
        await conn.close()


# -------------------- ADMIN QUERIES --------------------
async def staff_list() -> List[aiosqlite.Row]:
    conn = await db()
    try:
        cur = await conn.execute("SELECT user_id, role FROM staff ORDER BY role DESC, user_id ASC")
        return await cur.fetchall()
    finally:
        await conn.close()


async def staff_set_role(user_id: int, role: str):
    conn = await db()
    try:
        await conn.execute(
            "INSERT INTO staff(user_id, role) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",
            (user_id, role)
        )
        await conn.commit()
    finally:
        await conn.close()


async def staff_remove(user_id: int):
    conn = await db()
    try:
        await conn.execute("DELETE FROM staff WHERE user_id=?", (user_id,))
        await conn.commit()
    finally:
        await conn.close()


# -------------------- STATES --------------------
class AdminAddCategory(StatesGroup):
    name = State()


class AdminEditCategory(StatesGroup):
    category_id = State()
    name = State()


class AdminAddSubcategory(StatesGroup):
    category_id = State()
    name = State()


class AdminEditSubcategory(StatesGroup):
    subcategory_id = State()
    name = State()


class AdminAddProduct(StatesGroup):
    subcategory_id = State()
    name = State()
    description = State()
    price = State()
    media = State()


class AdminEditProduct(StatesGroup):
    product_id = State()
    field = State()
    value = State()
    media = State()


class AdminAddBuyMethod(StatesGroup):
    product_id = State()
    title = State()
    url = State()


class AdminEditBuyMethod(StatesGroup):
    method_id = State()
    title = State()
    url = State()


class EditTexts(StatesGroup):
    key = State()
    value = State()


class StaffAdd(StatesGroup):
    role = State()
    user_id = State()


# -------------------- COMMANDS --------------------
@router.message(CommandStart())
async def cmd_start(m: Message):
    role = await get_staff_role(m.from_user.id)
    is_admin = role_at_least(role, ROLE_MOD)
    text = await get_setting("start_text")
    await m.answer(text, reply_markup=kb_home(is_admin))


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await m.answer("⛔ Доступ запрещён.")
    await m.answer("⚙️ Админ панель", reply_markup=admin_home_kb(role))


# -------------------- GROUP WELCOME --------------------
def open_shop_kb(btn_text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_text, url=f"https://t.me/{(bot.username or '').lstrip('@')}")]
    ])


@router.message(F.new_chat_members)
async def on_new_members(m: Message):
    # Приветствие в группе
    welcome = await get_setting("group_welcome_text")
    btn_text = await get_setting("group_welcome_button")
    await m.reply(welcome, reply_markup=open_shop_kb(btn_text))


# -------------------- MAIN HOME --------------------
@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    is_admin = role_at_least(role, ROLE_MOD)
    text = await get_setting("start_text")
    await safe_edit_text(c.message, text, reply_markup=kb_home(is_admin))
    await c.answer()


@router.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    text = await get_setting("support_text")
    await safe_edit_text(c.message, text, reply_markup=kb_only_home())
    await c.answer()


# -------------------- SHOP FLOW --------------------
@router.callback_query(F.data == "shop:home")
async def shop_home(c: CallbackQuery):
    cats = await list_categories()
    if not cats:
        await safe_edit_text(c.message, await get_setting("no_items"), reply_markup=kb_only_home())
        return await c.answer()
    rows = []
    for r in cats:
        rows.append([InlineKeyboardButton(text=r["name"], callback_data=f"shop:cat:{r['id']}")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_edit_text(c.message, await get_setting("choose_category"), reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("shop:cat:"))
async def shop_category(c: CallbackQuery):
    category_id = int(c.data.split(":")[-1])
    subs = await list_subcategories(category_id)
    if not subs:
        await safe_edit_text(c.message, await get_setting("no_items"), reply_markup=kb_back("shop:home"))
        return await c.answer()
    rows = []
    for r in subs:
        rows.append([InlineKeyboardButton(text=r["name"], callback_data=f"shop:sub:{r['id']}:{category_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="shop:home")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_edit_text(c.message, await get_setting("choose_subcategory"), reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("shop:sub:"))
async def shop_subcategory(c: CallbackQuery):
    _, _, sub_id, cat_id = c.data.split(":")
    sub_id = int(sub_id)
    cat_id = int(cat_id)
    prods = await list_products(sub_id)
    if not prods:
        await safe_edit_text(c.message, await get_setting("no_items"), reply_markup=kb_back(f"shop:cat:{cat_id}"))
        return await c.answer()
    rows = []
    for r in prods:
        rows.append([InlineKeyboardButton(text=r["name"], callback_data=f"shop:prod:{r['id']}:{sub_id}:{cat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop:cat:{cat_id}")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_edit_text(c.message, await get_setting("choose_product"), reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("shop:prod:"))
async def shop_product(c: CallbackQuery):
    _, _, prod_id, sub_id, cat_id = c.data.split(":")
    prod_id = int(prod_id)
    sub_id = int(sub_id)
    cat_id = int(cat_id)

    p = await get_product(prod_id)
    if not p:
        await c.answer("Товар не найден", show_alert=True)
        return

    name = p["name"]
    desc = (p["description"] or "").strip()
    price = (p["price"] or "").strip()

    text = f"<b>{name}</b>\n"
    if price:
        text += f"\n💰 <b>Цена:</b> {price}\n"
    if desc:
        text += f"\n📝 {desc}\n"

    rows = [
        [InlineKeyboardButton(text="✅ Купить", callback_data=f"buy:{prod_id}:{sub_id}:{cat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop:sub:{sub_id}:{cat_id}")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="home")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    # если есть медиа — отправим/покажем
    media_type = (p["media_type"] or "").strip()
    media_file_id = (p["media_file_id"] or "").strip()

    try:
        if media_type == "photo" and media_file_id:
            await c.message.edit_media(
                media=InputMediaPhoto(media=media_file_id, caption=text, parse_mode=ParseMode.HTML),
                reply_markup=kb
            )
        elif media_type == "video" and media_file_id:
            await c.message.edit_media(
                media=InputMediaVideo(media=media_file_id, caption=text, parse_mode=ParseMode.HTML),
                reply_markup=kb
            )
        else:
            await safe_edit_text(c.message, text, reply_markup=kb)
    except Exception:
        # если не получилось edit_media (например, предыдущее сообщение не медиа)
        if media_type == "photo" and media_file_id:
            await c.message.answer_photo(media_file_id, caption=text, reply_markup=kb)
        elif media_type == "video" and media_file_id:
            await c.message.answer_video(media_file_id, caption=text, reply_markup=kb)
        else:
            await c.message.answer(text, reply_markup=kb)

    await c.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_menu(c: CallbackQuery):
    _, prod_id, sub_id, cat_id = c.data.split(":")
    prod_id = int(prod_id); sub_id = int(sub_id); cat_id = int(cat_id)

    methods = await list_buy_methods(prod_id)
    if not methods:
        await c.answer("Способы покупки не настроены", show_alert=True)
        return

    rows = []
    for m in methods:
        rows.append([InlineKeyboardButton(text=m["title"], url=m["url"])])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop:prod:{prod_id}:{sub_id}:{cat_id}")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await safe_edit_text(c.message, "✅ Выберите способ покупки:", reply_markup=kb)
    await c.answer()


# -------------------- ADMIN UI --------------------
def admin_home_kb(role: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📦 Категории", callback_data="adm:cats")],
        [InlineKeyboardButton(text="✏️ Редактировать тексты", callback_data="adm:texts")],
    ]
    if role_at_least(role, ROLE_ADMIN):
        rows.append([InlineKeyboardButton(text="👥 Админы/модераторы", callback_data="adm:staff")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin:home")
async def cb_admin_home(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)
    await safe_edit_text(c.message, "⚙️ Админ панель", reply_markup=admin_home_kb(role))
    await c.answer()


# -------------------- ADMIN: TEXTS --------------------
@router.callback_query(F.data == "adm:texts")
async def adm_texts(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    keys = [
        ("start_text", "Стартовое сообщение"),
        ("support_text", "Поддержка"),
        ("group_welcome_text", "Приветствие в группе"),
        ("group_welcome_button", "Текст кнопки приветствия"),
    ]

    rows = []
    for k, title in keys:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"txt:{k}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_edit_text(c.message, "✏️ Выберите текст для редактирования:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("txt:"))
async def adm_text_pick(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    key = c.data.split(":", 1)[1]
    cur = await get_setting(key)
    await state.update_data(text_key=key)

    await safe_edit_text(
        c.message,
        f"Текущий текст для <b>{key}</b>:\n\n{cur}\n\n"
        "Отправьте новый текст (или '-' чтобы очистить):",
        reply_markup=kb_back("adm:texts")
    )
    await state.set_state(EditTexts.value)
    await c.answer()


@router.message(EditTexts.value)
async def adm_text_set(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return

    data = await state.get_data()
    key = data.get("text_key")
    if not key:
        await state.clear()
        return await m.answer("Ошибка состояния. /admin")

    new_val = m.text or ""
    if new_val.strip() == "-":
        new_val = ""

    await set_setting(key, new_val)
    await state.clear()
    await m.answer("✅ Текст обновлён. /admin")


# -------------------- ADMIN: CATEGORIES / SUBCATS / PRODUCTS --------------------
@router.callback_query(F.data == "adm:cats")
async def adm_cats(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    cats = await list_categories()
    rows = []
    for r in cats:
        rows.append([InlineKeyboardButton(text=r["name"], callback_data=f"adm:cat:{r['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data="adm:cat_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await safe_edit_text(c.message, "📦 Категории:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data == "adm:cat_add")
async def adm_cat_add(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)
    await safe_edit_text(c.message, "Введите название новой категории:", reply_markup=kb_back("adm:cats"))
    await state.set_state(AdminAddCategory.name)
    await c.answer()


@router.message(AdminAddCategory.name)
async def adm_cat_add_save(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return

    name = (m.text or "").strip()
    if not name:
        return await m.answer("Введите название текстом.")

    conn = await db()
    try:
        await conn.execute("INSERT INTO categories(name, pos) VALUES(?, 0)", (name,))
        await conn.commit()
    finally:
        await conn.close()

    await state.clear()
    await m.answer("✅ Категория добавлена. /admin")


@router.callback_query(F.data.startswith("adm:cat:"))
async def adm_cat_menu(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)
    cat_id = int(c.data.split(":")[-1])

    subs = await list_subcategories(cat_id)
    rows = []
    for s in subs:
        rows.append([InlineKeyboardButton(text=s["name"], callback_data=f"adm:sub:{s['id']}:{cat_id}")])

    rows.append([InlineKeyboardButton(text="➕ Добавить подкатегорию", callback_data=f"adm:sub_add:{cat_id}")])
    rows.append([InlineKeyboardButton(text="✏️ Переименовать категорию", callback_data=f"adm:cat_edit:{cat_id}")])
    rows.append([InlineKeyboardButton(text="🗑️ Удалить категорию", callback_data=f"adm:cat_del:{cat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:cats")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await safe_edit_text(c.message, "📦 Категория → Подкатегории:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("adm:cat_edit:"))
async def adm_cat_edit(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)
    cat_id = int(c.data.split(":")[-1])
    await state.update_data(category_id=cat_id)
    await safe_edit_text(c.message, "Введите новое название категории:", reply_markup=kb_back(f"adm:cat:{cat_id}"))
    await state.set_state(AdminEditCategory.name)
    await c.answer()


@router.message(AdminEditCategory.name)
async def adm_cat_edit_save(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return
    data = await state.get_data()
    cat_id = int(data.get("category_id") or 0)
    name = (m.text or "").strip()
    if not cat_id or not name:
        await state.clear()
        return await m.answer("Ошибка.")
    conn = await db()
    try:
        await conn.execute("UPDATE categories SET name=? WHERE id=?", (name, cat_id))
        await conn.commit()
    finally:
        await conn.close()
    await state.clear()
    await m.answer("✅ Категория обновлена. /admin")


@router.callback_query(F.data.startswith("adm:cat_del:"))
async def adm_cat_del(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_ADMIN):
        return await c.answer("⛔ Нужно быть admin/owner", show_alert=True)
    cat_id = int(c.data.split(":")[-1])

    conn = await db()
    try:
        await conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        await conn.commit()
    finally:
        await conn.close()

    await safe_edit_text(c.message, "🗑️ Категория удалена.", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:cats")]]
    ))
    await c.answer()


@router.callback_query(F.data.startswith("adm:sub_add:"))
async def adm_sub_add(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)
    cat_id = int(c.data.split(":")[-1])
    await state.update_data(category_id=cat_id)
    await safe_edit_text(c.message, "Введите название подкатегории:", reply_markup=kb_back(f"adm:cat:{cat_id}"))
    await state.set_state(AdminAddSubcategory.name)
    await c.answer()


@router.message(AdminAddSubcategory.name)
async def adm_sub_add_save(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return
    data = await state.get_data()
    cat_id = int(data.get("category_id") or 0)
    name = (m.text or "").strip()
    if not cat_id or not name:
        await state.clear()
        return await m.answer("Ошибка.")
    conn = await db()
    try:
        await conn.execute("INSERT INTO subcategories(category_id, name, pos) VALUES(?,?,0)", (cat_id, name))
        await conn.commit()
    finally:
        await conn.close()
    await state.clear()
    await m.answer("✅ Подкатегория добавлена. /admin")


@router.callback_query(F.data.startswith("adm:sub:"))
async def adm_sub_menu(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, sub_id, cat_id = c.data.split(":")
    sub_id = int(sub_id)
    cat_id = int(cat_id)

    prods = await list_products(sub_id)
    rows = []
    for p in prods:
        rows.append([InlineKeyboardButton(text=p["name"], callback_data=f"adm:prod:{p['id']}:{sub_id}:{cat_id}")])

    rows.append([InlineKeyboardButton(text="➕ Добавить товар", callback_data=f"adm:prod_add:{sub_id}:{cat_id}")])
    rows.append([InlineKeyboardButton(text="✏️ Переименовать подкатегорию", callback_data=f"adm:sub_edit:{sub_id}:{cat_id}")])
    rows.append([InlineKeyboardButton(text="🗑️ Удалить подкатегорию", callback_data=f"adm:sub_del:{sub_id}:{cat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:cat:{cat_id}")])

    await safe_edit_text(c.message, "📁 Подкатегория → Товары:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(F.data.startswith("adm:sub_edit:"))
async def adm_sub_edit(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, sub_id, cat_id = c.data.split(":")
    await state.update_data(subcategory_id=int(sub_id), category_id=int(cat_id))
    await safe_edit_text(c.message, "Введите новое название подкатегории:", reply_markup=kb_back(f"adm:sub:{sub_id}:{cat_id}"))
    await state.set_state(AdminEditSubcategory.name)
    await c.answer()


@router.message(AdminEditSubcategory.name)
async def adm_sub_edit_save(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return
    data = await state.get_data()
    sub_id = int(data.get("subcategory_id") or 0)
    name = (m.text or "").strip()
    if not sub_id or not name:
        await state.clear()
        return await m.answer("Ошибка.")
    conn = await db()
    try:
        await conn.execute("UPDATE subcategories SET name=? WHERE id=?", (name, sub_id))
        await conn.commit()
    finally:
        await conn.close()
    await state.clear()
    await m.answer("✅ Подкатегория обновлена. /admin")


@router.callback_query(F.data.startswith("adm:sub_del:"))
async def adm_sub_del(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_ADMIN):
        return await c.answer("⛔ Нужно быть admin/owner", show_alert=True)

    _, _, sub_id, cat_id = c.data.split(":")
    sub_id = int(sub_id); cat_id = int(cat_id)

    conn = await db()
    try:
        await conn.execute("DELETE FROM subcategories WHERE id=?", (sub_id,))
        await conn.commit()
    finally:
        await conn.close()

    await safe_edit_text(c.message, "🗑️ Подкатегория удалена.", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:cat:{cat_id}")]]
    ))
    await c.answer()


@router.callback_query(F.data.startswith("adm:prod_add:"))
async def adm_prod_add(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, sub_id, cat_id = c.data.split(":")
    await state.update_data(subcategory_id=int(sub_id), category_id=int(cat_id))
    await safe_edit_text(c.message, "Введите название товара:", reply_markup=kb_back(f"adm:sub:{sub_id}:{cat_id}"))
    await state.set_state(AdminAddProduct.name)
    await c.answer()


@router.message(AdminAddProduct.name)
async def adm_prod_add_name(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return
    name = (m.text or "").strip()
    if not name:
        return await m.answer("Введите название.")
    await state.update_data(name=name)
    await m.answer("Отправьте описание товара (или '-' чтобы пропустить):")
    await state.set_state(AdminAddProduct.description)


@router.message(AdminAddProduct.description)
async def adm_prod_add_desc(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return
    desc = (m.text or "").strip()
    if desc == "-":
        desc = ""
    await state.update_data(description=desc)
    await m.answer("Введите цену (или '-' чтобы пропустить):")
    await state.set_state(AdminAddProduct.price)


@router.message(AdminAddProduct.price)
async def adm_prod_add_price(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return
    price = (m.text or "").strip()
    if price == "-":
        price = ""
    await state.update_data(price=price)
    await m.answer("Отправьте фото/видео (или '-' чтобы пропустить):")
    await state.set_state(AdminAddProduct.media)


@router.message(AdminAddProduct.media)
async def adm_prod_add_media(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return

    data = await state.get_data()
    sub_id = int(data.get("subcategory_id") or 0)
    cat_id = int(data.get("category_id") or 0)
    name = data.get("name", "")
    desc = data.get("description", "")
    price = data.get("price", "")

    media_type = ""
    media_file_id = ""

    if (m.text or "").strip() == "-":
        pass
    elif m.photo:
        media_type = "photo"
        media_file_id = m.photo[-1].file_id
    elif m.video:
        media_type = "video"
        media_file_id = m.video.file_id
    else:
        return await m.answer("Пришлите фото/видео или '-'.")

    conn = await db()
    try:
        cur = await conn.execute(
            "INSERT INTO products(subcategory_id, name, description, price, media_type, media_file_id, pos) "
            "VALUES(?,?,?,?,?,?,0)",
            (sub_id, name, desc, price, media_type, media_file_id)
        )
        prod_id = cur.lastrowid
        await conn.commit()
    finally:
        await conn.close()

    await state.clear()
    await m.answer(f"✅ Товар добавлен.\nТеперь добавьте способы покупки: /admin\n(найдите товар и нажмите «Способы покупки»)")


@router.callback_query(F.data.startswith("adm:prod:"))
async def adm_prod_menu(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, prod_id, sub_id, cat_id = c.data.split(":")
    prod_id = int(prod_id); sub_id = int(sub_id); cat_id = int(cat_id)

    p = await get_product(prod_id)
    if not p:
        return await c.answer("Не найдено", show_alert=True)

    text = f"🛍️ <b>{p['name']}</b>\n\n"
    if p["price"]:
        text += f"💰 Цена: {p['price']}\n"
    if p["description"]:
        text += f"📝 {p['description']}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 Способы покупки", callback_data=f"adm:buy:{prod_id}:{sub_id}:{cat_id}")],
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"adm:prod_edit_name:{prod_id}:{sub_id}:{cat_id}")],
        [InlineKeyboardButton(text="✏️ Изменить описание", callback_data=f"adm:prod_edit_desc:{prod_id}:{sub_id}:{cat_id}")],
        [InlineKeyboardButton(text="✏️ Изменить цену", callback_data=f"adm:prod_edit_price:{prod_id}:{sub_id}:{cat_id}")],
        [InlineKeyboardButton(text="🖼️ Изменить медиа", callback_data=f"adm:prod_edit_media:{prod_id}:{sub_id}:{cat_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить товар", callback_data=f"adm:prod_del:{prod_id}:{sub_id}:{cat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:sub:{sub_id}:{cat_id}")],
    ])

    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("adm:prod_del:"))
async def adm_prod_del(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_ADMIN):
        return await c.answer("⛔ Нужно быть admin/owner", show_alert=True)

    _, _, prod_id, sub_id, cat_id = c.data.split(":")
    prod_id = int(prod_id); sub_id = int(sub_id); cat_id = int(cat_id)

    conn = await db()
    try:
        await conn.execute("DELETE FROM products WHERE id=?", (prod_id,))
        await conn.commit()
    finally:
        await conn.close()

    await safe_edit_text(c.message, "🗑️ Товар удалён.", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:sub:{sub_id}:{cat_id}")]]
    ))
    await c.answer()


@router.callback_query(F.data.startswith("adm:prod_edit_name:"))
async def adm_prod_edit_name(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, _, prod_id, sub_id, cat_id = c.data.split(":")
    await state.update_data(product_id=int(prod_id), sub_id=int(sub_id), cat_id=int(cat_id), field="name")
    await safe_edit_text(c.message, "Введите новое название товара:", reply_markup=kb_back(f"adm:prod:{prod_id}:{sub_id}:{cat_id}"))
    await state.set_state(AdminEditProduct.value)
    await c.answer()


@router.callback_query(F.data.startswith("adm:prod_edit_desc:"))
async def adm_prod_edit_desc(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, _, prod_id, sub_id, cat_id = c.data.split(":")
    await state.update_data(product_id=int(prod_id), sub_id=int(sub_id), cat_id=int(cat_id), field="description")
    await safe_edit_text(c.message, "Введите новое описание (или '-' чтобы очистить):", reply_markup=kb_back(f"adm:prod:{prod_id}:{sub_id}:{cat_id}"))
    await state.set_state(AdminEditProduct.value)
    await c.answer()


@router.callback_query(F.data.startswith("adm:prod_edit_price:"))
async def adm_prod_edit_price(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, _, prod_id, sub_id, cat_id = c.data.split(":")
    await state.update_data(product_id=int(prod_id), sub_id=int(sub_id), cat_id=int(cat_id), field="price")
    await safe_edit_text(c.message, "Введите новую цену (или '-' чтобы очистить):", reply_markup=kb_back(f"adm:prod:{prod_id}:{sub_id}:{cat_id}"))
    await state.set_state(AdminEditProduct.value)
    await c.answer()


@router.message(AdminEditProduct.value)
async def adm_prod_edit_save(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return

    data = await state.get_data()
    prod_id = int(data.get("product_id") or 0)
    field = data.get("field")
    sub_id = int(data.get("sub_id") or 0)
    cat_id = int(data.get("cat_id") or 0)

    if not prod_id or field not in ("name", "description", "price"):
        await state.clear()
        return await m.answer("Ошибка состояния.")

    val = (m.text or "").strip()
    if val == "-":
        val = ""

    conn = await db()
    try:
        await conn.execute(f"UPDATE products SET {field}=? WHERE id=?", (val, prod_id))
        await conn.commit()
    finally:
        await conn.close()

    await state.clear()
    await m.answer("✅ Обновлено. /admin")


@router.callback_query(F.data.startswith("adm:prod_edit_media:"))
async def adm_prod_edit_media(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, _, prod_id, sub_id, cat_id = c.data.split(":")
    await state.update_data(product_id=int(prod_id), sub_id=int(sub_id), cat_id=int(cat_id))
    await safe_edit_text(c.message, "Пришлите новое фото/видео (или '-' чтобы удалить медиа):", reply_markup=kb_back(f"adm:prod:{prod_id}:{sub_id}:{cat_id}"))
    await state.set_state(AdminEditProduct.media)
    await c.answer()


@router.message(AdminEditProduct.media)
async def adm_prod_edit_media_save(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return
    data = await state.get_data()
    prod_id = int(data.get("product_id") or 0)
    if not prod_id:
        await state.clear()
        return await m.answer("Ошибка состояния.")

    media_type = ""
    media_file_id = ""
    if (m.text or "").strip() == "-":
        pass
    elif m.photo:
        media_type = "photo"
        media_file_id = m.photo[-1].file_id
    elif m.video:
        media_type = "video"
        media_file_id = m.video.file_id
    else:
        return await m.answer("Пришлите фото/видео или '-'.")

    conn = await db()
    try:
        await conn.execute(
            "UPDATE products SET media_type=?, media_file_id=? WHERE id=?",
            (media_type, media_file_id, prod_id)
        )
        await conn.commit()
    finally:
        await conn.close()

    await state.clear()
    await m.answer("✅ Медиа обновлено. /admin")


# -------------------- ADMIN: BUY METHODS --------------------
@router.callback_query(F.data.startswith("adm:buy:"))
async def adm_buy_list(c: CallbackQuery):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, prod_id, sub_id, cat_id = c.data.split(":")
    prod_id = int(prod_id); sub_id = int(sub_id); cat_id = int(cat_id)

    methods = await list_buy_methods(prod_id)
    rows = []
    for mth in methods:
        rows.append([InlineKeyboardButton(text=f"✏️ {mth['title']}", callback_data=f"adm:buy_edit:{mth['id']}:{prod_id}:{sub_id}:{cat_id}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить способ", callback_data=f"adm:buy_add:{prod_id}:{sub_id}:{cat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:prod:{prod_id}:{sub_id}:{cat_id}")])

    await safe_edit_text(c.message, "🧾 Способы покупки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(F.data.startswith("adm:buy_add:"))
async def adm_buy_add(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, prod_id, sub_id, cat_id = c.data.split(":")
    await state.update_data(product_id=int(prod_id), sub_id=int(sub_id), cat_id=int(cat_id))
    await safe_edit_text(c.message, "Введите название кнопки (например: 'Оплатить картой'):", reply_markup=kb_back(f"adm:buy:{prod_id}:{sub_id}:{cat_id}"))
    await state.set_state(AdminAddBuyMethod.title)
    await c.answer()


@router.message(AdminAddBuyMethod.title)
async def adm_buy_add_title(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return
    title = (m.text or "").strip()
    if not title:
        return await m.answer("Введите название.")
    await state.update_data(title=title)
    await m.answer("Теперь отправьте ссылку (URL):")
    await state.set_state(AdminAddBuyMethod.url)


@router.message(AdminAddBuyMethod.url)
async def adm_buy_add_url(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return
    data = await state.get_data()
    prod_id = int(data.get("product_id") or 0)
    title = data.get("title", "")
    url = (m.text or "").strip()
    if not (prod_id and title and url.startswith(("http://", "https://", "tg://"))):
        return await m.answer("Нужна ссылка, начинающаяся с http:// или https:// (или tg://).")

    conn = await db()
    try:
        await conn.execute(
            "INSERT INTO buy_methods(product_id, title, url, pos) VALUES(?,?,?,0)",
            (prod_id, title, url)
        )
        await conn.commit()
    finally:
        await conn.close()

    await state.clear()
    await m.answer("✅ Способ покупки добавлен. /admin")


@router.callback_query(F.data.startswith("adm:buy_edit:"))
async def adm_buy_edit(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return await c.answer("⛔ Нет доступа", show_alert=True)

    _, _, mid, prod_id, sub_id, cat_id = c.data.split(":")
    mid = int(mid)

    conn = await db()
    try:
        cur = await conn.execute("SELECT id, title, url FROM buy_methods WHERE id=?", (mid,))
        row = await cur.fetchone()
    finally:
        await conn.close()

    if not row:
        return await c.answer("Не найдено", show_alert=True)

    await state.update_data(method_id=mid, prod_id=int(prod_id), sub_id=int(sub_id), cat_id=int(cat_id))
    await safe_edit_text(
        c.message,
        f"Текущий способ:\n<b>{row['title']}</b>\n{row['url']}\n\n"
        "Отправьте новое название (или '-' чтобы оставить):",
        reply_markup=kb_back(f"adm:buy:{prod_id}:{sub_id}:{cat_id}")
    )
    await state.set_state(AdminEditBuyMethod.title)
    await c.answer()


@router.message(AdminEditBuyMethod.title)
async def adm_buy_edit_title(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return

    data = await state.get_data()
    mid = int(data.get("method_id") or 0)
    if not mid:
        await state.clear()
        return await m.answer("Ошибка состояния.")

    title = (m.text or "").strip()
    if title != "-":
        conn = await db()
        try:
            await conn.execute("UPDATE buy_methods SET title=? WHERE id=?", (title, mid))
            await conn.commit()
        finally:
            await conn.close()

    await m.answer("Теперь отправьте новый URL (или '-' чтобы оставить):")
    await state.set_state(AdminEditBuyMethod.url)


@router.message(AdminEditBuyMethod.url)
async def adm_buy_edit_url(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_MOD):
        return

    data = await state.get_data()
    mid = int(data.get("method_id") or 0)
    url = (m.text or "").strip()

    if url != "-":
        if not url.startswith(("http://", "https://", "tg://")):
            return await m.answer("Нужна ссылка http:// или https:// (или tg://).")
        conn = await db()
        try:
            await conn.execute("UPDATE buy_methods SET url=? WHERE id=?", (url, mid))
            await conn.commit()
        finally:
            await conn.close()

    await state.clear()
    await m.answer("✅ Способ покупки обновлён. /admin")


# -------------------- ADMIN: STAFF (OWNER/ADMIN can) --------------------
@router.callback_query(F.data == "adm:staff")
async def adm_staff(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_ADMIN):
        return await c.answer("⛔ Нужно быть admin/owner", show_alert=True)

    rows = await staff_list()
    text = "👥 <b>Сотрудники</b>:\n\n"
    if not rows:
        text += "Пока никого нет."
    else:
        for r in rows:
            text += f"• <code>{r['user_id']}</code> — <b>{r['role']}</b>\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="staff:add")],
        [InlineKeyboardButton(text="➖ Удалить", callback_data="staff:remove")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")],
    ])
    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data == "staff:add")
async def staff_add_start(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_ADMIN):
        return await c.answer("⛔ Нужно быть admin/owner", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="admin", callback_data="staff:role:admin")],
        [InlineKeyboardButton(text="mod", callback_data="staff:role:mod")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:staff")],
    ])
    await safe_edit_text(c.message, "Выберите роль:", reply_markup=kb)
    await state.set_state(StaffAdd.role)
    await c.answer()


@router.callback_query(F.data.startswith("staff:role:"))
async def staff_add_role(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_ADMIN):
        return await c.answer("⛔ Нужно быть admin/owner", show_alert=True)

    new_role = c.data.split(":")[-1]
    await state.update_data(new_role=new_role)
    await safe_edit_text(c.message, "Отправьте user_id человека (число).", reply_markup=kb_back("adm:staff"))
    await state.set_state(StaffAdd.user_id)
    await c.answer()


@router.message(StaffAdd.user_id)
async def staff_add_finish(m: Message, state: FSMContext):
    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_ADMIN):
        return

    data = await state.get_data()
    new_role = data.get("new_role")
    try:
        uid = int((m.text or "").strip())
    except Exception:
        return await m.answer("Нужен user_id числом.")

    if OWNER_ID and uid == OWNER_ID:
        await state.clear()
        return await m.answer("Это владелец — роль owner уже установлена.")

    if new_role not in (ROLE_ADMIN, ROLE_MOD):
        await state.clear()
        return await m.answer("Ошибка роли.")

    await staff_set_role(uid, new_role)
    await state.clear()
    await m.answer(f"✅ Добавлено: {uid} → {new_role}. /admin")


@router.callback_query(F.data == "staff:remove")
async def staff_remove_start(c: CallbackQuery, state: FSMContext):
    role = await get_staff_role(c.from_user.id)
    if not role_at_least(role, ROLE_ADMIN):
        return await c.answer("⛔ Нужно быть admin/owner", show_alert=True)
    await safe_edit_text(c.message, "Отправьте user_id, которого удалить из staff:", reply_markup=kb_back("adm:staff"))
    await state.set_state(StaffAdd.user_id)
    await state.update_data(remove_mode=True)
    await c.answer()


@router.message(StaffAdd.user_id)
async def staff_remove_finish(m: Message, state: FSMContext):
    # этот хендлер уже используется выше; различим режим по remove_mode
    data = await state.get_data()
    if not data.get("remove_mode"):
        return  # пусть отработает add_finish выше

    role = await get_staff_role(m.from_user.id)
    if not role_at_least(role, ROLE_ADMIN):
        return

    try:
        uid = int((m.text or "").strip())
    except Exception:
        await state.clear()
        return await m.answer("Нужен user_id числом.")

    if OWNER_ID and uid == OWNER_ID:
        await state.clear()
        return await m.answer("Нельзя удалить владельца.")

    await staff_remove(uid)
    await state.clear()
    await m.answer(f"✅ Удалено: {uid}. /admin")


# -------------------- MAIN --------------------
async def main():
    await init_db()
    me = await bot.get_me()
    log.info("Bot started as @%s", me.username)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
