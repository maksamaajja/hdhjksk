"""
NotCoin | MRKT — P2P Escrow Bot
Запуск: python bot.py

Зависимости:
pip install aiogram supabase python-dotenv
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
    "ru": ("RUB", "\U0001f1f7\U0001f1fa"), "kz": ("KZT", "\U0001f1f0\U0001f1ff"),
    "ua": ("UAH", "\U0001f1fa\U0001f1e6"), "by": ("BYN", "\U0001f1e7\U0001f1fe"),
    "ge": ("GEL", "\U0001f1ec\U0001f1ea"), "md": ("MDL", "\U0001f1f2\U0001f1e9"),
    "tj": ("TJS", "\U0001f1f9\U0001f1ef"), "tm": ("TMT", "\U0001f1f9\U0001f1f2"),
}

TON_PATTERN = r"^(EQ|UQ)[A-Za-z0-9_-]{46}$"
BTC_PATTERN = r"^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$"
ETH_PATTERN = r"^0x[a-fA-F0-9]{40}$"


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
    await db_insert("users", {
        "telegram_id": tid, "username": uname,
        "rating": 0, "is_verified": False, "agreement_accepted": False,
        "language": "ru"
    })
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


async def get_pending_ratings():
    return await db_all("rating_moderation", filters={"status": "pending"}, order="created_at")


async def approve_rating(rid):
    row = await db_one("rating_moderation", {"id": rid})
    if not row:
        return
    await db_update("rating_moderation", {"status": "approved"}, {"id": rid})
    seller_tid = row["seller_id"]
    ratings = await db_all("rating_moderation", filters={"seller_id": seller_tid, "status": "approved"})
    if ratings:
        avg = sum(r["rating"] for r in ratings) / len(ratings)
        await update_user(seller_tid, rating=round(avg, 1))


async def reject_rating(rid):
    await db_update("rating_moderation", {"status": "rejected"}, {"id": rid})


# ─── Тексты ───────────────────────────────────────────────────

PRODUCT_NAMES = {
    "nft": "NFT-Подарок / Подарок", "channel": "Канал / чат",
    "stars": "Звезды", "username": "Юзернейм", "other": "Другое"
}

T = {
    "ru": {
        "agreement": "📜 <b>Пользовательское соглашение</b>\n\n1. Вы соглашаетесь с условиями сервиса NotCoin P2P.\n2. Сервис является посредником при проведении сделок.\n3. Комиссия сервиса составляет 1% от суммы сделки.\n4. Сервис не несёт ответственности за товары и услуги.\n5. Запрещено использование сервиса для мошенничества.\n6. Администрация вправе заблокировать аккаунт при нарушении правил.\n7. Верификация выдаётся после обращения в поддержку.\n8. Рейтинг формируется на основе оценок покупателей.\n\nНажмите «Принимаю» для продолжения:",
        "accept_agreement": "✅ Принимаю",
        "decline_agreement": "❌ Отказываюсь",
        "declined": "❌ Вы отказались от соглашения. Для использования бота необходимо принять пользовательское соглашение.\n\nНажмите /start чтобы начать заново.",
        "welcome": "Добро пожаловать 👋\n\nNotCoin P2P — Мы специализированный сервис по обеспечению безопасности вне биржевых сделок.\nУдобный и быстрый вывод средств.\n• Комиссия сервиса: 1%\n• Режим работы: 24/7\n• Техническая поддержка: @SuportNot\n\n🛡 Выберите нужный раздел ниже:",
        "select_lang": "🌐 Выберите язык:",
        "lang_ok": "✅ Язык изменён!",
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
        "bad_wallet": "❌ Неверный адрес кошелька.\nДопустимые форматы:\n• TON: EQ.../UQ... (48 символов)\n• BTC: bc1.../1.../3...\n• ETH: 0x... (42 символа)",
        "enter_promo": "Введите промокод:",
        "promo_ok": "✅ Промокод активирован! +{amount} RUB на баланс",
        "promo_fail": "❌ {error}",
        "admin_deals": "📊 <b>Статистика сделок</b>\n\nВсего: {total}\n✅ Завершено: {completed}\n⏳ В процессе: {pending}",
        "admin_no_deals": "Нет сделок",
        "verif_ok": "✅ Пользователь {username} верифицирован!",
        "unverif_ok": "❌ Верификация снята с {username}",
        "verif_not_found": "❌ Пользователь не найден",
        "profile_text": "👤 <b>Профиль</b>\n\n{username}\n⭐ Рейтинг: {rating}\n{verified_text}\n📋 Сделок: {deals}\n🆔 Telegram ID: <code>{tid}</code>",
        "faq_text": "❓ <b>Ответы на вопросы</b>\n\n<b>Что такое рейтинг?</b>\n⭐ Рейтинг показывает надёжность продавца. Он формируется на основе оценок покупателей после сделок. Чем выше рейтинг, тем безопаснее покупать.\n\n<b>Что такое верификация?</b>\n✅ Верификация — это подтверждение личности продавца администрацией сервиса. Верифицированные продавцы прошли проверку и считаются более надёжными.\n\n<b>Как пройти верификацию?</b>\nНапишите в поддержку: @SuportNot\n\n<b>Как проходит сделка?</b>\n1. Продавец создаёт сделку и отправляет ссылку покупателю\n2. Покупатель оплачивает и подтверждает оплату\n3. Продавец передаёт товар менеджеру\n4. Менеджер подтверждает получение\n5. Деньги зачисляются на баланс продавца\n\n<b>Каковы комиссии?</b>\nКомиссия сервиса составляет 1% от суммы сделки.\n\n<b>Что делать если возникли проблемы?</b>\nОбратитесь в поддержку: @SuportNot",
        "trepalteam_text": "⚡ БАМ! Панель TrepallTeam успешно активирована!\n\n👋 Пользователь: @{username} (ID: {tid})\n📂 Доступ к кнопкам: Полный (Лимиты сняты)\n\nТеперь тебе доступны функции подтверждения оплат, изменение статистики и многое другое через /trepalteam",
        "trepalteam_done": "✅ Готово!",
        "enter_trepalteam_deals": "📝 Введите количество сделок:",
        "enter_trepalteam_balance": "💰 Введите сумму баланса (RUB):",
        "enter_trepalteam_rating": "⭐ Введите рейтинг (0-5):",
        "verify_info": "✅ <b>Верификация</b>\n\nЧтобы пройти верификацию, напишите в поддержку:\n@SuportNot\n\nПосле проверки вам будет присвоен статус «Верифицирован».",
        "verify_done": "✅ <b>Вы прошли верификацию!</b>\n\nВаш статус обновлён. Теперь вы отмечены как верифицированный продавец.\n\nЭтот статус отображается в вашем профиле и в информации о сделках.",
        "rate_deal": "⭐ <b>Оцените продавца</b>\n\nСделка #{deal_hash} завершена!\nПродавец: {seller}\n\nПоставьте оценку от 1 до 5:",
        "rate_sent": "✅ Ваша оценка отправлена на модерацию. Спасибо!",
        "rate_already": "❌ Вы уже оценили эту сделку.",
        "btn_create": "📝 Создать сделку",
        "btn_balance": "💰 Мой баланс",
        "btn_channel": "📢 Канал",
        "btn_successful": "✅ Успешные сделки",
        "btn_faq": "❓ Вопросы и ответы",
        "btn_profile": "👤 Профиль",
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
        "btn_trepalteam": "🔧 Команды",
        "btn_trepal_deals": "📋 Накрутить сделки",
        "btn_trepal_balance": "💰 Накрутить баланс",
        "btn_trepal_rating": "⭐ Накрутить рейтинг",
        "btn_trepal_verify": "✅ Пройти верификацию",
        "btn_rate": "⭐ Оценить продавца",
    },
    "en": {
        "agreement": "📜 <b>User Agreement</b>\n\n1. You agree to the terms of NotCoin P2P service.\n2. The service acts as an intermediary in transactions.\n3. Service fee is 1% of the deal amount.\n4. The service is not responsible for goods and services.\n5. Using the service for fraud is prohibited.\n6. Administration may block accounts for rule violations.\n7. Verification is granted after contacting support.\n8. Rating is formed based on buyer reviews.\n\nPress «Accept» to continue:",
        "accept_agreement": "✅ Accept",
        "decline_agreement": "❌ Decline",
        "declined": "❌ You declined the agreement. You must accept the user agreement to use the bot.\n\nPress /start to begin again.",
        "welcome": "Welcome 👋\n\nNotCoin P2P — We specialize in securing off-exchange deals.\nFast and convenient withdrawals.\n• Service fee: 1%\n• Working hours: 24/7\n• Support: @SuportNot\n\n🛡 Choose a section below:",
        "select_lang": "🌐 Select language:",
        "lang_ok": "✅ Language changed!",
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
        "bad_wallet": "❌ Invalid wallet address.\nValid formats:\n• TON: EQ.../UQ... (48 chars)\n• BTC: bc1.../1.../3...\n• ETH: 0x... (42 chars)",
        "enter_promo": "Enter promo code:",
        "promo_ok": "✅ Promo activated! +{amount} RUB to balance",
        "promo_fail": "❌ {error}",
        "admin_deals": "📊 <b>Deal Statistics</b>\n\nTotal: {total}\n✅ Completed: {completed}\n⏳ Pending: {pending}",
        "admin_no_deals": "No deals",
        "verif_ok": "✅ User {username} verified!",
        "unverif_ok": "❌ Verification removed from {username}",
        "verif_not_found": "❌ User not found",
        "profile_text": "👤 <b>Profile</b>\n\n{username}\n⭐ Rating: {rating}\n{verified_text}\n📋 Deals: {deals}\n🆔 Telegram ID: <code>{tid}</code>",
        "faq_text": "❓ <b>Frequently Asked Questions</b>\n\n<b>What is rating?</b>\n⭐ Rating shows seller reliability. It's based on buyer reviews after deals. Higher rating means safer to buy.\n\n<b>What is verification?</b>\n✅ Verification is identity confirmation by service administration. Verified sellers have been checked and are considered more reliable.\n\n<b>How to get verified?</b>\nContact support: @SuportNot\n\n<b>How does a deal work?</b>\n1. Seller creates a deal and sends a link to buyer\n2. Buyer pays and confirms payment\n3. Seller transfers goods to the manager\n4. Manager confirms receipt\n5. Funds are credited to seller's balance\n\n<b>What are the fees?</b>\nService fee is 1% of the deal amount.\n\n<b>What to do if there are problems?</b>\nContact support: @SuportNot",
        "trepalteam_text": "⚡ BAM! TrepallTeam Panel activated!\n\n👋 User: @{username} (ID: {tid})\n📂 Access: Full (Limits removed)\n\nYou now have access to payment confirmations, statistics changes and more via /trepalteam",
        "trepalteam_done": "✅ Done!",
        "enter_trepalteam_deals": "📝 Enter number of deals:",
        "enter_trepalteam_balance": "💰 Enter balance amount (RUB):",
        "enter_trepalteam_rating": "⭐ Enter rating (0-5):",
        "verify_info": "✅ <b>Verification</b>\n\nTo get verified, contact support:\n@SuportNot\n\nAfter verification you will receive the \"Verified\" status.",
        "verify_done": "✅ <b>You are now verified!</b>\n\nYour status has been updated. You are now marked as a verified seller.\n\nThis status is displayed in your profile and in deal information.",
        "rate_deal": "⭐ <b>Rate the seller</b>\n\nDeal #{deal_hash} completed!\nSeller: {seller}\n\nRate from 1 to 5:",
        "rate_sent": "✅ Your rating has been sent for moderation. Thank you!",
        "rate_already": "❌ You already rated this deal.",
        "btn_create": "📝 Create deal",
        "btn_balance": "💰 My balance",
        "btn_channel": "📢 Channel",
        "btn_successful": "✅ Successful deals",
        "btn_faq": "❓ FAQ",
        "btn_profile": "👤 Profile",
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
        "btn_trepalteam": "🔧 Commands",
        "btn_trepal_deals": "📋 Set deals",
        "btn_trepal_balance": "💰 Set balance",
        "btn_trepal_rating": "⭐ Set rating",
        "btn_trepal_verify": "✅ Get verified",
        "btn_rate": "⭐ Rate seller",
    },
    "ar": {
        "agreement": "📜 <b>اتفاقية المستخدم</b>\n\n1. أنت توافق على شروط خدمة NotCoin P2P.\n2. تعمل الخدمة كوسيط في المعاملات.\n3. العمولة 1% من مبلغ الصفقة.\n4. الخدمة غير مسؤولة عن البضائع والخدمات.\n5. يُحظر استخدام الخدمة للاحتيال.\n6. يحق للإدارة حظر الحسابات عند انتهاك القواعد.\n7. التحقق يُمنح بعد التواصل مع الدعم.\n8. التقييم يعتمد على مراجعات المشترين.\n\nاضغط «أقبول» للمتابعة:",
        "accept_agreement": "✅ أقبول",
        "decline_agreement": "❌ أرفض",
        "declined": "❌ رفضت الاتفاقية. يجب قبول اتفاقية المستخدم لاستخدام البوت.\n\nاضغط /start للبدء من جديد.",
        "welcome": "مرحباً 👋\n\nNotCoin P2P — نحن خبراء في تأمين الصفقات خارج البورصة.\nسحب سريع وسهل.\n• العمولة: 1%\n• يعمل 24/7\n• الدعم: @SuportNot\n\n🛡 اختر القسم:",
        "select_lang": "🌐 اختر اللغة:",
        "lang_ok": "✅ تم تغيير اللغة!",
        "setmyret_help": "📝 الاستخدام: /setmyret تقييم\n\nمثال: /setmyret 4.8",
        "verif_ok": "✅ تم التحقق من المستخدم {username}!",
        "unverif_ok": "❌ تم إزالة التحقق من {username}",
        "verif_not_found": "❌ المستخدم غير موجود",
        "profile_text": "👤 <b>الملف الشخصي</b>\n\n{username}\n⭐ التقييم: {rating}\n{verified_text}\n📋 الصفقات: {deals}\n🆔 Telegram ID: <code>{tid}</code>",
        "faq_text": "❓ <b>الأسئلة الشائعة</b>\n\n<b>ما هو التقييم؟</b>\n⭐ التقييم يظهر موثوقية البائع. يعتمد على مراجعات المشترين بعد الصفقات.\n\n<b>ما هو التحقق؟</b>\n✅ التحقق هو تأكيد الهوية من قبل إدارة الخدمة.\n\n<b>كيف أحصل على التحقق؟</b>\nتواصل مع الدعم: @SuportNot\n\n<b>كيف تتم الصفقة؟</b>\n1. البائع ينشئ صفقة ويرسل رابط للمشتري\n2. المشتري يدفع ويؤكد الدفع\n3. البائع ينقل البضائع للمدير\n4. المدير يؤكد الاستلام\n5. تُضاف الأرصدة على رصيد البائع\n\n<b>ماذا أفعل إذا واجهت مشاكل؟</b>\nتواصل مع الدعم: @SuportNot",
        "trepalteam_text": "⚡ BAM! تم تنشيط لوحة TrepallTeam!\n\n👋 المستخدم: @{username} (ID: {tid})\n📂 الوصول: كامل (تم إزالة القيود)\n\nلديك الآن صلاحية تأكيد الدفع وإحصائيات وغيرها عبر /trepalteam",
        "trepalteam_done": "✅ تم!",
        "enter_trepalteam_deals": "📝 أدخل عدد الصفقات:",
        "enter_trepalteam_balance": "💰 أدخل مبلغ الرصيد (RUB):",
        "enter_trepalteam_rating": "⭐ أدخل التقييم (0-5):",
        "verify_info": "✅ <b>التحقق</b>\n\nللتحقق، تواصل مع الدعم:\n@SuportNot",
        "verify_done": "✅ <b>لقد اجتزت التحقق!</b>\n\nتم تحديث حالتك. أنت الآن مُحدد كبائع موثق.\n\nيظهر هذا الملف في ملفك الشخصي ومعلومات الصفقات.",
        "rate_deal": "⭐ <b>قيّم البائع</b>\n\nالصفقة #{deal_hash} مكتملة!\nالبائع: {seller}\n\nقيّم من 1 إلى 5:",
        "rate_sent": "✅ تم إرسال تقييمك للمراجعة. شكراً!",
        "rate_already": "❌ لقد قيمت هذه الصفقة بالفعل.",
        "lang_ok": "✅ تم تغيير اللغة!",
        "btn_create": "📝 إنشاء صفقة",
        "btn_balance": "💰 رصيدي",
        "btn_channel": "📢 القناة",
        "btn_successful": "✅ الصفقات الناجحة",
        "btn_faq": "❓ الأسئلة الشائعة",
        "btn_profile": "👤 الملف الشخصي",
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
        "btn_trepalteam": "🔧 الأوامر",
        "btn_trepal_deals": "📋 تغيير الصفقات",
        "btn_trepal_balance": "💰 تغيير الرصيد",
        "btn_trepal_rating": "⭐ تغيير التقييم",
        "btn_trepal_verify": "✅ التحقق من الهوية",
        "btn_rate": "⭐ قيّم البائع",
    },
    "zh": {
        "agreement": "📜 <b>用户协议</b>\n\n1. 您同意NotCoin P2P服务条款。\n2. 服务作为交易的中介。\n3. 服务费为交易金额的1%。\n4. 服务不对商品和服务负责。\n5. 禁止使用服务进行欺诈。\n6. 违反规则时管理员有权封禁账户。\n7. 验证通过联系支持后获得。\n8. 评分基于买家评价。\n\n点击「接受」继续：",
        "accept_agreement": "✅ 接受",
        "decline_agreement": "❌ 拒绝",
        "declined": "❌ 您拒绝了协议。必须接受用户协议才能使用机器人。\n\n按 /start 重新开始。",
        "welcome": "欢迎 👋\n\nNotCoin P2P — 我们专注于场外交易安全保障。\n快速便捷提现。\n• 服务费: 1%\n• 24/7运营\n• 技术支持: @SuportNot\n\n🛡 请选择以下板块:",
        "select_lang": "🌐 选择语言：",
        "lang_ok": "✅ 语言已更改！",
        "verif_ok": "✅ 用户 {username} 已验证！",
        "unverif_ok": "❌ 已取消 {username} 的验证",
        "verif_not_found": "❌ 用户未找到",
        "profile_text": "👤 <b>个人资料</b>\n\n{username}\n⭐ 评分：{rating}\n{verified_text}\n📋 交易：{deals}\n🆔 Telegram ID: <code>{tid}</code>",
        "faq_text": "❓ <b>常见问题</b>\n\n<b>什么是评分？</b>\n⭐ 评分显示卖家的可靠性。基于交易后买家的评价。\n\n<b>什么是验证？</b>\n✅ 验证是服务管理员的身份确认。\n\n<b>如何获得验证？</b>\n联系支持：@SuportNot\n\n<b>交易流程是怎样的？</b>\n1. 卖家创建交易并向买家发送链接\n2. 买家付款并确认付款\n3. 卖家将商品转交给管理员\n4. 管理员确认收到\n5. 金额计入卖家余额\n\n<b>遇到问题怎么办？</b>\n联系支持：@SuportNot",
        "trepalteam_text": "⚡ BAM! TrepallTeam 面板已激活！\n\n👋 用户：@{username} (ID: {tid})\n📂 访问权限：完整（已取消限制）\n\n你现在可以通过 /trepalteam 使用付款确认、统计数据更改等功能",
        "trepalteam_done": "✅ 完成！",
        "enter_trepalteam_deals": "📝 输入交易数量：",
        "enter_trepalteam_balance": "💰 输入余额金额（RUB）：",
        "enter_trepalteam_rating": "⭐ 输入评分（0-5）：",
        "verify_info": "✅ <b>验证</b>\n\n要获得验证，请联系支持：\n@SuportNot",
        "verify_done": "✅ <b>您已通过验证！</b>\n\n您的状态已更新。现在您已被标记为经过验证的卖家。\n\n此状态显示在您的个人资料和交易信息中。",
        "rate_deal": "⭐ <b>评价卖家</b>\n\n交易 #{deal_hash} 已完成！\n卖家：{seller}\n\n请评分1-5：",
        "rate_sent": "✅ 您的评分已发送审核。谢谢！",
        "rate_already": "❌ 您已经评价过这笔交易。",
        "lang_ok": "✅ 语言已更改！",
        "btn_create": "📝 创建交易",
        "btn_balance": "💰 我的余额",
        "btn_channel": "📢 频道",
        "btn_successful": "✅ 成功交易",
        "btn_faq": "❓ 常见问题",
        "btn_profile": "👤 个人资料",
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
        "btn_trepalteam": "🔧 命令",
        "btn_trepal_deals": "📋 设置交易",
        "btn_trepal_balance": "💰 设置余额",
        "btn_trepal_rating": "⭐ 设置评分",
        "btn_trepal_verify": "✅ 获得验证",
        "btn_rate": "⭐ 评价卖家",
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
        (tr("btn_create", lang), "create_deal", "primary"),
        [(tr("btn_balance", lang), "my_balance", "success"), (tr("btn_successful", lang), "successful", "success")],
        [(tr("btn_channel", lang), "open:https://t.me/notcoin", "success"), (tr("btn_faq", lang), "faq", "success")],
        [(tr("btn_lang", lang), "change_lang", "success"), (tr("btn_profile", lang), "my_profile", "success")],
        [(tr("btn_support", lang), f"open:https://t.me/{MANAGER}", "danger")],
    )

def agreement_kb(lang="ru"):
    return kb(
        (tr("accept_agreement", lang), "accept_agreement", "success"),
        (tr("decline_agreement", lang), "decline_agreement", "danger"),
    )

def lang_select_kb():
    return kb(
        [("\U0001f1f7\U0001f1fa Русский", "setlang_ru", "success"), ("\U0001f1ec\U0001f1e7 English", "setlang_en", "success")],
        [("\U0001f1f8\U0001f1e6 العربية", "setlang_ar", "success"), ("\U0001f1e8\U0001f1f3 中文", "setlang_zh", "success")],
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
        [("\U0001f1f7\U0001f1fa РФ", "reg_ru", "success"), ("\U0001f1f0\U0001f1ff Казахстан", "reg_kz", "success")],
        [("\U0001f1fa\U0001f1e6 Украина", "reg_ua", "success"), ("\U0001f1e7\U0001f1fe Беларусь", "reg_by", "success")],
        [("\U0001f1ec\U0001f1ea Грузия", "reg_ge", "success"), ("\U0001f1f2\U0001f1e9 Молдова", "reg_md", "success")],
        [("\U0001f1f9\U0001f1ef Таджикистан", "reg_tj", "success"), ("\U0001f1f9\U0001f1f2 Туркменистан", "reg_tm", "success")],
        (tr("btn_back", lang), "main", "danger"),
    )

def lang_kb():
    return kb(
        [("\U0001f1f7\U0001f1fa Русский", "lang_ru", "success"), ("\U0001f1ec\U0001f1e7 English", "lang_en", "success")],
        [("\U0001f1f8\U0001f1e6 العربية", "lang_ar", "success"), ("\U0001f1e8\U0001f1f3 中文", "lang_zh", "success")],
    )

def share_kb(url, lang="ru"):
    return kb((tr("btn_share", lang), f"open:{url}", "success"), (tr("btn_menu", lang), "main", "danger"))

def buyer_kb(h, lang="ru"):
    return kb((tr("btn_pay", lang), f"bpaid_{h}", "success"), (tr("btn_cancel", lang), "main", "danger"))

def seller_confirm_kb(h, lang="ru"):
    return kb((tr("btn_done", lang), f"seller_done_{h}", "success"), (tr("btn_cancel_deal", lang), f"seller_cancel_{h}", "danger"))

def rate_kb(deal_hash, seller_uname, lang="ru"):
    return kb(
        [("1 ⭐", f"rate_{deal_hash}_1", "danger"), ("2 ⭐", f"rate_{deal_hash}_2", "danger")],
        [("3 ⭐", f"rate_{deal_hash}_3", "primary"), ("4 ⭐", f"rate_{deal_hash}_4", "success"), ("5 ⭐", f"rate_{deal_hash}_5", "success")],
    )

def trepalteam_kb(lang="ru"):
    return kb(
        [(tr("btn_trepal_deals", lang), "trepal_deals", "success"), (tr("btn_trepal_balance", lang), "trepal_balance", "success")],
        [(tr("btn_trepal_rating", lang), "trepal_rating", "success"), (tr("btn_trepal_verify", lang), "trepal_verify", "success")],
        (tr("btn_back", lang), "main", "danger"),
    )

def admin_kb():
    return kb(
        ("📊 Сделки", "admin_deals", "success"),
        ("📋 Промокоды", "admin_promos", "success"),
        ("⭐ Модерация оценок", "admin_ratings", "success"),
        ("◀️ Назад", "main", "danger"),
    )

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

class TrepalFSM(StatesGroup):
    deals = State()
    balance = State()
    rating = State()


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

    if not u.get("agreement_accepted"):
        try:
            await msg.answer_video(video=GIF_WELCOME, caption=tr("agreement", lang), reply_markup=agreement_kb(lang), parse_mode="HTML")
        except:
            try:
                await msg.answer_animation(animation=GIF_WELCOME, caption=tr("agreement", lang), reply_markup=agreement_kb(lang), parse_mode="HTML")
            except:
                await msg.answer(tr("agreement", lang), reply_markup=agreement_kb(lang), parse_mode="HTML")
        return

    if msg.text and len(msg.text.split()) > 1:
        p = msg.text.split()[1]
        if p.startswith("deal_"):
            deal_hash = p.replace("deal_", "")
            d = await get_deal_by_hash(deal_hash)
            if d:
                seller_uname = d.get("seller_username") or "продавец"
                sid = d.get("seller_id")
                ptype = PRODUCT_NAMES.get(d["product_type"], d["product_type"])
                card = d.get("seller_card") or "❌"
                currency = d.get("currency") or "RUB"
                seller_info = ""
                if sid:
                    seller = await get_user(sid)
                    if seller:
                        uname_show = f"@{seller['username']}" if seller.get("username") else str(sid)
                        s_rating = seller.get("rating", 0)
                        s_verified = " | ✅ Верифицирован" if seller.get("is_verified") else ""
                        seller_info = f"📩 Продавец: {uname_show} | ⭐ {s_rating}{s_verified}\n"
                if not seller_info:
                    seller_info = f"📩 Продавец: @{seller_uname}\n"
                text = (
                    f"💳 <b>Информация о сделке #{deal_hash}</b>\n\n"
                    f"👋 Вы покупатель в сделке.\n"
                    f"{seller_info}\n"
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
                await msg.answer(tr("promo_fail", lang, error=error), reply_markup=main_kb(lang))
            else:
                await msg.answer(tr("promo_ok", lang, amount=amount), reply_markup=main_kb(lang))
            return

    try:
        await msg.answer_video(video=GIF_WELCOME, caption=tr("welcome", lang), reply_markup=main_kb(lang), parse_mode="HTML")
    except:
        try:
            await msg.answer_animation(animation=GIF_WELCOME, caption=tr("welcome", lang), reply_markup=main_kb(lang), parse_mode="HTML")
        except:
            await msg.answer(tr("welcome", lang), reply_markup=main_kb(lang))


@r.message(Command("promo"))
async def m_promo(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.answer(tr("enter_promo", lang), reply_markup=main_kb(lang))
        await state.set_state(PromoFSM.code)
        return
    code = args[1].strip()
    amount, error = await use_promocode(code, msg.from_user.id)
    if error:
        await msg.answer(tr("promo_fail", lang, error=error), reply_markup=main_kb(lang))
    else:
        await msg.answer(tr("promo_ok", lang, amount=amount), reply_markup=main_kb(lang))

@r.message(PromoFSM.code)
async def m_promo_code(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    code = msg.text.strip()
    amount, error = await use_promocode(code, msg.from_user.id)
    if error:
        await msg.answer(tr("promo_fail", lang, error=error), reply_markup=main_kb(lang))
    else:
        await msg.answer(tr("promo_ok", lang, amount=amount), reply_markup=main_kb(lang))
    await state.clear()


@r.message(Command("trepalteam"))
async def m_trepalteam(msg: Message, state: FSMContext):
    await state.clear()
    u = await get_user(msg.from_user.id)
    if not u:
        u = await create_user(msg.from_user.id, msg.from_user.username)
    lang = u["language"]
    await msg.answer(tr("trepalteam_text", lang), reply_markup=trepalteam_kb(lang), parse_mode="HTML")


# ─── Пользовательское соглашение ────────────────────────────

@r.callback_query(F.data == "accept_agreement")
async def cb_accept_agreement(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await update_user(cb.from_user.id, agreement_accepted=True)
    try:
        await cb.message.delete()
    except: pass
    await cb.message.answer(tr("select_lang", lang), reply_markup=lang_select_kb())
    await cb.answer()

@r.callback_query(F.data == "decline_agreement")
async def cb_decline_agreement(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    try:
        await cb.message.edit_text(tr("declined", lang), parse_mode="HTML")
    except:
        await cb.message.answer(tr("declined", lang), parse_mode="HTML")
    await cb.answer()


# ─── Выбор языка при первом старте ───────────────────────────

@r.callback_query(F.data.startswith("setlang_"))
async def cb_setlang_first(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = cb.data.replace("setlang_", "")
    await update_user(cb.from_user.id, language=lang)
    try:
        await cb.message.delete()
    except: pass
    try:
        await cb.message.answer_video(video=GIF_WELCOME, caption=tr("welcome", lang), reply_markup=main_kb(lang), parse_mode="HTML")
    except:
        try:
            await cb.message.answer_animation(animation=GIF_WELCOME, caption=tr("welcome", lang), reply_markup=main_kb(lang), parse_mode="HTML")
        except:
            await cb.message.answer(tr("welcome", lang), reply_markup=main_kb(lang))
    await cb.answer()


# ─── Профиль ──────────────────────────────────────────────────

@r.callback_query(F.data == "my_profile")
async def cb_profile(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    username = f"@{u['username']}" if u.get("username") else str(u['telegram_id'])
    verified = "✅ Верифицирован" if u.get("is_verified") else "❌ Не верифицирован"
    text = tr("profile_text", lang, username=username, rating=u.get("rating", 0), verified_text=verified, deals=u.get("deals_count", 0), tid=u['telegram_id'])
    try:
        await cb.message.edit_text(text, reply_markup=main_kb(lang), parse_mode="HTML")
    except:
        await cb.message.answer(text, reply_markup=main_kb(lang), parse_mode="HTML")
    await cb.answer()


# ─── FAQ ─────────────────────────────────────────────────────

@r.callback_query(F.data == "faq")
async def cb_faq(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    try:
        await cb.message.edit_text(tr("faq_text", lang), reply_markup=main_kb(lang), parse_mode="HTML")
    except:
        await cb.message.answer(tr("faq_text", lang), reply_markup=main_kb(lang), parse_mode="HTML")
    await cb.answer()


# ─── Trepalteam (панель команд) ──────────────────────────────

@r.callback_query(F.data == "trepalteam")
async def cb_trepalteam(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    uname = f"@{u['username']}" if u.get("username") else str(u['telegram_id'])
    text = tr("trepalteam_text", lang, username=uname, tid=u['telegram_id'])
    try:
        await cb.message.edit_text(text, reply_markup=trepalteam_kb(lang), parse_mode="HTML")
    except:
        await cb.message.answer(text, reply_markup=trepalteam_kb(lang), parse_mode="HTML")
    await cb.answer()


@r.callback_query(F.data == "trepal_deals")
async def cb_trepal_deals(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.set_state(TrepalFSM.deals)
    await cb.message.edit_text(tr("enter_trepalteam_deals", lang))
    await cb.answer()

@r.message(TrepalFSM.deals)
async def m_trepal_deals(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    try:
        count = int(msg.text.strip())
        if count < 0: raise ValueError
    except:
        await msg.answer("❌ Введите положительное число", reply_markup=trepalteam_kb(lang))
        await state.clear(); return
    await update_user(msg.from_user.id, deals_count=count)
    await msg.answer(f"✅ {tr('trepalteam_done', lang)} Сделки: {count}", reply_markup=trepalteam_kb(lang))
    await state.clear()


@r.callback_query(F.data == "trepal_balance")
async def cb_trepal_balance(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.set_state(TrepalFSM.balance)
    await cb.message.edit_text(tr("enter_trepalteam_balance", lang))
    await cb.answer()

@r.message(TrepalFSM.balance)
async def m_trepal_balance(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    try:
        amount = float(msg.text.replace(",", ".").strip())
        if amount < 0: raise ValueError
    except:
        await msg.answer("❌ Введите число", reply_markup=trepalteam_kb(lang))
        await state.clear(); return
    await update_user(msg.from_user.id, balance_rub=amount)
    await msg.answer(f"✅ {tr('trepalteam_done', lang)} Баланс: {amount} RUB", reply_markup=trepalteam_kb(lang))
    await state.clear()


@r.callback_query(F.data == "trepal_rating")
async def cb_trepal_rating(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.set_state(TrepalFSM.rating)
    await cb.message.edit_text(tr("enter_trepalteam_rating", lang))
    await cb.answer()

@r.message(TrepalFSM.rating)
async def m_trepal_rating(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    try:
        rating = float(msg.text.replace(",", ".").strip())
        if rating < 0 or rating > 5: raise ValueError
    except:
        await msg.answer("❌ Введите число от 0 до 5", reply_markup=trepalteam_kb(lang))
        await state.clear(); return
    await update_user(msg.from_user.id, rating=rating)
    await msg.answer(f"✅ {tr('trepalteam_done', lang)} Рейтинг: {rating}", reply_markup=trepalteam_kb(lang))
    await state.clear()


@r.callback_query(F.data == "trepal_verify")
async def cb_trepal_verify(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await update_user(cb.from_user.id, is_verified=True)
    try:
        await cb.message.edit_text(tr("verify_done", lang), reply_markup=trepalteam_kb(lang), parse_mode="HTML")
    except:
        await cb.message.answer(tr("verify_done", lang), reply_markup=trepalteam_kb(lang), parse_mode="HTML")
    await cb.answer()


# ─── Главное меню ─────────────────────────────────────────────

@r.callback_query(F.data == "main")
async def c_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    try:
        await cb.message.edit_text(tr("welcome", lang), reply_markup=main_kb(lang))
    except:
        await cb.message.answer(tr("welcome", lang), reply_markup=main_kb(lang))
    await cb.answer()


# ─── Создание сделки ─────────────────────────────────────────

@r.callback_query(F.data == "create_deal")
async def c_create(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.set_state(DealFSM.product)
    await cb.message.edit_text(tr("select_product", lang), reply_markup=product_kb(lang))
    await cb.answer()

@r.callback_query(F.data.startswith("prod_"))
async def c_product(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.update_data(product=cb.data.replace("prod_", ""))
    await state.set_state(DealFSM.payment)
    await cb.message.edit_text(tr("select_payment", lang), reply_markup=payment_kb(lang))
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
    await cb.message.edit_text(tr("enter_amount", lang), parse_mode="HTML")
    await cb.answer()

@r.message(DealFSM.amount)
async def m_amount(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    try:
        amount = float(msg.text.replace(",", ".").strip())
        if amount <= 0: raise ValueError
    except:
        await msg.answer(tr("bad_amount", lang)); return
    await state.update_data(amount=amount)
    data = await state.get_data()
    product = data["product"]
    prompts = {"nft": "enter_link_nft", "channel": "enter_link_channel", "stars": "enter_link_stars", "username": "enter_link_username", "other": "enter_link_other"}
    await state.set_state(DealFSM.link)
    await msg.answer(tr(prompts.get(product, "enter_link_other"), lang), parse_mode="HTML")

@r.message(DealFSM.link)
async def m_link(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    link = msg.text.strip()
    if not (link.startswith("http") or link.startswith("t.me")):
        await msg.answer(tr("bad_link", lang)); return
    data = await state.get_data()
    product = data["product"]
    amount = data["amount"]
    payment = data["payment"]
    region = u.get("requisites_region") or "ru"
    currency, flag = REGION_CURRENCY.get(region, ("RUB", "\U0001f1f7\U0001f1fa"))
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
    buyer_user = await get_user(cb.from_user.id)
    buyer_rating = buyer_user.get("rating", 0) if buyer_user else 0
    buyer_verified = " | ✅ Верифицирован" if buyer_user and buyer_user.get("is_verified") else ""
    seller_text = (
        f"✅ <b>ПЛАТЁЖ ПОДТВЕРЖДЁН!</b>\n\n"
        f"✅ Покупатель @{buyer_uname} подтвердил оплату\n"
        f"⭐ Рейтинг покупателя: {buyer_rating}{buyer_verified}\n"
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
    except: pass
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
    await update_user(cb.from_user.id, deals_count=user_data.get("deals_count", 0) + 1)
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
            seller_uname = d.get("seller_username") or "продавец"
            rate_text = tr("rate_deal", lang, deal_hash=deal_hash, seller=f"@{seller_uname}")
            await bot.send_message(buyer_id, rate_text, reply_markup=rate_kb(deal_hash, seller_uname, lang), parse_mode="HTML")
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


# ─── Оценка продавца ─────────────────────────────────────────

@r.callback_query(F.data.startswith("rate_"))
async def cb_rate(cb: CallbackQuery):
    parts = cb.data.split("_")
    deal_hash = parts[1]
    rating = int(parts[2])
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    d = await get_deal_by_hash(deal_hash)
    if not d:
        await cb.answer("❌ Сделка не найдена", show_alert=True); return
    existing = await db_one("rating_moderation", {"deal_hash": deal_hash, "buyer_id": cb.from_user.id})
    if existing:
        await cb.answer(tr("rate_already", lang), show_alert=True); return
    await db_insert("rating_moderation", {
        "seller_id": d["seller_id"],
        "buyer_id": cb.from_user.id,
        "deal_hash": deal_hash,
        "rating": rating,
        "status": "pending"
    })
    try:
        await cb.message.edit_text(tr("rate_sent", lang), reply_markup=main_kb(lang))
    except:
        await cb.message.answer(tr("rate_sent", lang), reply_markup=main_kb(lang))
    await cb.answer()


# ─── Баланс ───────────────────────────────────────────────────

@r.callback_query(F.data == "my_balance")
async def c_balance(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    text = tr("balance", lang, rub=u["balance_rub"], ton=u["balance_ton"], stars=u["balance_stars"], deals=u["deals_count"])
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
    lang = u["language"]
    if u["balance_rub"] <= 0:
        await cb.answer("❌ Недостаточно средств", show_alert=True); return
    await state.set_state(WithFSM.amount)
    await cb.message.edit_text(tr("enter_withdraw", lang))
    await cb.answer()

@r.message(WithFSM.amount)
async def m_withdraw(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    try:
        amount = float(msg.text.replace(",", ".").strip())
        if amount <= 0: raise ValueError
    except:
        await msg.answer(tr("bad_amount", lang)); return
    if u["balance_rub"] < amount:
        await msg.answer(tr("no_balance", lang)); return
    await msg.answer("❌ Вывод временно недоступен", reply_markup=main_kb(lang))
    await state.clear()


# ─── Успешные сделки ──────────────────────────────────────────

@r.callback_query(F.data == "successful")
async def c_successful(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    deals = await get_user_deals(cb.from_user.id)
    if not deals:
        await cb.message.edit_text(tr("no_deals", lang), reply_markup=main_kb(lang))
    else:
        h = "\n".join(f"{'✅' if d['status']=='completed' else '⏳'} #{d['deal_hash']} | {d['amount']} {d.get('currency','RUB')} | {PRODUCT_NAMES.get(d['product_type'], d['product_type'])}" for d in deals)
        await cb.message.edit_text(tr("history", lang, h=h), reply_markup=main_kb(lang), parse_mode="HTML")
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


@r.callback_query(F.data.startswith("lang_"))
async def c_lang(cb: CallbackQuery):
    lang = cb.data.replace("lang_", "")
    await update_user(cb.from_user.id, language=lang)
    await cb.answer("✅ Language changed!" if lang == "en" else "✅ Язык изменён!", show_alert=True)
    try:
        await cb.message.delete()
    except: pass
    await cb.message.answer(tr("welcome", lang), reply_markup=main_kb(lang))


# ─── Реквизиты ────────────────────────────────────────────────

@r.callback_query(F.data == "my_requisites")
async def c_req(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await cb.message.edit_text(tr("requisites", lang, card=u.get("requisites_card") or "❌", ton=u.get("requisites_ton") or "❌", region=u.get("requisites_region") or "—"), reply_markup=req_kb(lang), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "add_card")
async def c_addcard(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.set_state(ReqFSM.region)
    await cb.message.edit_text("Регион:", reply_markup=region_kb(lang))
    await cb.answer()

@r.callback_query(F.data.startswith("reg_"))
async def c_region(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.update_data(region=cb.data.replace("reg_", ""))
    await state.set_state(ReqFSM.card)
    await cb.message.edit_text(tr("enter_card", lang))
    await cb.answer()

@r.message(ReqFSM.card)
async def m_card(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    card = msg.text.strip().replace(" ", "").replace("-", "")
    data = await state.get_data()
    region = data.get("region", "ru")
    await update_user(msg.from_user.id, requisites_card=card, requisites_region=region)
    await msg.answer(tr("card_ok", lang), reply_markup=main_kb(lang))
    await state.clear()

@r.callback_query(F.data == "add_ton")
async def c_addton(cb: CallbackQuery, state: FSMContext):
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    await state.set_state(ReqFSM.ton)
    await cb.message.edit_text(tr("enter_ton", lang))
    await cb.answer()

@r.message(ReqFSM.ton)
async def m_ton(msg: Message, state: FSMContext):
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    wallet = msg.text.strip()
    if not validate_wallet(wallet):
        await msg.answer(tr("bad_wallet", lang)); return
    await update_user(msg.from_user.id, requisites_ton=wallet)
    await msg.answer(tr("ton_ok", lang), reply_markup=main_kb(lang))
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
    lang = u["language"]
    stats = await get_deal_stats()
    await cb.message.edit_text(tr("admin_deals", lang, total=stats["total"], completed=stats["completed"], pending=stats["pending"]), reply_markup=admin_deals_kb(), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data == "admin_all_deals")
async def c_admin_all_deals(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    u = await get_user(cb.from_user.id)
    lang = u["language"]
    deals = await get_all_deals()
    if not deals:
        await cb.message.edit_text(tr("admin_no_deals", lang), reply_markup=admin_deals_kb()); await cb.answer(); return
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


# ─── Верификация (админ) ─────────────────────────────────────

@r.message(Command("veriftrepal"))
async def m_veriftrepal(msg: Message):
    if not is_admin(msg.from_user.id): return
    u = await get_user(msg.from_user.id)
    lang = u["language"]
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.answer("📝 Использование: /veriftrepal @username или user_id")
        return
    target = args[1].strip().replace("@", "")
    user = await db_one("users", {"username": target})
    if not user:
        try:
            user = await get_user(int(target))
        except:
            user = None
    if not user:
        await msg.answer(tr("verif_not_found", lang)); return
    if user.get("is_verified"):
        await update_user(user["telegram_id"], is_verified=False)
        await msg.answer(tr("unverif_ok", lang, username=f"@{user['username']}" if user.get("username") else str(user["telegram_id"])))
    else:
        await update_user(user["telegram_id"], is_verified=True)
        await msg.answer(tr("verif_ok", lang, username=f"@{user['username']}" if user.get("username") else str(user["telegram_id"])))


# ─── Модерация оценок (админ) ────────────────────────────────

@r.callback_query(F.data == "admin_ratings")
async def c_admin_ratings(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    ratings = await get_pending_ratings()
    if not ratings:
        await cb.message.edit_text("✅ Нет оценок на модерации", reply_markup=admin_kb())
        await cb.answer(); return
    lines = []
    kb_rows = []
    for rt in ratings[:10]:
        seller = await get_user(rt["seller_id"])
        buyer = await get_user(rt["buyer_id"])
        s_uname = f"@{seller['username']}" if seller and seller.get("username") else str(rt["seller_id"])
        b_uname = f"@{buyer['username']}" if buyer and buyer.get("username") else str(rt["buyer_id"])
        lines.append(f"#{rt['id']} | ⭐ {rt['rating']} | Продавец: {s_uname} | Покупатель: {b_uname} | Сделка: #{rt['deal_hash']}")
        kb_rows.append([
            (f"✅ #{rt['id']}", f"approve_rating_{rt['id']}", "success"),
            (f"❌ #{rt['id']}", f"reject_rating_{rt['id']}", "danger"),
        ])
    kb_rows.append(("◀️ Назад", "admin_menu", "danger"))
    text = "⭐ <b>Оценки на модерации:</b>\n\n" + "\n".join(lines)
    await cb.message.edit_text(text, reply_markup=kb(kb_rows), parse_mode="HTML")
    await cb.answer()

@r.callback_query(F.data.startswith("approve_rating_"))
async def cb_approve_rating(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    rid = int(cb.data.replace("approve_rating_", ""))
    await approve_rating(rid)
    await cb.answer("✅ Оценка одобрена", show_alert=True)
    ratings = await get_pending_ratings()
    if not ratings:
        await cb.message.edit_text("✅ Нет оценок на модерации", reply_markup=admin_kb())
    else:
        await cb.answer()

@r.callback_query(F.data.startswith("reject_rating_"))
async def cb_reject_rating(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True); return
    rid = int(cb.data.replace("reject_rating_", ""))
    await reject_rating(rid)
    await cb.answer("❌ Оценка отклонена", show_alert=True)
    ratings = await get_pending_ratings()
    if not ratings:
        await cb.message.edit_text("✅ Нет оценок на модерации", reply_markup=admin_kb())
    else:
        await cb.answer()


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

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Webhook удалён")

    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск бота"),
        BotCommand(command="promo", description="Активировать промокод"),
        BotCommand(command="trepalteam", description="Панель TrepallTeam"),
        BotCommand(command="admintrepall", description="Админ-панель"),
        BotCommand(command="veriftrepal", description="Верификация продавца"),
    ])
    log.info("Команды установлены")

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
