"""
NotCoin | MRKT — P2P Escrow Bot
Запуск: python bot.py

Зависимости:
pip install aiogram supabase python-dotenv aiohttp
"""

import asyncio
import logging
import random
import string
import re
from os import getenv
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from supabase import create_client, Client
from aiohttp import web

load_dotenv()

BOT_TOKEN = getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in getenv("ADMIN_IDS", "0").split(",") if x.strip()]
SUPABASE_URL = getenv("SUPABASE_URL", "")
SUPABASE_KEY = getenv("SUPABASE_KEY", "")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mrkt")

MANAGER = "SuportNot"

REGION_CURRENCY = {
    "ru": ("RUB", "🇷🇺"), "kz": ("KZT", "🇰🇿"), "ua": ("UAH", "🇺🇦"),
    "by": ("BYN", "🇧🇾"), "ge": ("GEL", "🇬🇪"), "md": ("MDL", "🇲🇩"),
    "tj": ("TJS", "🇹🇯"), "tm": ("TMT", "🇹🇲"),
}

TON_PATTERN = r"^(EQ|UQ)[A-Za-z0-9_-]{46}$"
BTC_PATTERN = r"^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$"
ETH_PATTERN = r"^0x[a-fA-F0-9]{40}$"


def luhn_check(number: str) -> bool:
    digits = [int(d) for d in number]
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    total = sum(odd_digits)
    for d in even_digits:
        total += sum(divmod(d * 2, 10))
    return total % 10 == 0


def validate_wallet(address: str) -> bool:
    address = address.strip()
    return bool(re.match(TON_PATTERN, address) or re.match(BTC_PATTERN, address) or re.match(ETH_PATTERN, address))


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ─── БД (Supabase) ──────────────────────────────────────────

supabase: Client = None


def db_init():
    global supabase
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


async def db_one(table, filters=None):
    query = supabase.table(table).select("*")
    if filters:
        for col, val in filters.items():
            query = query.eq(col, val)
    resp = await asyncio.to_thread(query.limit(1).execute)
    return resp.data[0] if resp.data else None


async def db_all(table, filters=None, order=None, limit=None):
    query = supabase.table(table).select("*")
    if filters:
        for col, val in filters.items():
            query = query.eq(col, val)
    if order:
        query = query.order(order, desc=True)
    if limit:
        query = query.limit(limit)
    resp = await asyncio.to_thread(query.execute)
    return resp.data or []


async def db_insert(table, data):
    resp = await asyncio.to_thread(supabase.table(table).insert(data).execute)
    return resp.data[0] if resp.data else None


async def db_update(table, data, filters):
    query = supabase.table(table).update(data)
    for col, val in filters.items():
        query = query.eq(col, val)
    await asyncio.to_thread(query.execute)


async def db_delete(table, filters):
    query = supabase.table(table).delete()
    for col, val in filters.items():
        query = query.eq(col, val)
    await asyncio.to_thread(query.execute)


async def db_inc(table, column, amount, filters):
    row = await db_one(table, filters)
    if row:
        new_val = (row.get(column) or 0) + amount
        await db_update(table, {column: new_val}, filters)


async def get_user(tid):
    return await db_one("users", {"telegram_id": tid})


async def create_user(tid, uname=None):
    existing = await get_user(tid)
    if existing:
        return existing
    await db_insert("users", {"telegram_id": tid, "username": uname})
    return await get_user(tid)


async def update_user(tid, **kw):
    await db_update("users", kw, {"telegram_id": tid})


async def update_balance(tid, currency, amount):
    col = f"balance_{currency}"
    row = await get_user(tid)
    if row:
        new_val = (row.get(col) or 0) + amount
        await db_update("users", {col: new_val}, {"telegram_id": tid})


async def gen_hash():
    abc = string.ascii_letters + string.digits
    for _ in range(100):
        h = ''.join(random.choices(abc, k=10))
        existing = await db_one("deals", {"deal_hash": h})
        if not existing:
            return h
    return ''.join(random.choices(abc, k=10))


async def add_deal(sid, suname, amt, cur, ptype, plink, card):
    h = await gen_hash()
    await db_insert("deals", {
        "deal_hash": h, "seller_id": sid, "seller_username": suname,
        "amount": amt, "currency": cur, "product_type": ptype,
        "product_link": plink, "seller_card": card
    })
    return h


async def get_deal_by_hash(h):
    return await db_one("deals", {"deal_hash": h})


async def update_deal(h, **kw):
    await db_update("deals", kw, {"deal_hash": h})


async def get_user_deals(tid):
    rows = await db_all("deals", order="created_at", limit=50)
    return [r for r in rows if r.get("seller_id") == tid or r.get("buyer_id") == tid][:10]


async def get_all_deals():
    return await db_all("deals", order="created_at", limit=50)


async def get_deal_stats():
    all_d = await db_all("deals")
    completed = sum(1 for d in all_d if d.get("status") == "completed")
    pending = sum(1 for d in all_d if d.get("status") == "pending")
    return {"total": len(all_d), "completed": completed, "pending": pending}


async def add_promocode(code, amt, mx, by):
    await db_insert("promocodes", {
        "code": code.upper(), "amount": amt, "max_uses": mx, "created_by": by
    })


async def get_promocode(code):
    return await db_one("promocodes", {"code": code.upper()})


async def get_all_promocodes():
    return await db_all("promocodes", order="created_at")


async def delete_promocode(code):
    await db_delete("promocodes", {"code": code.upper()})
    await db_delete("promo_uses", {"code": code.upper()})


async def use_promocode(code, tid):
    p = await get_promocode(code)
    if not p:
        return None, "Промокод не найден"
    if p.get("used_count", 0) >= p.get("max_uses", 1):
        return None, "Лимит использований исчерпан"
    existing = await db_one("promo_uses", {"code": code.upper(), "telegram_id": tid})
    if existing:
        return None, "Вы уже использовали этот промокод"
    await db_inc("promocodes", "used_count", 1, {"code": code.upper()})
    await db_insert("promo_uses", {"code": code.upper(), "telegram_id": tid})
    await update_balance(tid, "rub", p["amount"])
    return p["amount"], None


# ─── Тексты ───────────────────────────────────────────────────

PRODUCT_NAMES = {"nft": "NFT-Подарок / Подарок", "channel": "Канал / чат", "stars": "Звезды", "username": "Юзернейм", "other": "Другое"}

T = {
    "ru": {
        "welcome": "Добро пожаловать 👋\n\nNotCoin P2P — Мы специализированный сервис по обеспечению безопасности вне биржевых сделок.\nУдобный и быстрый вывод средств.\n• Комиссия сервиса: 1%\n• Режим работы: 24/7\n• Техническая поддержка: @SuportNot\n\n🛡 Выберите нужный раздел ниже:",
        "select_product": "Тип товара:",
        "select_payment": "Метод оплаты:",
        "enter_amount": "Введите сумму:\n\n<code>1000.50</code>",
        "bad_amount": "❌ Формат: 1000.50",
        "enter_link_nft": "🎁 NFT-Подарок / Подарок:\n\nВведите ссылку(-и) на подарок(-и):\nhttps://... или t.me/...\n\nПример:\nt.me/nft/PlushPepe-1\n\nЕсли несколько ссылок — каждая с новой строки.",
        "enter_link_channel": "📢 Канал / чат:\n\nВведите ссылку:\nhttps://... или t.me/...",
        "enter_link_stars": "⭐ Звезды:\n\nВведите ссылку или количество:",
        "enter_link_username": "🏷 Юзернейм:\n\nВведите ссылку:\nhttps://... или t.me/...",
        "enter_link_other": "📦 Другое:\n\nВведите ссылку на товар:\nhttps://...",
        "bad_link": "❌ Неверная ссылка",
        "balance": "💰 <b>Баланс</b>\n\n💳 RUB: {rub}\n💎 TON: {ton}\n⭐ Stars: {stars}\n\n📊 Комиссия: 1%\n📋 Сделок: {deals}",
        "no_balance": "❌ Недостаточно средств",
        "enter_withdraw": "Сумма вывода:",
        "withdraw_ok": "✅ Заявка создана",
        "requisites": "🔑 <b>Реквизиты</b>\n\n💳 Карта: {card}\n💎 TON: {ton}\n🌍 Регион: {region}",
        "enter_card": "Номер карты/телефона:",
        "card_ok": "✅ Сохранено",
        "enter_ton": "Адрес TON-кошелька:",
        "ton_ok": "✅ TON сохранен",
        "no_deals": "Нет сделок",
        "history": "📋 <b>История</b>\n\n{h}",
        "lang_ok": "✅ Язык изменен",
        "bad_wallet": "❌ Неверный адрес кошелька.\nДопустимые форматы:\n• TON: EQ.../UQ... (48 символов)\n• BTC: bc1.../1.../3...\n• ETH: 0x... (42 символа)",
        "enter_promo": "Введите промокод:",
        "promo_ok": "✅ Промокод активирован! +{amount} RUB на баланс",
        "promo_fail": "❌ {error}",
        "admin_deals": "📊 <b>Статистика сделок</b>\n\nВсего: {total}\n✅ Завершено: {completed}\n⏳ В процессе: {pending}",
        "admin_no_deals": "Нет сделок",
        "btn_create": "📝 Создать сделку",
        "btn_balance": "💰 Мой баланс",
        "btn_channel": "📢 Канал",
        "btn_successful": "✅ Успешные сделки",
        "btn_lang": "🌐 Изменить язык",
        "btn_support": "📩 Поддержка",
        "btn_back": "◀️ Назад",
        "btn_withdraw": "💸 Вывод",
        "btn_add_card": "💳 Добавить карту",
        "btn_add_ton": "💎 Добавить TON",
        "btn_share": "📤 Поделиться",
        "btn_menu": "◀️ Меню",
        "btn_nft": "🎁 NFT-Подарок",
        "btn_channel_prod": "📢 Канал / чат",
        "btn_stars": "⭐ Звезды",
        "btn_username": "🏷 Юзернейм",
        "btn_other": "📦 Другое",
        "btn_ton_pay": "💎 TON-кошелек",
        "btn_card_pay": "💳 Карта / СБП",
        "btn_other_pay": "🌍 Другие страны",
        "btn_pay": "💳 Оплатить",
        "btn_cancel": "❌ Отмена",
        "btn_done": "✅ Готово — передано",
        "btn_cancel_deal": "❌ Отменить сделку",
        "btn_seller": "🏷 Я Продавец",
    },
    "en": {
        "welcome": "Welcome 👋\n\nNotCoin P2P — We specialize in securing off-exchange deals.\nFast and convenient withdrawals.\n• Service fee: 1%\n• Working hours: 24/7\n• Support: @SuportNot\n\n🛡 Choose a section below:",
        "select_product": "Product type:",
        "select_payment": "Payment method:",
        "enter_amount": "Enter amount:\n\n<code>1000.50</code>",
        "bad_amount": "❌ Format: 1000.50",
        "enter_link_nft": "🎁 NFT-Gift / Gift:\n\nEnter gift link(s):\nhttps://... or t.me/...\n\nExample:\nt.me/nft/PlushPepe-1\n\nIf multiple links — each on a new line.",
        "enter_link_channel": "📢 Channel / Chat:\n\nEnter channel link:\nhttps://... or t.me/...",
        "enter_link_stars": "⭐ Stars:\n\nEnter link or amount:",
        "enter_link_username": "🏷 Username:\n\nEnter link:\nhttps://... or t.me/...",
        "enter_link_other": "📦 Other:\n\nEnter product link:\nhttps://...",
        "bad_link": "❌ Invalid link",
        "balance": "💰 <b>Balance</b>\n\n💳 RUB: {rub}\n💎 TON: {ton}\n⭐ Stars: {stars}\n\n📊 Commission: 1%\n📋 Deals: {deals}",
        "no_balance": "❌ Insufficient funds",
        "enter_withdraw": "Withdrawal amount:",
        "withdraw_ok": "✅ Request created",
        "requisites": "🔑 <b>Requisites</b>\n\n💳 Card: {card}\n💎 TON: {ton}\n🌍 Region: {region}",
        "enter_card": "Card / phone number:",
        "card_ok": "✅ Saved",
        "enter_ton": "TON wallet address:",
        "ton_ok": "✅ TON saved",
        "no_deals": "No deals",
        "history": "📋 <b>History</b>\n\n{h}",
        "lang_ok": "✅ Language changed",
        "bad_wallet": "❌ Invalid wallet address.\nValid formats:\n• TON: EQ.../UQ... (48 chars)\n• BTC: bc1.../1.../3...\n• ETH: 0x... (42 chars)",
        "enter_promo": "Enter promo code:",
        "promo_ok": "✅ Promo activated! +{amount} RUB to balance",
        "promo_fail": "❌ {error}",
        "admin_deals": "📊 <b>Deal Statistics</b>\n\nTotal: {total}\n✅ Completed: {completed}\n⏳ Pending: {pending}",
        "admin_no_deals": "No deals",
        "btn_create": "📝 Create deal",
        "btn_balance": "💰 My balance",
        "btn_channel": "📢 Channel",
        "btn_successful": "✅ Successful deals",
        "btn_lang": "🌐 Change language",
        "btn_support": "📩 Support",
        "btn_back": "◀️ Back",
        "btn_withdraw": "💸 Withdraw",
        "btn_add_card": "💳 Add card",
        "btn_add_ton": "💎 Add TON",
        "btn_share": "📤 Share",
        "btn_menu": "◀️ Menu",
        "btn_nft": "🎁 NFT-Gift",
        "btn_channel_prod": "📢 Channel / chat",
        "btn_stars": "⭐ Stars",
        "btn_username": "🏷 Username",
        "btn_other": "📦 Other",
        "btn_ton_pay": "💎 TON wallet",
        "btn_card_pay": "💳 Card / SBP",
        "btn_other_pay": "🌍 Other countries",
        "btn_pay": "💳 Pay",
        "btn_cancel": "❌ Cancel",
        "btn_done": "✅ Done — transferred",
        "btn_cancel_deal": "❌ Cancel deal",
        "btn_seller": "🏷 I'm Seller",
    },
    "ar": {
        "welcome": "مرحباً 👋\n\nNotCoin P2P — نحن خبراء في تأمين الصفقات خارج البورصة.\nسحب سريع وسهل.\n• العمولة: 1%\n• يعمل 24/7\n• الدعم: @SuportNot\n\n🛡 اختر القسم:",
        "lang_ok": "✅ تم تغيير اللغة",
        "btn_create": "📝 إنشاء صفقة",
        "btn_balance": "💰 رصيدي",
        "btn_channel": "📢 القناة",
        "btn_successful": "✅ الصفقات الناجحة",
        "btn_lang": "🌐 تغيير اللغة",
        "btn_support": "📩 الدعم",
        "btn_back": "◀️ رجوع",
        "btn_withdraw": "💸 سحب",
        "btn_add_card": "💳 إضافة بطاقة",
        "btn_add_ton": "💎 إضافة TON",
        "btn_share": "📤 مشاركة",
        "btn_menu": "◀️ القائمة",
        "btn_nft": "🎁 NFT-هدية",
        "btn_channel_prod": "📢 قناة / دردشة",
        "btn_stars": "⭐ نجوم",
        "btn_username": "🏷 اسم مستخدم",
        "btn_other": "📦 أخرى",
        "btn_ton_pay": "💎 محفظة TON",
        "btn_card_pay": "💳 بطاقة / SBP",
        "btn_other_pay": "🌍 دول أخرى",
        "btn_pay": "💳 دفع",
        "btn_cancel": "❌ إلغاء",
        "btn_done": "✅ تم — تم التحويل",
        "btn_cancel_deal": "❌ إلغاء الصفقة",
        "btn_seller": "🏷 أنا البائع",
    },
    "zh": {
        "welcome": "欢迎 👋\n\nNotCoin P2P — 我们专注于场外交易安全保障。\n快速便捷提现。\n• 服务费: 1%\n• 24/7运营\n• 技术支持: @SuportNot\n\n🛡 请选择以下板块:",
        "lang_ok": "✅ 语言已更改",
        "btn_create": "📝 创建交易",
        "btn_balance": "💰 我的余额",
        "btn_channel": "📢 频道",
        "btn_successful": "✅ 成功交易",
        "btn_lang": "🌐 更改语言",
        "btn_support": "📩 支持",
        "btn_back": "◀️ 返回",
        "btn_withdraw": "💸 提现",
        "btn_add_card": "💳 添加银行卡",
        "btn_add_ton": "💎 添加 TON",
        "btn_share": "📤 分享",
        "btn_menu": "◀️ 菜单",
        "btn_nft": "🎁 NFT礼品",
        "btn_channel_prod": "📢 频道 / 聊天",
        "btn_stars": "⭐ 星星",
        "btn_username": "🏷 用户名",
        "btn_other": "📦 其他",
        "btn_ton_pay": "💎 TON钱包",
        "btn_card_pay": "💳 银行卡 / SBP",
        "btn_other_pay": "🌍 其他国家",
        "btn_pay": "💳 支付",
        "btn_cancel": "❌ 取消",
        "btn_done": "✅ 完成 — 已转交",
        "btn_cancel_deal": "❌ 取消交易",
        "btn_seller": "🏷 我是卖家",
    },
}


def tr(key, lang="ru", **kw):
    texts = T.get(lang, T["ru"])
    text = texts.get(key, T["ru"].get(key, key))
    return text.format(**kw) if kw else text


# ─── Клавиатуры ───────────────────────────────────────────────

def btn(text, data, style=None):
    b = {"text": text}
    if data.startswith("http") or data.startswith("open:"):
        b["url"] = data.replace("open:", "")
    else:
        b["callback_data"] = data
    if style:
        b["style"] = style
    return b

def kb(*rows):
    keyboard = []
    for row in rows:
        if isinstance(row, list):
            keyboard.append([btn(t, d, s) for t, d, s in row])
        else:
            keyboard.append([btn(row[0], row[1], row[2] if len(row) > 2 else None)])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def main_kb(lang="ru"):
    return kb(
        (tr("btn_create", lang), "create_deal", "success"),
        (tr("btn_balance", lang), "my_balance", "success"),
        (tr("btn_channel", lang), "open:https://t.me/notcoin", "success"),
        (tr("btn_successful", lang), "successful", "success"),
        (tr("btn_lang", lang), "change_lang", "success"),
        (tr("btn_support", lang), f"open:https://t.me/{MANAGER}", "danger"),
    )

def product_kb(lang="ru"):
    return kb(
        (tr("btn_nft", lang), "prod_nft", "success"), (tr("btn_channel_prod", lang), "prod_channel", "success"),
        (tr("btn_stars", lang), "prod_stars", "success"), (tr("btn_username", lang), "prod_username", "success"),
        (tr("btn_other", lang), "prod_other", "success"), (tr("btn_back", lang), "main", "danger"),
    )

def payment_kb(lang="ru"):
    return kb(
        (tr("btn_ton_pay", lang), "pay_ton", "success"), (tr("btn_card_pay", lang), "pay_card", "success"),
        (tr("btn_other_pay", lang), "pay_other", "success"), (tr("btn_stars", lang), "pay_stars", "success"),
        (tr("btn_back", lang), "select_product", "danger"),
    )

def balance_kb(lang="ru"):
    return kb((tr("btn_withdraw", lang), "withdraw", "success"), (tr("btn_back", lang), "main", "danger"))

def req_kb(lang="ru"):
    return kb((tr("btn_add_card", lang), "add_card", "success"), (tr("btn_add_ton", lang), "add_ton", "success"), (tr("btn_back", lang), "main", "danger"))

def region_kb(lang="ru"):
    return kb(
        [("🇷🇺 РФ", "reg_ru", "success"), ("🇰🇿 Казахстан", "reg_kz", "success")],
        [("🇺🇦 Украина", "reg_ua", "success"), ("🇧🇾 Беларусь", "reg_by", "success")],
        [("🇬🇪 Грузия", "reg_ge", "success"), ("🇲🇩 Молдова", "reg_md", "success")],
        [("🇹🇯 Таджикистан", "reg_tj", "success"), ("🇹🇲 Туркменистан", "reg_tm", "success")],
        (tr("btn_back", lang), "main", "danger"),
    )

def lang_kb():
    return kb(
        [("🇷🇺 Русский", "lang_ru", "success"), ("🇬🇧 English", "lang_en", "success")],
        [("🇸🇦 العربية", "lang_ar", "success"), ("🇨🇳 中文", "lang_zh", "success")],
    )

def share_kb(url, lang="ru"):
    return kb((tr("btn_share", lang), f"open:{url}", "success"), (tr("btn_menu", lang), "main", "danger"))

def buyer_kb(h, lang="ru"):
    return kb((tr("btn_pay", lang), f"bpaid_{h}", "success"), (tr("btn_cancel", lang), "main", "danger"))

def seller_confirm_kb(h, lang="ru"):
    return kb((tr("btn_done", lang), f"seller_done_{h}", "success"), (tr("btn_cancel_deal", lang), f"seller_cancel_{h}", "danger"))

def admin_kb():
    return kb(("📊 Сделки", "admin_deals", "success"), ("📋 Промокоды", "admin_promos", "success"), ("◀️ Назад", "main", "danger"))

def admin_deals_kb():
    return kb(("📊 Статистика", "admin_stats", "success"), ("📋 Все сделки", "admin_all_deals", "success"), ("◀️ Назад", "admin_menu", "danger"))

def admin_promo_kb():
    return kb(("➕ Создать промокод", "admin_create_promo", "success"), ("📋 Все промокоды", "admin_all_promos", "success"), ("◀️ Назад", "admin_menu", "danger"))


# ─── FSM ──────────────────────────────────────────────────────

class DealFSM(StatesGroup):
    product = State()
    payment = State()
    amount = State()
    link = State()

class ReqFSM(StatesGroup):
    region = State()
    card = State()
    ton = State()

class WithFSM(StatesGroup):
    amount = State()

class PromoFSM(StatesGroup):
    code = State()
    amount = State()
    max_uses = State()


# ─── Роутер ───────────────────────────────────────────────────

r = Router()
GIF_WELCOME = "https://i.imgur.com/BI4fgau.mp4"
GIF_LANG = "https://i.imgur.com/nmCkhIr.mp4"
GIF_BALANCE = "https://i.imgur.com/CFprQXN.mp4"

BOT_USERNAME = ""


# ─── /start ───────────────────────────────────────────────────

@r.message(CommandStart())
async def m_start(msg: Message, state: FSMContext):
    await state.clear()
    u = await get_user(msg.from_user.id)
    if not u:
        u = await create_user(msg.from_user.id, msg.from_user.username)
    lang = u["language"]

    if msg.text and len(msg.text.split()) > 1:
        p = msg.text.split()[1]
        if p.startswith("deal_"):
            deal_hash = p.replace("deal_", "")
            d = await get_deal_by_hash(deal_hash)
            if d:
                seller_uname = d.get("seller_username") or "продавец"
                ptype = PRODUCT_NAMES.get(d["product_type"], d["product_type"])
                card = d.get("seller_card") or "❌"
                currency = d.get("currency") or "RUB"
                text = (
                    f"💳 <b>Информация о сделке #{deal_hash}</b>\n\n"
                    f"👋 Вы покупатель в сделке.\n"
                    f"📩 Продавец: @{seller_uname}\n\n"
                    f"📜 Вы покупаете: {d['product_link']}\n\n"
                    f"🛒 Товар: {ptype}\n\n"
                    f"💼 Способ оплаты: {currency}\n"
                    f"💳 Карта: {card}\n"
                    f"ID сделки: {deal_hash}\n\n"
                    f"Сумма к оплате: {d['amount']} {currency}\n\n"
                    f"Пожалуйста, следуйте инструкциям продавца по оплате.\n"
                    f"Сохраните ID сделки для подтверждения!\n\n"
                    f"В случае проблем обратитесь: https://t.me/{MANAGER}"
                )
                await update_deal(deal_hash, buyer_id=msg.from_user.id, buyer_username=msg.from_user.username or str(msg.from_user.id))
                await msg.answer(text, reply_markup=buyer_kb(deal_hash, lang), parse_mode="HTML")
                return
        if p.startswith("promo_"):
            code = p.replace("promo_", "")
            amount, error = await use_promocode(code, msg.from_user.id)
            if error:
                await msg.answer(tr("promo_fail", u["language"], error=error), reply_markup=main_kb(lang))
            else:
                await msg.answer(tr("promo_ok", u["language"], amount=amount), reply_markup=main_kb(lang))
            return

    try:
        await msg.answer_video(video=GIF_WELCOME, caption=tr("welcome", u["language"]), reply_markup=main_kb(lang), parse_mode="HTML")
    except:
        try:
            await msg.answer_animation(animation=GIF_WELCOME, caption=tr("welcome", u["language"]), reply_markup=main_kb(lang), parse_mode="HTML")
        except:
            await msg.answer(tr("welcome", u["language"]), reply_markup=main_kb(lang))


@r.message(Command("promo"))
async def m_promo(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.answer(tr("enter_promo", u["language"]), reply_markup=main_kb(lang))
        await state.set_state(PromoFSM.code)
        return
    code = args[1].strip()
    amount, error = await use_promocode(code, msg.from_user.id)
    if error:
        await msg.answer(tr("promo_fail", u["language"], error=error), reply_markup=main_kb(lang))
    else:
        await msg.answer(tr("promo_ok", u["language"], amount=amount), reply_markup=main_kb(lang))

@r.message(PromoFSM.code)
async def m_promo_code(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    code = msg.text.strip()
    amount, error = await use_promocode(code, msg.from_user.id)
    if error:
        await msg.answer(tr("promo_fail", u["language"], error=error), reply_markup=main_kb(lang))
    else:
        await msg.answer(tr("promo_ok", u["language"], amount=amount), reply_markup=main_kb(lang))
    await state.clear()


# ─── Главное меню ─────────────────────────────────────────────

@r.callback_query(F.data == "main")
async def c_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    try:
        await cb.message.edit_text(tr("welcome", u["language"]), reply_markup=main_kb(lang))
    except:
        await cb.message.answer(tr("welcome", u["language"]), reply_markup=main_kb(lang))
    await cb.answer()


# ─── Создание сделки ─────────────────────────────────────────

@r.callback_query(F.data == "create_deal")
async def c_create(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.set_state(DealFSM.product)
    await cb.message.edit_text(tr("select_product", u["language"]), reply_markup=product_kb(lang))
    await cb.answer()

@r.callback_query(F.data.startswith("prod_"))
async def c_product(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.update_data(product=cb.data.replace("prod_", ""))
    await state.set_state(DealFSM.payment)
    await cb.message.edit_text(tr("select_payment", u["language"]), reply_markup=payment_kb(lang))
    await cb.answer()

@r.callback_query(F.data.startswith("pay_"))
async def c_payment(cb: CallbackQuery, state: FSMContext):
    pay = cb.data.replace("pay_", "")
    await state.update_data(payment=pay)
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    if pay == "card" and not u.get("requisites_card"):
        await cb.message.edit_text("❌ Нет реквизитов! Добавьте сначала.", reply_markup=req_kb(lang))
        await cb.answer(); return
    if pay == "ton" and not u.get("requisites_ton"):
        await cb.message.edit_text("❌ Нет TON-кошелька! Добавьте сначала.", reply_markup=req_kb(lang))
        await cb.answer(); return
    await state.set_state(DealFSM.amount)
    await cb.message.edit_text(tr("enter_amount", u["language"]), parse_mode="HTML")
    await cb.answer()

@r.message(DealFSM.amount)
async def m_amount(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    try:
        amount = float(msg.text.replace(",", ".").strip())
        if amount <= 0: raise ValueError
    except:
        await msg.answer(tr("bad_amount", u["language"])); return
    await state.update_data(amount=amount)
    data = await state.get_data()
    product = data["product"]
    prompts = {"nft": "enter_link_nft", "channel": "enter_link_channel", "stars": "enter_link_stars", "username": "enter_link_username", "other": "enter_link_other"}
    await state.set_state(DealFSM.link)
    await msg.answer(tr(prompts.get(product, "enter_link_other"), u["language"]), parse_mode="HTML")

@r.message(DealFSM.link)
async def m_link(msg: Message, state: FSMContext):
    log.info(f"DealFSM.link received from {msg.from_user.id}: {msg.text}")
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    link = msg.text.strip()
    if not (link.startswith("http") or link.startswith("t.me")):
        await msg.answer(tr("bad_link", u["language"])); return
    data = await state.get_data()
    product = data["product"]
    amount = data["amount"]
    payment = data["payment"]
    region = u.get("requisites_region") or "ru"
    currency, flag = REGION_CURRENCY.get(region, ("RUB", "🇷🇺"))
    seller_card = u.get("requisites_card") or "" if payment == "card" else ""
    deal_hash = await add_deal(msg.from_user.id, msg.from_user.username or str(msg.from_user.id), amount, currency, product, link, seller_card)
    ptype = PRODUCT_NAMES.get(product, product)
    text = (
        f"✅ <b>Сделка #{deal_hash} создана!</b>\n\n"
        f"📜 Товар: {ptype}\n"
        f"🔗 Ссылка: {link}\n"
        f"💸 Сумма: {amount} {currency}\n\n"
        f"📣 Ссылка для покупателя:\n"
        f"<code>https://t.me/{BOT_USERNAME}?start=deal_{deal_hash}</code>\n\n"
        f"Отправьте эту ссылку покупателю."
    )
    url = f"https://t.me/{BOT_USERNAME}?start=deal_{deal_hash}"
    try:
        await msg.answer_video(video=GIF_WELCOME, caption=text, reply_markup=share_kb(url, lang), parse_mode="HTML")
    except:
        try:
            await msg.answer_animation(animation=GIF_WELCOME, caption=text, reply_markup=share_kb(url, lang), parse_mode="HTML")
        except:
            await msg.answer(text, reply_markup=share_kb(url, lang), parse_mode="HTML")
    await state.clear()


# ─── Покупатель нажал "Оплатил" ──────────────────────────────

@r.callback_query(F.data.startswith("bpaid_"))
async def cb_pay_done(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    deal_hash = cb.data.replace("bpaid_", "")
    d = await get_deal_by_hash(deal_hash)
    if not d:
        await cb.answer("❌ Сделка не найдена", show_alert=True); return
    if d["buyer_id"] and d["buyer_id"] != cb.from_user.id:
        await cb.answer("❌ Это не ваша сделка", show_alert=True); return
    buyer_uname = cb.from_user.username or str(cb.from_user.id)
    await update_deal(deal_hash, buyer_username=buyer_uname)
    seller_id = d["seller_id"]
    currency = d.get("currency") or "RUB"
    fee = d["amount"] * 0.01
    payout = d["amount"] * 0.99
    seller_text = (
        f"✅ <b>ПЛАТЁЖ ПОДТВЕРЖДЁН!</b>\n\n"
        f"✅ Покупатель @{buyer_uname} подтвердил оплату\n"
        f"📜 Сделка: #{deal_hash}\n"
        f"💼 Товар: {d['product_link']}\n"
        f"💸 Сумма: {d['amount']} {currency}\n\n"
        f"📂 Финансовые условия:\n"
        f"• Комиссия системы: 1% ({fee:.1f} {currency})\n"
        f"• К зачислению на баланс: {payout:.1f} {currency}\n\n"
        f"ТРЕБУЕТСЯ ВАШЕ ДЕЙСТВИЕ:\n"
        f"1. Передайте товар менеджеру https://t.me/{MANAGER}\n"
        f"2. После передачи нажмите кнопку ниже\n"
        f"3. Менеджер подтвердит получение товара\n"
        f"4. Сумма {payout:.1f} {currency} будет зачислена на ваш баланс\n\n"
        f"❌ Не передавайте товар покупателю напрямую!"
    )
    try:
        await bot.send_message(seller_id, seller_text, reply_markup=seller_confirm_kb(deal_hash, lang), parse_mode="HTML")
    except Exception as e:
        log.error(f"send_message to seller failed: {e}")
    await cb.answer("✅ Оплата подтверждена!", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await cb.message.answer(f"✅ Вы подтвердили оплату. Ожидайте передачи товара менеджером.\nhttps://t.me/{MANAGER}")


# ─── Продавец "Готово — передано" ────────────────────────────

@r.callback_query(F.data.startswith("seller_done_"))
async def cb_seller_done(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    deal_hash = cb.data.replace("seller_done_", "")
    d = await get_deal_by_hash(deal_hash)
    if not d:
        await cb.answer("❌ Сделка не найдена", show_alert=True); return
    if d["seller_id"] != cb.from_user.id:
        await cb.answer("❌ Это не ваша сделка", show_alert=True); return
    currency = d.get("currency") or "RUB"
    payout = d["amount"] * 0.99
    await update_deal(deal_hash, status="completed")
    await update_balance(cb.from_user.id, "rub", payout)
    user_data = await get_user(cb.from_user.id)
    await update_user(cb.from_user.id, deals_count=user_data["deals_count"] + 1)
    text = (
        f"✅ <b>Заявка на передачу отправлена!</b>\n\n"
        f"📂 Сделка: #{deal_hash}\n"
        f"💼 Товар: {d['product_link']}\n"
        f"💸 К зачислению: {payout:.1f} {currency}\n\n"
        f"Ожидайте подтверждения менеджера."
    )
    await cb.message.edit_text(text, reply_markup=main_kb(lang), parse_mode="HTML")
    await cb.answer()
    buyer_id = d.get("buyer_id")
    if buyer_id:
        try:
            await bot.send_message(buyer_id, f"✅ Сделка #{deal_hash} завершена! Товар передан менеджеру.", reply_markup=main_kb(lang), parse_mode="HTML")
        except: pass


# ─── Продавец отменил сделку ──────────────────────────────────

@r.callback_query(F.data.startswith("seller_cancel_"))
async def cb_seller_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    deal_hash = cb.data.replace("seller_cancel_", "")
    d = await get_deal_by_hash(deal_hash)
    if not d:
        await cb.answer("❌ Сделка не найдена", show_alert=True); return
    if d["seller_id"] != cb.from_user.id:
        await cb.answer("❌ Это не ваша сделка", show_alert=True); return
    await update_deal(deal_hash, status="cancelled")
    await cb.message.edit_text(f"❌ Сделка #{deal_hash} отменена.", reply_markup=main_kb(lang))
    await cb.answer()
    buyer_id = d.get("buyer_id")
    if buyer_id:
        try:
            await bot.send_message(buyer_id, f"❌ Сделка #{deal_hash} отменена продавцом.", reply_markup=main_kb(lang), parse_mode="HTML")
        except: pass


# ─── Баланс ───────────────────────────────────────────────────

@r.callback_query(F.data == "my_balance")
async def c_balance(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    text = tr("balance", u["language"], rub=u["balance_rub"], ton=u["balance_ton"], stars=u["balance_stars"], deals=u["deals_count"])
    try:
        await cb.message.delete()
    except: pass
    try:
        await cb.message.answer_video(video=GIF_BALANCE, caption=text, reply_markup=balance_kb(lang), parse_mode="HTML")
    except:
        try:
            await cb.message.answer_animation(animation=GIF_BALANCE, caption=text, reply_markup=balance_kb(lang), parse_mode="HTML")
        except:
            await cb.message.answer(text, reply_markup=balance_kb(lang), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "withdraw")
async def c_withdraw(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    if u["balance_rub"] <= 0:
        await cb.answer("❌ Недостаточно средств", show_alert=True); return
    await state.set_state(WithFSM.amount)
    await cb.message.edit_text(tr("enter_withdraw", u["language"]))
    await cb.answer()

@r.message(WithFSM.amount)
async def m_withdraw(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    try:
        amount = float(msg.text.replace(",", ".").strip())
        if amount <= 0: raise ValueError
    except:
        await msg.answer(tr("bad_amount", u["language"])); return
    if u["balance_rub"] < amount:
        await msg.answer(tr("no_balance", u["language"])); return
    await msg.answer("❌ Вывод временно недоступен", reply_markup=main_kb(lang))
    await state.clear()


# ─── Успешные сделки ──────────────────────────────────────────

@r.callback_query(F.data == "successful")
async def c_successful(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    deals = await get_user_deals(cb.from_user.id)
    if not deals:
        await cb.message.edit_text(tr("no_deals", u["language"]), reply_markup=main_kb(lang))
    else:
        h = "\n".join(f"{'✅' if d['status']=='completed' else '⏳'} #{d['deal_hash']} | {d['amount']} {d.get('currency','RUB')} | {PRODUCT_NAMES.get(d['product_type'], d['product_type'])}" for d in deals)
        await cb.message.edit_text(tr("history", u["language"], h=h), reply_markup=main_kb(lang), parse_mode="HTML")
    await cb.answer()


# ─── Смена языка ──────────────────────────────────────────────

@r.callback_query(F.data == "change_lang")
async def c_clang(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except: pass
    kb_lang = lang_kb()
    sent = None
    try:
        sent = await cb.message.answer_video(video=GIF_LANG, caption="🌐 Выберите язык / Select language:", reply_markup=kb_lang)
    except:
        try:
            sent = await cb.message.answer_animation(animation=GIF_LANG, caption="🌐 Выберите язык / Select language:", reply_markup=kb_lang)
        except:
            sent = await cb.message.answer("🌐 Выберите язык:", reply_markup=kb_lang)
    await cb.answer()
    if not sent:
        log.error("Failed to send language keyboard")


@r.callback_query(F.data.startswith("lang_"))
async def c_lang(cb: CallbackQuery):
    lang = cb.data.replace("lang_", "")
    log.info(f"Language change: user={cb.from_user.id}, lang={lang}")
    await update_user(cb.from_user.id, language=lang)
    await cb.answer("✅ Language changed!" if lang == "en" else "✅ Язык изменён!", show_alert=True)
    try:
        await cb.message.delete()
    except:
        pass
    await cb.message.answer(tr("welcome", lang), reply_markup=main_kb(lang))


# ─── Реквизиты ────────────────────────────────────────────────

@r.callback_query(F.data == "my_requisites")
async def c_req(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await cb.message.edit_text(tr("requisites", u["language"], card=u.get("requisites_card") or "❌", ton=u.get("requisites_ton") or "❌", region=u.get("requisites_region") or "—"), reply_markup=req_kb(lang), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "add_card")
async def c_addcard(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.set_state(ReqFSM.region)
    await cb.message.edit_text(tr("select_region", u["language"]) if False else "Регион:", reply_markup=region_kb(lang))
    await cb.answer()

@r.callback_query(F.data.startswith("reg_"))
async def c_region(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    await state.update_data(region=cb.data.replace("reg_", ""))
    await state.set_state(ReqFSM.card)
    await cb.message.edit_text(tr("enter_card", u["language"]))
    await cb.answer()

@r.message(ReqFSM.card)
async def m_card(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    card = msg.text.strip().replace(" ", "").replace("-", "")
    data = await state.get_data()
    region = data.get("region", "ru")
    await update_user(msg.from_user.id, requisites_card=card, requisites_region=region)
    await msg.answer(tr("card_ok", u["language"]), reply_markup=main_kb(lang))
    await state.clear()

@r.callback_query(F.data == "add_ton")
async def c_addton(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    await state.set_state(ReqFSM.ton)
    await cb.message.edit_text(tr("enter_ton", u["language"]))
    await cb.answer()

@r.message(ReqFSM.ton)
async def m_ton(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    wallet = msg.text.strip()
    if not validate_wallet(wallet):
        await msg.answer(tr("bad_wallet", u["language"])); return
    await update_user(msg.from_user.id, requisites_ton=wallet)
    await msg.answer(tr("ton_ok", u["language"]), reply_markup=main_kb(lang))
    await state.clear()


# ─── Админ ────────────────────────────────────────────────────

@r.message(Command("admintrepall"))
async def m_admin(msg: Message):
    if not is_admin(msg.from_user.id): return
    await msg.answer("🔧 <b>Админ-панель</b>", reply_markup=admin_kb(), parse_mode="HTML")

@r.callback_query(F.data == "admin_menu")
async def c_admin_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    await cb.message.edit_text("🔧 <b>Админ-панель</b>", reply_markup=admin_kb(), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "admin_deals")
async def c_admin_deals(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    await cb.message.edit_text("📊 <b>Управление сделками</b>", reply_markup=admin_deals_kb(), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "admin_stats")
async def c_admin_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    u = await get_user(cb.from_user.id)
    stats = await get_deal_stats()
    await cb.message.edit_text(tr("admin_deals", u["language"], total=stats["total"], completed=stats["completed"], pending=stats["pending"]), reply_markup=admin_deals_kb(), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "admin_all_deals")
async def c_admin_all_deals(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    u = await get_user(cb.from_user.id)
    deals = await get_all_deals()
    if not deals:
        await cb.message.edit_text(tr("admin_no_deals", u["language"]), reply_markup=admin_deals_kb()); await cb.answer(); return
    lines = []
    for d in deals[:20]:
        st = {"completed": "✅", "pending": "⏳", "cancelled": "❌"}.get(d["status"], "⏳")
        lines.append(f"{st} <code>#{d['deal_hash']}</code> | {d['amount']} {d.get('currency','RUB')} | {PRODUCT_NAMES.get(d['product_type'], d['product_type'])} | @{d.get('seller_username','?')}")
    await cb.message.edit_text("📋 <b>Последние сделки:</b>\n\n" + "\n".join(lines), reply_markup=admin_deals_kb(), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "admin_promos")
async def c_admin_promos(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    await cb.message.edit_text("📋 <b>Управление промокодами</b>", reply_markup=admin_promo_kb(), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "admin_all_promos")
async def c_admin_all_promos(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    promos = await get_all_promocodes()
    if not promos:
        await cb.message.edit_text("Нет промокодов", reply_markup=admin_promo_kb()); await cb.answer(); return
    lines = [f"<code>{p['code']}</code> | {p['amount']} RUB | Использований: {p['used_count']}/{p['max_uses']}" for p in promos]
    await cb.message.edit_text("📋 <b>Промокоды:</b>\n\n" + "\n".join(lines), reply_markup=admin_promo_kb(), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "admin_create_promo")
async def c_admin_create_promo(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(PromoFSM.amount)
    await cb.message.edit_text("Введите сумму промокода (RUB):")
    await cb.answer()

@r.message(PromoFSM.amount)
async def m_admin_promo_amount(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        amount = float(msg.text.replace(",", ".").strip())
        if amount <= 0: raise ValueError
    except:
        await msg.answer("❌ Введите число больше 0"); return
    await state.update_data(promo_amount=amount)
    await state.set_state(PromoFSM.max_uses)
    await msg.answer("Максимальное количество использований:")

@r.message(PromoFSM.max_uses)
async def m_admin_promo_uses(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        max_uses = int(msg.text.strip())
        if max_uses <= 0: raise ValueError
    except:
        await msg.answer("❌ Введите целое число больше 0"); return
    data = await state.get_data()
    amount = data.get("promo_amount")
    if amount is None:
        await msg.answer("❌ Ошибка. Начните заново.", reply_markup=admin_kb()); await state.clear(); return
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    try:
        await add_promocode(code, amount, max_uses, msg.from_user.id)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}", reply_markup=admin_kb()); await state.clear(); return
    await msg.answer(
        f"✅ <b>Промокод создан!</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Сумма: {amount} RUB\n"
        f"Макс. использований: {max_uses}\n\n"
        f"Ссылка для активации:\n"
        f"<code>https://t.me/{BOT_USERNAME}?start=promo_{code}</code>",
        reply_markup=admin_kb(), parse_mode="HTML")
    await state.clear()


# ─── Запуск ───────────────────────────────────────────────────

async def handle_health(request):
    return web.Response(text="OK")

async def main():
    if not BOT_TOKEN:
        print("ОШИБКА: Создай файл .env с BOT_TOKEN=...")
        return

    db_init()
    log.info("БД инициализирована")

    global bot, BOT_USERNAME
    bot = Bot(token=BOT_TOKEN)
    me = await bot.get_me()
    BOT_USERNAME = me.username
    log.info(f"Бот: @{BOT_USERNAME}")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(r)

    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(getenv("PORT", "8080")))
    await site.start()
    log.info(f"Web server started on port {getenv('PORT', '8080')}")

    print("=" * 40)
    print(f"  NotCoin | MRKT запущен! @{BOT_USERNAME}")
    print("=" * 40)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
