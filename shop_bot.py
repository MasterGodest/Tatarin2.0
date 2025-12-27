import asyncio
import logging
import json
from typing import List, Tuple, Optional, Dict

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8512928119:AAFCNGuCvwhKs48JUeAnUMTl7N1uisu3qF8"
OWNER_ID = 1831731188  # <-- твой Telegram user_id
DB_PATH = "shop.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shop_bot")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

BOT_USERNAME: Optional[str] = None  # заполним при старте


# =========================
# DB
# =========================
DEFAULT_SETTINGS: Dict[str, str] = {
    "start_text": "Привет! Нажми кнопку и открой магазин:",
    "support_text": "🆘 Поддержка\nНапишите менеджеру: @your_manager_username",
    "group_welcome_text": "👋 Добро пожаловать! Хотите посмотреть товары?\nНажмите кнопку 👇",
    "group_welcome_button": "🛍 Открыть магазин",
}

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'user' -- user/mod/admin/owner
        );
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            sort INTEGER NOT NULL DEFAULT 0
        );
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS subcategories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            sort INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
        );
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subcategory_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            price TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT '',    -- photo/video/''
            media_file_id TEXT NOT NULL DEFAULT '', -- telegram file_id
            is_active INTEGER NOT NULL DEFAULT 1,
            sort INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(subcategory_id) REFERENCES subcategories(id) ON DELETE CASCADE
        );
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS purchase_methods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL UNIQUE,     -- один метод на товар (MVP)
            method_type TEXT NOT NULL,              -- link/manager/text
            payload TEXT NOT NULL,                  -- json
            button_text TEXT NOT NULL DEFAULT 'Купить',
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        """)

        # гарантируем owner
        await db.execute("""
        INSERT INTO users(user_id, role) VALUES(?, 'owner')
        ON CONFLICT(user_id) DO UPDATE SET role='owner';
        """, (OWNER_ID,))

        # дефолтные настройки (только если ключа нет)
        for k, v in DEFAULT_SETTINGS.items():
            await db.execute("""
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO NOTHING;
            """, (k, v))

        await db.commit()


async def db_get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]
    return DEFAULT_SETTINGS.get(key, "")


async def db_set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("""
        INSERT INTO settings(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))
        await db.commit()


async def db_get_role(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT role FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else "user"


async def db_set_role(user_id: int, role: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("""
        INSERT INTO users(user_id, role) VALUES(?, ?)
        ON CONFLICT(user_id) DO UPDATE SET role=excluded.role
        """, (user_id, role))
        await db.commit()


async def db_list_staff() -> List[Tuple[int, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("""
            SELECT user_id, role FROM users
            WHERE role IN ('owner','admin','mod')
            ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'mod' THEN 2 ELSE 3 END, user_id
        """) as cur:
            return await cur.fetchall()


def role_rank(role: str) -> int:
    return {"user": 0, "mod": 1, "admin": 2, "owner": 3}.get(role, 0)


async def require_min_role(user_id: int, min_role: str) -> bool:
    r = await db_get_role(user_id)
    return role_rank(r) >= role_rank(min_role)


# ---------- Catalog queries ----------
async def db_get_categories():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT id, title FROM categories ORDER BY sort, id") as cur:
            return await cur.fetchall()


async def db_get_category(category_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT id, title FROM categories WHERE id=?", (category_id,)) as cur:
            return await cur.fetchone()


async def db_rename_category(category_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE categories SET title=? WHERE id=?", (title, category_id))
        await db.commit()


async def db_delete_category(category_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("DELETE FROM categories WHERE id=?", (category_id,))
        await db.commit()


async def db_get_subcategories(category_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute(
            "SELECT id, title FROM subcategories WHERE category_id=? ORDER BY sort, id",
            (category_id,),
        ) as cur:
            return await cur.fetchall()


async def db_get_subcategory(subcategory_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT id, category_id, title FROM subcategories WHERE id=?", (subcategory_id,)) as cur:
            return await cur.fetchone()


async def db_rename_subcategory(subcategory_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE subcategories SET title=? WHERE id=?", (title, subcategory_id))
        await db.commit()


async def db_delete_subcategory(subcategory_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("DELETE FROM subcategories WHERE id=?", (subcategory_id,))
        await db.commit()


async def db_get_products(subcategory_id: int, include_inactive: bool = False):
    q = "SELECT id, title, price, is_active FROM products WHERE subcategory_id=?"
    params = [subcategory_id]
    if not include_inactive:
        q += " AND is_active=1"
    q += " ORDER BY sort, id"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute(q, tuple(params)) as cur:
            return await cur.fetchall()


async def db_get_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("""
            SELECT id, subcategory_id, title, price, description, media_type, media_file_id, is_active
            FROM products WHERE id=?
        """, (product_id,)) as cur:
            return await cur.fetchone()


async def db_update_product_fields(product_id: int, **fields):
    # fields: title, price, description, media_type, media_file_id, is_active
    if not fields:
        return
    keys = []
    vals = []
    for k, v in fields.items():
        keys.append(f"{k}=?")
        vals.append(v)
    vals.append(product_id)
    q = f"UPDATE products SET {', '.join(keys)} WHERE id=?"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(q, tuple(vals))
        await db.commit()


async def db_delete_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("DELETE FROM products WHERE id=?", (product_id,))
        await db.commit()


async def db_get_purchase_method(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("""
            SELECT method_type, payload, button_text
            FROM purchase_methods WHERE product_id=?
        """, (product_id,)) as cur:
            return await cur.fetchone()


async def db_upsert_purchase_method(product_id: int, method_type: str, payload: dict, button_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("""
            INSERT INTO purchase_methods(product_id, method_type, payload, button_text)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                method_type=excluded.method_type,
                payload=excluded.payload,
                button_text=excluded.button_text
        """, (product_id, method_type, json.dumps(payload, ensure_ascii=False), button_text))
        await db.commit()


async def db_add_category(title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("INSERT INTO categories(title) VALUES(?)", (title,))
        await db.commit()


async def db_add_subcategory(category_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("INSERT INTO subcategories(category_id, title) VALUES(?, ?)", (category_id, title))
        await db.commit()


async def db_add_product(subcategory_id: int, title: str, price: str, description: str,
                         media_type: str, media_file_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute("""
            INSERT INTO products(subcategory_id, title, price, description, media_type, media_file_id)
            VALUES(?, ?, ?, ?, ?, ?)
        """, (subcategory_id, title, price, description, media_type, media_file_id))
        await db.commit()
        return cur.lastrowid


async def db_toggle_product_active(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("""
            UPDATE products
            SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END
            WHERE id=?
        """, (product_id,))
        await db.commit()


# =========================
# UI helpers
# =========================
def make_open_shop_kb(button_text: str) -> InlineKeyboardMarkup:
    if BOT_USERNAME:
        url = f"https://t.me/{BOT_USERNAME}?start=shop"
    else:
        url = "https://t.me/"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button_text, url=url)]
    ])


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")],
    ])


def kb_back(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=cb)]
    ])


def kb_admin_panel(is_owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить категорию", callback_data="adm_add_cat")],
        [InlineKeyboardButton(text="➕ Добавить подкатегорию", callback_data="adm_add_sub")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm_add_product")],
        [InlineKeyboardButton(text="✏️ Редактирование каталога", callback_data="adm_edit_catalog")],
        [InlineKeyboardButton(text="📝 Тексты (приветствие/поддержка)", callback_data="adm_texts")],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton(text="👑 Роли (admin/mod)", callback_data="adm_roles")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit_text(msg: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await msg.answer(text, reply_markup=reply_markup)


async def safe_delete(msg: Message):
    try:
        await msg.delete()
    except Exception:
        pass


# =========================
# FSM states
# =========================
class AddCategory(StatesGroup):
    title = State()


class AddSubcategory(StatesGroup):
    pick_category = State()
    title = State()


class AddProduct(StatesGroup):
    pick_category = State()
    pick_subcategory = State()
    title = State()
    price = State()
    description = State()
    media = State()
    purchase_type = State()
    purchase_payload = State()
    purchase_button_text = State()


class SetBuy(StatesGroup):
    product_id = State()
    purchase_type = State()
    purchase_payload = State()
    purchase_button_text = State()


class RolesManage(StatesGroup):
    action = State()
    user_id = State()
    role = State()
    target_user_id = State()


class EditTexts(StatesGroup):
    key = State()
    value = State()


class EditCategory(StatesGroup):
    category_id = State()
    new_title = State()


class EditSubcategory(StatesGroup):
    subcategory_id = State()
    new_title = State()


class EditProduct(StatesGroup):
    product_id = State()
    field = State()
    value = State()


# =========================
# CATALOG keyboards
# =========================
async def kb_categories(prefix: str = "cat") -> InlineKeyboardMarkup:
    cats = await db_get_categories()
    rows = []
    for cid, title in cats:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"{prefix}:{cid}")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def kb_subcategories(category_id: int) -> InlineKeyboardMarkup:
    subs = await db_get_subcategories(category_id)
    rows = []
    for sid, title in subs:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"sub:{category_id}:{sid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def kb_products(category_id: int, subcategory_id: int, is_staff: bool) -> InlineKeyboardMarkup:
    prods = await db_get_products(subcategory_id, include_inactive=is_staff)
    rows = []
    for pid, title, price, active in prods:
        label = title
        if price:
            label += f" — {price}"
        if is_staff and not active:
            label = "⛔ " + label
        rows.append([InlineKeyboardButton(text=label, callback_data=f"prod:{category_id}:{subcategory_id}:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"cat:{category_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_product_view(category_id: int, subcategory_id: int, product_id: int,
                    buy_text: str, has_buy: bool, is_mod: bool, is_admin: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_buy:
        rows.append([InlineKeyboardButton(text=f"✅ {buy_text}", callback_data=f"buy:{product_id}")])
    if is_mod:
        rows.append([InlineKeyboardButton(text="🔁 Вкл/Выкл", callback_data=f"adm_toggle:{product_id}")])
    if is_admin:
        rows.append([InlineKeyboardButton(text="✏️ Редактировать товар", callback_data=f"adm_edit_product:{product_id}")])
        rows.append([InlineKeyboardButton(text="🛒 Настроить покупку", callback_data=f"adm_setbuy:{product_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"sub:{category_id}:{subcategory_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =========================
# START / MENU
# =========================
@router.message(CommandStart())
async def start(m: Message):
    if m.from_user:
        uid = m.from_user.id
        if uid == OWNER_ID:
            await db_set_role(uid, "owner")
        else:
            r = await db_get_role(uid)
            if r == "user":
                await db_set_role(uid, "user")

    arg = (m.text or "").split(maxsplit=1)
    start_arg = arg[1] if len(arg) > 1 else ""

    start_text = await db_get_setting("start_text")
    if start_arg == "shop":
        await m.answer("🛍 Добро пожаловать в магазин! Выберите раздел:", reply_markup=kb_main())
    else:
        await m.answer(start_text, reply_markup=kb_main())


@router.message(Command("id"))
async def cmd_id(m: Message):
    if m.from_user:
        await m.answer(f"Ваш user_id: <code>{m.from_user.id}</code>")


@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    start_text = await db_get_setting("start_text")
    await safe_edit_text(c.message, start_text, reply_markup=kb_main())
    await c.answer()


@router.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    support_text = await db_get_setting("support_text")
    await safe_edit_text(c.message, support_text, reply_markup=kb_back("home"))
    await c.answer()


# =========================
# GROUP WELCOME
# =========================
@router.message(F.new_chat_members)
async def on_new_members(m: Message):
    if m.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    welcome = await db_get_setting("group_welcome_text")
    btn_text = await db_get_setting("group_welcome_button")

    for u in m.new_chat_members:
        if u.is_bot:
            continue
        await m.reply(welcome, reply_markup=make_open_shop_kb(btn_text))


# =========================
# SHOP FLOW
# =========================
@router.callback_query(F.data == "catalog")
async def cb_catalog(c: CallbackQuery):
    kb = await kb_categories(prefix="cat")
    await safe_edit_text(c.message, "🗂 Выберите категорию:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("cat:"))
async def cb_category(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    kb = await kb_subcategories(cid)
    await safe_edit_text(c.message, "📁 Выберите подкатегорию:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("sub:"))
async def cb_subcategory(c: CallbackQuery):
    _, cid_s, sid_s = c.data.split(":")
    cid, sid = int(cid_s), int(sid_s)
    is_staff = await require_min_role(c.from_user.id, "mod")
    kb = await kb_products(cid, sid, is_staff=is_staff)
    await safe_edit_text(c.message, "📦 Товары:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("prod:"))
async def cb_product(c: CallbackQuery):
    _, cid_s, sid_s, pid_s = c.data.split(":")
    cid, sid, pid = int(cid_s), int(sid_s), int(pid_s)

    p = await db_get_product(pid)
    if not p:
        await c.answer("Товар не найден", show_alert=True)
        return

    _, _, title, price, desc, media_type, media_file_id, is_active = p

    is_mod = await require_min_role(c.from_user.id, "mod")
    is_admin = await require_min_role(c.from_user.id, "admin")

    if not is_active and not is_mod:
        await c.answer("Товар недоступен", show_alert=True)
        return

    method = await db_get_purchase_method(pid)
    has_buy = method is not None
    buy_text = method[2] if method else "Купить"

    text = f"<b>{title}</b>\n"
    if price:
        text += f"💰 Цена: <b>{price}</b>\n"
    if desc:
        text += f"\n{desc}"

    kb = kb_product_view(cid, sid, pid, buy_text, has_buy, is_mod, is_admin)

    try:
        if media_type == "photo" and media_file_id:
            await safe_delete(c.message)
            await bot.send_photo(c.message.chat.id, photo=media_file_id, caption=text, reply_markup=kb)
        elif media_type == "video" and media_file_id:
            await safe_delete(c.message)
            await bot.send_video(c.message.chat.id, video=media_file_id, caption=text, reply_markup=kb)
        else:
            await safe_edit_text(c.message, text, reply_markup=kb)
    except Exception as e:
        logger.exception(e)
        await c.message.answer(text, reply_markup=kb)

    await c.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    method = await db_get_purchase_method(pid)
    if not method:
        await c.answer("Способ покупки не настроен", show_alert=True)
        return

    method_type, payload_str, button_text = method
    payload = json.loads(payload_str)

    if method_type == "link":
        url = payload.get("url", "").strip()
        if not url:
            await c.answer("Ссылка не задана", show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=url)]])
        await c.message.answer("Нажмите кнопку для покупки:", reply_markup=kb)
        await c.answer()

    elif method_type == "manager":
        username = payload.get("username", "").strip()
        template = payload.get("template", "Здравствуйте! Хочу купить товар: {product_id}")
        msg = template.format(product_id=pid)
        if username and not username.startswith("@"):
            username = "@" + username
        await c.message.answer(
            f"Напишите менеджеру: <b>{username}</b>\n\n"
            f"Сообщение:\n<code>{msg}</code>"
        )
        await c.answer()

    elif method_type == "text":
        text = payload.get("text", "").strip()
        if not text:
            await c.answer("Текст не задан", show_alert=True)
            return
        await c.message.answer(text)
        await c.answer()
    else:
        await c.answer("Неизвестный метод покупки", show_alert=True)


# =========================
# ADMIN ENTRY
# =========================
@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not m.from_user:
        return
    if not await require_min_role(m.from_user.id, "mod"):
        await m.answer("Нет доступа.")
        return
    is_owner = await require_min_role(m.from_user.id, "owner")
    await m.answer("⚙️ Админ-панель:", reply_markup=kb_admin_panel(is_owner=is_owner))


@router.callback_query(F.data == "adm_back_admin")
async def cb_adm_back(c: CallbackQuery):
    is_owner = await require_min_role(c.from_user.id, "owner")
    await safe_edit_text(c.message, "⚙️ Админ-панель:", reply_markup=kb_admin_panel(is_owner=is_owner))
    await c.answer()


# =========================
# ADD CATEGORY / SUB / PRODUCT (как раньше)
# =========================
@router.callback_query(F.data == "adm_add_cat")
async def cb_add_cat(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нужно быть admin", show_alert=True)
        return
    await state.set_state(AddCategory.title)
    await safe_edit_text(c.message, "Введите название новой категории:")
    await c.answer()


@router.message(AddCategory.title)
async def st_add_cat_title(m: Message, state: FSMContext):
    if not m.from_user or not await require_min_role(m.from_user.id, "admin"):
        return
    title = (m.text or "").strip()
    if not title:
        await m.answer("Название не может быть пустым. Введите снова:")
        return
    await db_add_category(title)
    await state.clear()
    is_owner = await require_min_role(m.from_user.id, "owner")
    await m.answer(f"✅ Категория добавлена: <b>{title}</b>", reply_markup=kb_admin_panel(is_owner=is_owner))


@router.callback_query(F.data == "adm_add_sub")
async def cb_add_sub(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нужно быть admin", show_alert=True)
        return

    cats = await db_get_categories()
    if not cats:
        await safe_edit_text(c.message, "Сначала добавьте категорию. /admin", reply_markup=kb_back("adm_back_admin"))
        await c.answer()
        return

    rows = [[InlineKeyboardButton(text=title, callback_data=f"adm_pick_cat_for_sub:{cid}")]
            for cid, title in cats]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back_admin")])

    await state.set_state(AddSubcategory.pick_category)
    await safe_edit_text(c.message, "Выберите категорию:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(AddSubcategory.pick_category, F.data.startswith("adm_pick_cat_for_sub:"))
async def cb_pick_cat_for_sub(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    await state.update_data(category_id=cid)
    await state.set_state(AddSubcategory.title)
    await safe_edit_text(c.message, "Введите название подкатегории:")
    await c.answer()


@router.message(AddSubcategory.title)
async def st_add_sub_title(m: Message, state: FSMContext):
    if not m.from_user or not await require_min_role(m.from_user.id, "admin"):
        return
    title = (m.text or "").strip()
    if not title:
        await m.answer("Название не может быть пустым. Введите снова:")
        return
    data = await state.get_data()
    cid = int(data["category_id"])
    await db_add_subcategory(cid, title)
    await state.clear()
    is_owner = await require_min_role(m.from_user.id, "owner")
    await m.answer(f"✅ Подкатегория добавлена: <b>{title}</b>", reply_markup=kb_admin_panel(is_owner=is_owner))


@router.callback_query(F.data == "adm_add_product")
async def cb_add_product(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нужно быть admin", show_alert=True)
        return

    cats = await db_get_categories()
    if not cats:
        await safe_edit_text(c.message, "Сначала добавьте категорию. /admin", reply_markup=kb_back("adm_back_admin"))
        await c.answer()
        return

    rows = [[InlineKeyboardButton(text=title, callback_data=f"adm_prod_cat:{cid}")]
            for cid, title in cats]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back_admin")])

    await state.set_state(AddProduct.pick_category)
    await safe_edit_text(c.message, "Выберите категорию:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(AddProduct.pick_category, F.data.startswith("adm_prod_cat:"))
async def cb_prod_pick_cat(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    subs = await db_get_subcategories(cid)
    if not subs:
        await c.answer("Нет подкатегорий. Сначала создайте подкатегорию.", show_alert=True)
        return

    rows = [[InlineKeyboardButton(text=title, callback_data=f"adm_prod_sub:{cid}:{sid}")]
            for sid, title in subs]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_add_product")])

    await state.set_state(AddProduct.pick_subcategory)
    await safe_edit_text(c.message, "Выберите подкатегорию:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(AddProduct.pick_subcategory, F.data.startswith("adm_prod_sub:"))
async def cb_prod_pick_sub(c: CallbackQuery, state: FSMContext):
    _, cid_s, sid_s = c.data.split(":")
    await state.update_data(category_id=int(cid_s), subcategory_id=int(sid_s))
    await state.set_state(AddProduct.title)
    await safe_edit_text(c.message, "Введите название товара:")
    await c.answer()


@router.message(AddProduct.title)
async def st_prod_title(m: Message, state: FSMContext):
    title = (m.text or "").strip()
    if not title:
        await m.answer("Название не может быть пустым. Введите снова:")
        return
    await state.update_data(title=title)
    await state.set_state(AddProduct.price)
    await m.answer("Введите цену (или '-' если не нужно):")


@router.message(AddProduct.price)
async def st_prod_price(m: Message, state: FSMContext):
    price = (m.text or "").strip()
    if price == "-":
        price = ""
    await state.update_data(price=price)
    await state.set_state(AddProduct.description)
    await m.answer("Введите описание (или '-' если не нужно):")


@router.message(AddProduct.description)
async def st_prod_desc(m: Message, state: FSMContext):
    desc = (m.text or "").strip()
    if desc == "-":
        desc = ""
    await state.update_data(description=desc)
    await state.set_state(AddProduct.media)
    await m.answer("Отправьте фото/видео для товара или '-' чтобы пропустить:")


@router.message(AddProduct.media)
async def st_prod_media(m: Message, state: FSMContext):
    media_type = ""
    media_file_id = ""

    if m.text and m.text.strip() == "-":
        pass
    elif m.photo:
        media_type = "photo"
        media_file_id = m.photo[-1].file_id
    elif m.video:
        media_type = "video"
        media_file_id = m.video.file_id
    else:
        await m.answer("Нужно фото/видео или '-' чтобы пропустить. Попробуйте ещё раз:")
        return

    await state.update_data(media_type=media_type, media_file_id=media_file_id)
    await state.set_state(AddProduct.purchase_type)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Ссылка (URL)", callback_data="pm_type:link")],
        [InlineKeyboardButton(text="👤 Менеджер (@username)", callback_data="pm_type:manager")],
        [InlineKeyboardButton(text="📝 Текст/инструкция", callback_data="pm_type:text")],
    ])
    await m.answer("Выберите способ покупки:", reply_markup=kb)


@router.callback_query(AddProduct.purchase_type, F.data.startswith("pm_type:"))
async def cb_pm_type(c: CallbackQuery, state: FSMContext):
    ptype = c.data.split(":")[1]
    await state.update_data(purchase_type=ptype)
    await state.set_state(AddProduct.purchase_payload)

    if ptype == "link":
        await safe_edit_text(c.message, "Отправьте ссылку (URL) для покупки:")
    elif ptype == "manager":
        await safe_edit_text(c.message, "Отправьте username менеджера (@manager или manager):")
    else:
        await safe_edit_text(c.message, "Отправьте текст инструкции, который увидит покупатель:")
    await c.answer()


@router.message(AddProduct.purchase_payload)
async def st_pm_payload(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt:
        await m.answer("Пусто. Введите ещё раз:")
        return

    data = await state.get_data()
    ptype = data["purchase_type"]

    if ptype == "link":
        payload = {"url": txt}
    elif ptype == "manager":
        username = txt[1:] if txt.startswith("@") else txt
        payload = {"username": username, "template": "Здравствуйте! Хочу купить товар (id={product_id})."}
    else:
        payload = {"text": txt}

    await state.update_data(purchase_payload=payload)
    await state.set_state(AddProduct.purchase_button_text)
    await m.answer("Текст кнопки? (например: Купить/Оплатить/Перейти) или '-' для 'Купить':")


@router.message(AddProduct.purchase_button_text)
async def st_pm_btn(m: Message, state: FSMContext):
    btn = (m.text or "").strip()
    if btn == "-" or not btn:
        btn = "Купить"

    data = await state.get_data()
    subcategory_id = int(data["subcategory_id"])
    title = data["title"]
    price = data["price"]
    description = data["description"]
    media_type = data["media_type"]
    media_file_id = data["media_file_id"]
    ptype = data["purchase_type"]
    payload = data["purchase_payload"]

    new_pid = await db_add_product(subcategory_id, title, price, description, media_type, media_file_id)
    await db_upsert_purchase_method(new_pid, ptype, payload, btn)

    await state.clear()
    is_owner = await require_min_role(m.from_user.id, "owner")
    await m.answer(f"✅ Товар добавлен: <b>{title}</b> (id=<code>{new_pid}</code>)",
                   reply_markup=kb_admin_panel(is_owner=is_owner))


# =========================
# MOD/ADMIN actions
# =========================
@router.callback_query(F.data.startswith("adm_toggle:"))
async def cb_toggle(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "mod"):
        await c.answer("Нет доступа", show_alert=True)
        return
    pid = int(c.data.split(":")[1])
    await db_toggle_product_active(pid)
    await c.answer("Готово ✅")


# =========================
# EDIT CATALOG (категории/подкатегории/товары)
# =========================
@router.callback_query(F.data == "adm_edit_catalog")
async def cb_edit_catalog(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нужно быть admin", show_alert=True)
        return
    cats = await db_get_categories()
    if not cats:
        await safe_edit_text(c.message, "Категорий нет. Добавьте категорию.", reply_markup=kb_back("adm_back_admin"))
        await c.answer()
        return

    rows = []
    for cid, title in cats:
        rows.append([
            InlineKeyboardButton(text=title, callback_data=f"adm_cat_manage:{cid}")
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back_admin")])

    await safe_edit_text(c.message, "✏️ Редактирование: выберите категорию", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(F.data.startswith("adm_cat_manage:"))
async def cb_cat_manage(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    cid = int(c.data.split(":")[1])
    cat = await db_get_category(cid)
    if not cat:
        await c.answer("Категория не найдена", show_alert=True)
        return
    _, title = cat

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Переименовать категорию", callback_data=f"adm_cat_rename:{cid}")],
        [InlineKeyboardButton(text="🗑 Удалить категорию", callback_data=f"adm_cat_delete:{cid}")],
        [InlineKeyboardButton(text="📁 Управлять подкатегориями", callback_data=f"adm_sub_list:{cid}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_edit_catalog")],
    ])
    await safe_edit_text(c.message, f"Категория: <b>{title}</b>\nЧто сделать?", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("adm_cat_delete:"))
async def cb_cat_delete(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    cid = int(c.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_cat_delete_yes:{cid}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_cat_manage:{cid}")],
    ])
    await safe_edit_text(c.message, "Удалить категорию? (Удалятся и подкатегории/товары внутри)", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("adm_cat_delete_yes:"))
async def cb_cat_delete_yes(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    cid = int(c.data.split(":")[1])
    await db_delete_category(cid)
    await c.answer("Удалено ✅")
    # вернёмся к списку
    await cb_edit_catalog(c)


@router.callback_query(F.data.startswith("adm_cat_rename:"))
async def cb_cat_rename(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    cid = int(c.data.split(":")[1])
    await state.set_state(EditCategory.category_id)
    await state.update_data(category_id=cid)
    await state.set_state(EditCategory.new_title)
    await c.message.answer("Введите новое название категории:")
    await c.answer()


@router.message(EditCategory.new_title)
async def st_cat_new_title(m: Message, state: FSMContext):
    if not m.from_user or not await require_min_role(m.from_user.id, "admin"):
        return
    title = (m.text or "").strip()
    if not title:
        await m.answer("Пусто. Введите ещё раз:")
        return
    data = await state.get_data()
    cid = int(data["category_id"])
    await db_rename_category(cid, title)
    await state.clear()
    await m.answer("✅ Категория переименована. /admin")


# ----- subcategories manage -----
@router.callback_query(F.data.startswith("adm_sub_list:"))
async def cb_sub_list(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    cid = int(c.data.split(":")[1])
    subs = await db_get_subcategories(cid)
    rows = []
    for sid, title in subs:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"adm_sub_manage:{cid}:{sid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_cat_manage:{cid}")])
    await safe_edit_text(c.message, "Подкатегории: выберите", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(F.data.startswith("adm_sub_manage:"))
async def cb_sub_manage(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, cid_s, sid_s = c.data.split(":")
    cid, sid = int(cid_s), int(sid_s)
    sub = await db_get_subcategory(sid)
    if not sub:
        await c.answer("Подкатегория не найдена", show_alert=True)
        return
    _, _, title = sub
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Переименовать подкатегорию", callback_data=f"adm_sub_rename:{sid}")],
        [InlineKeyboardButton(text="🗑 Удалить подкатегорию", callback_data=f"adm_sub_delete:{cid}:{sid}")],
        [InlineKeyboardButton(text="📦 Управлять товарами", callback_data=f"adm_prod_list:{cid}:{sid}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_sub_list:{cid}")],
    ])
    await safe_edit_text(c.message, f"Подкатегория: <b>{title}</b>\nЧто сделать?", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("adm_sub_delete:"))
async def cb_sub_delete(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, cid_s, sid_s = c.data.split(":")
    cid, sid = int(cid_s), int(sid_s)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_sub_delete_yes:{cid}:{sid}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_sub_manage:{cid}:{sid}")],
    ])
    await safe_edit_text(c.message, "Удалить подкатегорию? (Товары внутри удалятся)", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("adm_sub_delete_yes:"))
async def cb_sub_delete_yes(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, cid_s, sid_s = c.data.split(":")
    cid, sid = int(cid_s), int(sid_s)
    await db_delete_subcategory(sid)
    await c.answer("Удалено ✅")
    await cb_sub_list(c)  # вернёмся к списку подкатегорий


@router.callback_query(F.data.startswith("adm_sub_rename:"))
async def cb_sub_rename(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    sid = int(c.data.split(":")[1])
    await state.set_state(EditSubcategory.subcategory_id)
    await state.update_data(subcategory_id=sid)
    await state.set_state(EditSubcategory.new_title)
    await c.message.answer("Введите новое название подкатегории:")
    await c.answer()


@router.message(EditSubcategory.new_title)
async def st_sub_new_title(m: Message, state: FSMContext):
    if not m.from_user or not await require_min_role(m.from_user.id, "admin"):
        return
    title = (m.text or "").strip()
    if not title:
        await m.answer("Пусто. Введите ещё раз:")
        return
    data = await state.get_data()
    sid = int(data["subcategory_id"])
    await db_rename_subcategory(sid, title)
    await state.clear()
    await m.answer("✅ Подкатегория переименована. /admin")


# ----- products manage -----
@router.callback_query(F.data.startswith("adm_prod_list:"))
async def cb_prod_list(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, cid_s, sid_s = c.data.split(":")
    cid, sid = int(cid_s), int(sid_s)
    prods = await db_get_products(sid, include_inactive=True)
    rows = []
    for pid, title, price, active in prods:
        label = title + (f" — {price}" if price else "")
        if not active:
            label = "⛔ " + label
        rows.append([InlineKeyboardButton(text=label, callback_data=f"adm_prod_manage:{cid}:{sid}:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_sub_manage:{cid}:{sid}")])
    await safe_edit_text(c.message, "Товары: выберите", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(F.data.startswith("adm_prod_manage:"))
async def cb_prod_manage(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, cid_s, sid_s, pid_s = c.data.split(":")
    cid, sid, pid = int(cid_s), int(sid_s), int(pid_s)
    p = await db_get_product(pid)
    if not p:
        await c.answer("Товар не найден", show_alert=True)
        return
    _, _, title, price, desc, media_type, _, is_active = p
    status = "✅ активен" if is_active else "⛔ выключен"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать поля", callback_data=f"adm_edit_product:{pid}")],
        [InlineKeyboardButton(text="🛒 Настроить покупку", callback_data=f"adm_setbuy:{pid}")],
        [InlineKeyboardButton(text="🔁 Вкл/Выкл", callback_data=f"adm_toggle:{pid}")],
        [InlineKeyboardButton(text="🗑 Удалить товар", callback_data=f"adm_prod_delete:{cid}:{sid}:{pid}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_prod_list:{cid}:{sid}")],
    ])
    text = f"<b>{title}</b>\nСтатус: {status}\n"
    if price:
        text += f"Цена: <b>{price}</b>\n"
    if media_type:
        text += f"Медиа: <b>{media_type}</b>\n"
    if desc:
        text += f"\n{desc}"
    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("adm_prod_delete:"))
async def cb_prod_delete(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, cid_s, sid_s, pid_s = c.data.split(":")
    cid, sid, pid = int(cid_s), int(sid_s), int(pid_s)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_prod_delete_yes:{cid}:{sid}:{pid}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_prod_manage:{cid}:{sid}:{pid}")],
    ])
    await safe_edit_text(c.message, "Удалить товар?", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("adm_prod_delete_yes:"))
async def cb_prod_delete_yes(c: CallbackQuery):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, cid_s, sid_s, pid_s = c.data.split(":")
    cid, sid, pid = int(cid_s), int(sid_s), int(pid_s)
    await db_delete_product(pid)
    await c.answer("Удалено ✅")
    await cb_prod_list(c)


# ----- Edit product fields -----
@router.callback_query(F.data.startswith("adm_edit_product:"))
async def cb_edit_product(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нужно быть admin", show_alert=True)
        return
    pid = int(c.data.split(":")[1])
    p = await db_get_product(pid)
    if not p:
        await c.answer("Товар не найден", show_alert=True)
        return
    await state.set_state(EditProduct.product_id)
    await state.update_data(product_id=pid)
    await state.set_state(EditProduct.field)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Название", callback_data="ep_field:title")],
        [InlineKeyboardButton(text="Цена", callback_data="ep_field:price")],
        [InlineKeyboardButton(text="Описание", callback_data="ep_field:description")],
        [InlineKeyboardButton(text="Медиа (фото/видео/убрать)", callback_data="ep_field:media")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_prod_manage:0:0:{pid}")],
    ])
    await c.message.answer("Что редактируем у товара?", reply_markup=kb)
    await c.answer()


@router.callback_query(EditProduct.field, F.data.startswith("ep_field:"))
async def cb_ep_field(c: CallbackQuery, state: FSMContext):
    field = c.data.split(":")[1]
    await state.update_data(field=field)
    await state.set_state(EditProduct.value)

    if field == "media":
        await c.message.answer("Отправьте фото или видео для товара.\nИли отправьте '-' чтобы УБРАТЬ медиа.")
    else:
        await c.message.answer(f"Отправьте новое значение для поля <b>{field}</b>.\n(или '-' чтобы очистить)")
    await c.answer()


@router.message(EditProduct.value)
async def st_ep_value(m: Message, state: FSMContext):
    if not m.from_user or not await require_min_role(m.from_user.id, "admin"):
        return

    data = await state.get_data()
    pid = int(data["product_id"])
    field = data["field"]

    if field == "media":
        if m.text and m.text.strip() == "-":
            await db_update_product_fields(pid, media_type="", media_file_id="")
            await state.clear()
            await m.answer("✅ Медиа убрано. /admin")
            return
        if m.photo:
            await db_update_product_fields(pid, media_type="photo", media_file_id=m.photo[-1].file_id)
            await state.clear()
            await m.answer("✅ Фото обновлено. /admin")
            return
        if m.video:
            await db_update_product_fields(pid, media_type="video", media_file_id=m.video.file_id)
            await state.clear()
            await m.answer("✅ Видео обновлено. /admin")
            return
        await m.answer("Нужно фото/видео или '-' чтобы убрать. Попробуйте ещё раз:")
        return

    txt = (m.text or "").strip()
    if not txt:
        await m.answer("Пусто. Введите ещё раз:")
        return
    if txt == "-":
        txt = ""

    if field not in ("title", "price", "description"):
        await m.answer("Неизвестное поле.")
        await state.clear()
        return

    await db_update_product_fields(pid, **{field: txt})
    await state.clear()
    await m.answer("✅ Обновлено. /admin")


# =========================
# SET BUY METHOD (admin+)
# =========================
@router.callback_query(F.data.startswith("adm_setbuy:"))
async def cb_setbuy_start(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нужно быть admin", show_alert=True)
        return
    pid = int(c.data.split(":")[1])
    p = await db_get_product(pid)
    if not p:
        await c.answer("Товар не найден", show_alert=True)
        return

    await state.set_state(SetBuy.product_id)
    await state.update_data(product_id=pid)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Ссылка (URL)", callback_data="setbuy_type:link")],
        [InlineKeyboardButton(text="👤 Менеджер (@username)", callback_data="setbuy_type:manager")],
        [InlineKeyboardButton(text="📝 Текст/инструкция", callback_data="setbuy_type:text")],
    ])
    await c.message.answer("Выберите способ покупки:", reply_markup=kb)
    await c.answer()


@router.callback_query(SetBuy.product_id, F.data.startswith("setbuy_type:"))
async def cb_setbuy_type(c: CallbackQuery, state: FSMContext):
    ptype = c.data.split(":")[1]
    await state.update_data(purchase_type=ptype)
    await state.set_state(SetBuy.purchase_payload)

    if ptype == "link":
        await c.message.answer("Отправьте ссылку (URL):")
    elif ptype == "manager":
        await c.message.answer("Отправьте username менеджера (@manager или manager):")
    else:
        await c.message.answer("Отправьте текст инструкции:")
    await c.answer()


@router.message(SetBuy.purchase_payload)
async def st_setbuy_payload(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt:
        await m.answer("Пусто. Введите ещё раз:")
        return

    data = await state.get_data()
    ptype = data["purchase_type"]

    if ptype == "link":
        payload = {"url": txt}
    elif ptype == "manager":
        username = txt[1:] if txt.startswith("@") else txt
        payload = {"username": username, "template": "Здравствуйте! Хочу купить товар (id={product_id})."}
    else:
        payload = {"text": txt}

    await state.update_data(purchase_payload=payload)
    await state.set_state(SetBuy.purchase_button_text)
    await m.answer("Текст кнопки? Или '-' для 'Купить':")


@router.message(SetBuy.purchase_button_text)
async def st_setbuy_btn(m: Message, state: FSMContext):
    btn = (m.text or "").strip()
    if btn == "-" or not btn:
        btn = "Купить"

    data = await state.get_data()
    pid = int(data["product_id"])
    ptype = data["purchase_type"]
    payload = data["purchase_payload"]

    await db_upsert_purchase_method(pid, ptype, payload, btn)
    await state.clear()
    await m.answer("✅ Способ покупки обновлён. /admin")


# =========================
# EDIT TEXTS (приветствие / support / start)
# =========================
@router.callback_query(F.data == "adm_texts")
async def cb_texts(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нужно быть admin", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Текст /start", callback_data="txt:start_text")],
        [InlineKeyboardButton(text="Техподдержка", callback_data="txt:support_text")],
        [InlineKeyboardButton(text="Приветствие в группе", callback_data="txt:group_welcome_text")],
        [InlineKeyboardButton(text="Кнопка приветствия", callback_data="txt:group_welcome_button")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back_admin")],
    ])
    await safe_edit_text(c.message, "📝 Что редактируем?", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("txt:"))
async def cb_txt_pick(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "admin"):
        await c.answer("Нет доступа", show_alert=True)
        return
    key = c.data.split(":")[1]
    current = await db_get_setting(key)

    await state.set_state(EditTexts.key)
    await state.update_data(key=key)
    await state.set_state(EditTexts.value)

    await c.message.answer(
        f"Текущий текст для <b>{key}</b>:\n\n<code>{current}</code>\n\n"
        f"Отправьте новый текст (или '-' чтобы очистить):"
    )
    await c.answer()


@router.message(EditTexts.value)
async def st_txt_value(m: Message, state: FSMContext):
    if not m.from_user or not await require_min_role(m.from_user.id, "admin"):
        return
    txt = (m.text or "").strip()
    if not txt:
        await m.answer("Пусто. Введите ещё раз:")
        return
    if txt == "-":
        txt = ""

    data = await state.get_data()
    key = data["key"]
    await db_set_setting(key, txt)
    await state.clear()
    await m.answer("✅ Текст обновлён. /admin")


# =========================
# OWNER: roles
# =========================
@router.callback_query(F.data == "adm_roles")
async def cb_roles(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "owner"):
        await c.answer("Только OWNER", show_alert=True)
        return

    staff = await db_list_staff()
    text = "<b>👑 Роли</b>\n\n"
    for uid, role in staff:
        text += f"- <code>{uid}</code> — <b>{role}</b>\n"
    text += "\nВыберите действие:"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Назначить (admin/mod)", callback_data="role_action:set")],
        [InlineKeyboardButton(text="➖ Снять роль (в user)", callback_data="role_action:unset")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back_admin")],
    ])
    await safe_edit_text(c.message, text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("role_action:"))
async def cb_role_action(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "owner"):
        await c.answer("Только OWNER", show_alert=True)
        return
    action = c.data.split(":")[1]  # set/unset
    await state.set_state(RolesManage.action)
    await state.update_data(action=action)
    await state.set_state(RolesManage.user_id)
    await c.message.answer("Введите user_id пользователя (цифры):")
    await c.answer()


@router.message(RolesManage.user_id)
async def st_role_user_id(m: Message, state: FSMContext):
    if not m.from_user or not await require_min_role(m.from_user.id, "owner"):
        return
    txt = (m.text or "").strip()
    if not txt.isdigit():
        await m.answer("Нужно число user_id. Введите снова:")
        return

    uid = int(txt)
    data = await state.get_data()
    action = data.get("action")

    if uid == OWNER_ID and action != "set":
        await m.answer("Нельзя снять роль с OWNER_ID.")
        return

    if action == "unset":
        await db_set_role(uid, "user")
        await state.clear()
        await m.answer(f"✅ Пользователь <code>{uid}</code> теперь <b>user</b>.")
        return

    await state.update_data(target_user_id=uid)
    await state.set_state(RolesManage.role)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="admin", callback_data="setrole:admin")],
        [InlineKeyboardButton(text="mod", callback_data="setrole:mod")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm_back_admin")]
    ])
    await m.answer("Выберите роль:", reply_markup=kb)


@router.callback_query(RolesManage.role, F.data.startswith("setrole:"))
async def cb_set_role(c: CallbackQuery, state: FSMContext):
    if not await require_min_role(c.from_user.id, "owner"):
        await c.answer("Только OWNER", show_alert=True)
        return

    role = c.data.split(":")[1]
    data = await state.get_data()
    uid = int(data["target_user_id"])

    if uid == OWNER_ID:
        await c.answer("OWNER_ID всегда owner", show_alert=True)
        return

    await db_set_role(uid, role)
    await state.clear()
    await c.message.answer(f"✅ Пользователь <code>{uid}</code> теперь <b>{role}</b>.")
    await c.answer()


# =========================
# RUN
# =========================
async def main():
    global BOT_USERNAME
    await db_init()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"Bot started as @{BOT_USERNAME}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
