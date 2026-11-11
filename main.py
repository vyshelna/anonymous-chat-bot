"""
Анонимный Telegram-бот для общения 1 на 1 (aiogram 3).

ВОЗМОЖНОСТИ
- Поиск собеседника (очередь), анонимная пересылка текста, стикеров, фото, видео, голосовых и т.д.
- /stop (завершить), /next (следующий), кнопки «Искать собеседника», «Следующий», «Завершить».
- Капча-пример после отправки фото/видео/кружка (защита от рекламных ботов).
- /report: жалоба с передачей последних сообщений администратору (только после подтверждения).
- Платный «возврат собеседника» за Telegram Stars (с согласием собеседника, авто-возврат звёзд).
- /paysupport: поддержка по оплате.
- Меню команд (кнопка «Меню»): /next, /stop, /report, /return, /start, /rules.
- /rules: правила общения. Администратор: /ban <id> и /unban <id> (ID берётся из жалобы).

КАК ЗАПУСТИТЬ (подробно см. README.md)
1. Создайте бота у @BotFather командой /newbot и скопируйте токен.
2. Задайте переменные окружения: BOT_TOKEN (обязательно) и ADMIN_ID (ваш Telegram ID).
   Остальные настройки необязательны, их список есть в README.md и env.example.
3. Установите зависимости:   pip install -r requirements.txt
4. Запустите:               python main.py
"""

import asyncio
import logging
import os
import random
from collections import deque

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)

# ====================== НАСТРОЙКИ ======================
# Все настройки читаются из переменных окружения, код править не нужно.
def env_int(name: str, default: int) -> int:
    """Целое число из переменной окружения; если не задано или неверно, берётся default."""
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    """Да/нет из переменной окружения (1/true/yes/on/да)."""
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "y", "on", "да")


# Токен бота от @BotFather (в коде его хранить нельзя)
TOKEN = os.getenv("BOT_TOKEN")

# Тексты кнопок
BTN_SEARCH = "Искать собеседника"
BTN_STOP = "Завершить"
BTN_NEXT = "Следующий"
BTN_REPORT = "Пожаловаться"

# Капча после медиа: если пользователь отправил фото, видео (или кружок) в диалоге,
# перед новым поиском нужна проверка.
# True  — капча нужна, когда диалог закончился ЛЮБЫМ способом (сам или собеседник).
# False — только когда диалог завершил собеседник.
CAPTCHA_ON_OWN_END = env_bool("CAPTCHA_ON_OWN_END", True)

# Платный «возврат собеседника» (Telegram Stars, валюта XTR).
# RETURN_PRICE_STARS = 0 полностью отключает эту функцию.
RETURN_PRICE_STARS = env_int("RETURN_PRICE_STARS", 50)    # цена в звёздах
RETURN_REQUEST_TTL = env_int("RETURN_REQUEST_TTL", 60)    # секунд на ответ собеседника
PAYMENT_TTL = env_int("PAYMENT_TTL", 180)                 # секунд на оплату после согласия

# Жалобы (/report): Telegram ID администратора, которому уходят жалобы.
# Узнать свой ID можно у бота @userinfobot. Задаётся переменной окружения ADMIN_ID.
# Администратор должен хотя бы раз нажать /start в этом боте.
ADMIN_ID = env_int("ADMIN_ID", 0)
# Сколько последних сообщений диалога передаётся администратору при жалобе
REPORT_HISTORY = env_int("REPORT_HISTORY", 10)


def parse_ids(raw: str) -> set[int]:
    """Список Telegram ID через запятую -> множество чисел (неверные значения пропускаются)."""
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


# Правила общения (команда /rules). Текст можно заменить на свой.
RULES_TEXT = '''📌 Правила общения в анонимном чате:

1. Любые упоминания психоактивных веществ (наркотиков)

2. Детская порнография ("ЦП")

3. Мошенничество (Scam)

4. Любая реклама, спам

5. Продажи чего либо (например - продажа интимных фотографий, видео)

6. Любые действия, нарушающие правила Telegram

❌ За нарушение правил - блокировка аккаунта'''

# Сообщение заблокированному пользователю
BAN_TEXT = "🚫 Ваш аккаунт заблокирован за нарушение правил общения. Правила: /rules"

# Блокировки. Команда администратора /ban <id> действует до перезапуска бота.
# Чтобы блокировка была постоянной, добавьте ID в переменную BANNED_IDS (через запятую).

# ====================== КЛАВИАТУРЫ ======================
# Когда пользователь не в диалоге
kb_idle = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_SEARCH)]],
    resize_keyboard=True,
)
# Когда пользователь в диалоге
kb_chat = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_NEXT), KeyboardButton(text=BTN_STOP)],
        [KeyboardButton(text=BTN_REPORT)],
    ],
    resize_keyboard=True,
)
# Когда пользователь ждёт в очереди (кнопка «Завершить» отменяет поиск)
kb_wait = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_STOP)]],
    resize_keyboard=True,
)

# ====================== СОСТОЯНИЕ (в памяти) ======================
waiting: deque[int] = deque()   # очередь ожидающих собеседника
partners: dict[int, int] = {}   # user_id -> user_id собеседника (в обе стороны)
lock = asyncio.Lock()           # защита от гонок при одновременных запросах
sent_video: set[int] = set()    # кто отправлял видео в текущем диалоге
need_captcha: set[int] = set()  # кому нужно пройти капчу перед новым поиском
captcha_answers: dict[int, int] = {}  # user_id -> правильный ответ на текущую капчу
# Для жалоб храним ТОЛЬКО пары (кто отправил, id сообщения) последних сообщений диалога,
# сам текст переписки бот не хранит. Список общий для обоих участников диалога.
dialog_log: dict[int, deque] = {}
report_pending: dict[int, int] = {}   # user_id -> собеседник, на которого он собирается жаловаться
last_partner: dict[int, int] = {}     # user_id -> последний собеседник (для «вернуть собеседника»)
return_requests: dict[int, int] = {}  # собеседник -> тот, кто хочет вернуться (ждёт согласия)
pay_wait: dict[int, int] = {}         # покупатель -> собеседник (согласие получено, ждём оплату)
bg_tasks: set[asyncio.Task] = set()   # фоновые задачи (чтобы их не удалил сборщик мусора)

banned: set[int] = parse_ids(os.getenv("BANNED_IDS", ""))  # заблокированные пользователи

router = Router()


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
async def safe_send(bot: Bot, chat_id: int, text: str, kb=None) -> bool:
    """Отправка сообщения; False, если пользователь заблокировал бота."""
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


def make_captcha(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Создаёт простой пример со сложением и 4 вариантами ответа."""
    a, b = random.randint(2, 9), random.randint(2, 9)
    answer = a + b
    options = {answer}
    while len(options) < 4:
        options.add(max(2, answer + random.randint(-5, 5)))
    options = list(options)
    random.shuffle(options)
    captcha_answers[user_id] = answer
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=str(o), callback_data=f"cap:{o}") for o in options]]
    )
    return f"🤖 Проверка: сколько будет {a} + {b}?", kb


async def send_captcha(bot: Bot, user_id: int) -> None:
    text, kb = make_captcha(user_id)
    await safe_send(bot, user_id, text, kb)


async def offer_return(bot: Bot, user_id: int) -> None:
    """Предлагает вернуть последнего собеседника (платно, за звёзды)."""
    if RETURN_PRICE_STARS <= 0 or user_id not in last_partner:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💫 Вернуть собеседника", callback_data="ret:info")]]
    )
    await safe_send(bot, user_id, "💫 Хочешь продолжить общение?\nВерни собеседника в диалог 👇", kb)


async def notify_left(bot: Bot, partner: int, offer: bool = True) -> None:
    """Сообщает собеседнику, что диалог завершён (и просит капчу, если нужно)."""
    if partner in need_captcha:
        await safe_send(
            bot, partner,
            "Собеседник завершил диалог. Чтобы продолжить, пройдите быструю проверку 👇",
            kb_idle,
        )
        await send_captcha(bot, partner)
    else:
        await safe_send(bot, partner, "Собеседник завершил диалог.", kb_idle)
    if offer:
        await offer_return(bot, partner)


async def leave(user_id: int) -> tuple[int | None, bool]:
    """
    Убирает пользователя из очереди и/или из диалога.
    Возвращает (id бывшего собеседника или None, был ли в очереди).
    """
    async with lock:
        was_waiting = user_id in waiting
        if was_waiting:
            waiting.remove(user_id)
        partner = partners.pop(user_id, None)
        if partner is not None:
            partners.pop(partner, None)
            # Запоминаем последнего собеседника (для платного «вернуть собеседника»)
            last_partner[user_id] = partner
            last_partner[partner] = user_id
            # Собеседник отправлял фото/видео, а диалог завершил другой -> капча
            if partner in sent_video:
                need_captcha.add(partner)
                logging.info("Капча назначена пользователю (собеседник завершил диалог).")
            if CAPTCHA_ON_OWN_END and user_id in sent_video:
                need_captcha.add(user_id)
                logging.info("Капча назначена пользователю (завершил диалог сам).")
        # Новый диалог начнётся с чистого листа
        sent_video.discard(user_id)
        dialog_log.pop(user_id, None)
        report_pending.pop(user_id, None)
        if partner is not None:
            sent_video.discard(partner)
            dialog_log.pop(partner, None)
            report_pending.pop(partner, None)
        return partner, was_waiting


async def start_search(user_id: int, bot: Bot) -> None:
    """Ставит пользователя в очередь или сразу соединяет с ожидающим."""
    # Заблокированным поиск недоступен
    if user_id in banned:
        await safe_send(bot, user_id, BAN_TEXT, kb_idle)
        return
    # Сначала нужно пройти капчу, если она требуется
    if user_id in need_captcha:
        await safe_send(bot, user_id, "Сначала пройдите проверку 👇", kb_idle)
        await send_captcha(bot, user_id)
        return

    async with lock:
        if user_id in partners:
            state = "in_chat"
        elif user_id in waiting:
            state = "already_waiting"
        elif waiting:
            partner = waiting.popleft()
            partners[user_id] = partner
            partners[partner] = user_id
            log: deque = deque(maxlen=REPORT_HISTORY)
            dialog_log[user_id] = log
            dialog_log[partner] = log
            state = "matched"
        else:
            waiting.append(user_id)
            state = "queued"
            partner = None

    if state == "in_chat":
        await safe_send(bot, user_id, "Вы уже в диалоге. Нажмите «Следующий» или «Завершить».", kb_chat)
    elif state == "already_waiting":
        await safe_send(bot, user_id, "Вы уже в очереди, ищем собеседника…", kb_wait)
    elif state == "queued":
        await safe_send(bot, user_id, "🔍 Ищем собеседника…", kb_wait)
    else:  # matched
        text = "✅ Собеседник найден! Можете общаться. Все сообщения анонимны."
        ok_me = await safe_send(bot, user_id, text, kb_chat)
        ok_partner = await safe_send(bot, partner, text, kb_chat)
        # Если кто-то из двоих заблокировал бота — разрываем пару
        if not ok_me or not ok_partner:
            await leave(user_id)
            if ok_me:
                await safe_send(bot, user_id, "Собеседник недоступен. Нажмите «Искать собеседника».", kb_idle)
            if ok_partner:
                await safe_send(bot, partner, "Собеседник недоступен. Нажмите «Искать собеседника».", kb_idle)


# ====================== КОМАНДЫ И КНОПКИ ======================
def start_text() -> str:
    """Приветствие со списком команд (зависит от настроек)."""
    lines = [
        "👋 Это анонимный чат. Нажмите «Искать собеседника» (или /next), чтобы найти собеседника.",
        "",
        "/next — поиск собеседника",
        "/stop — завершить диалог",
        "/report — пожаловаться на собеседника",
    ]
    if RETURN_PRICE_STARS > 0:
        lines += [
            f"/return — вернуть собеседника ({RETURN_PRICE_STARS} ⭐)",
            "/paysupport — вопросы по оплате",
        ]
    lines += [
        "/rules — правила общения",
    ]
    return "\n".join(lines)


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message) -> None:
    await message.answer(start_text(), reply_markup=kb_idle)


@router.message(Command("rules"), F.chat.type == "private")
async def cmd_rules(message: Message) -> None:
    await message.answer(RULES_TEXT)


@router.message(Command("search"), F.chat.type == "private")
@router.message(F.text == BTN_SEARCH, F.chat.type == "private")
async def cmd_search(message: Message, bot: Bot) -> None:
    await start_search(message.from_user.id, bot)


@router.message(Command("stop"), F.chat.type == "private")
@router.message(F.text == BTN_STOP, F.chat.type == "private")
async def cmd_stop(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    partner, was_waiting = await leave(user_id)

    if partner is not None:
        await safe_send(bot, user_id, "Диалог завершён.", kb_idle)
        if user_id in need_captcha:  # только при CAPTCHA_ON_OWN_END = True
            await send_captcha(bot, user_id)
        await offer_return(bot, user_id)
        await notify_left(bot, partner)
    elif was_waiting:
        await safe_send(bot, user_id, "Поиск отменён.", kb_idle)
    else:
        await safe_send(bot, user_id, "Вы сейчас не в диалоге.", kb_idle)


@router.message(Command("next"), F.chat.type == "private")
@router.message(F.text == BTN_NEXT, F.chat.type == "private")
async def cmd_next(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    partner, _ = await leave(user_id)
    if partner is not None:
        await notify_left(bot, partner)
    # Сразу ищем нового (если нужна капча, start_search её запросит)
    await start_search(user_id, bot)


# ====================== ЖАЛОБЫ ======================
async def send_report(bot: Bot, reporter: int, offender: int, log: list) -> None:
    """Передаёт администратору последние сообщения диалога и ID участников."""
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🚨 Жалоба\nАвтор жалобы: {reporter}\nНа кого жалоба: {offender}\n"
            f"Последние сообщения ({len(log)}), от старых к новым:",
        )
        last_sender = None
        for sender, msg_id in log:
            if sender != last_sender:
                who = "Автор жалобы" if sender == reporter else "На кого жалоба"
                await bot.send_message(ADMIN_ID, f"— {who} ({sender}):")
                last_sender = sender
            try:
                # Копия сообщения из чата отправителя (без пометки «переслано»)
                await bot.copy_message(ADMIN_ID, from_chat_id=sender, message_id=msg_id)
            except TelegramBadRequest:
                pass  # сообщение удалено или недоступно
    except (TelegramForbiddenError, TelegramBadRequest):
        logging.warning("Не удалось отправить жалобу администратору: он должен нажать /start в боте.")


@router.message(Command("report"), F.chat.type == "private")
@router.message(F.text == BTN_REPORT, F.chat.type == "private")
async def cmd_report(message: Message) -> None:
    user_id = message.from_user.id
    partner = partners.get(user_id)

    if partner is None:
        await message.answer("Пожаловаться можно только во время диалога.")
        return
    if not ADMIN_ID:
        await message.answer("Жалобы сейчас недоступны.")
        return

    report_pending[user_id] = partner
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Отправить жалобу", callback_data="rep:yes"),
            InlineKeyboardButton(text="Отмена", callback_data="rep:no"),
        ]]
    )
    await message.answer(
        f"⚠️ Отправить жалобу? Последние сообщения этого диалога (до {REPORT_HISTORY}) "
        "и ID участников будут переданы администратору, а диалог завершится.",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("rep:"))
async def on_report(call: CallbackQuery, bot: Bot) -> None:
    user_id = call.from_user.id
    partner = report_pending.pop(user_id, None)

    if call.data == "rep:no":
        await call.answer("Отменено.")
        try:
            await call.message.edit_text("Жалоба отменена.")
        except TelegramBadRequest:
            pass
        return

    # Подтверждение: проверяем, что диалог ещё идёт с тем же собеседником
    if partner is None or partners.get(user_id) != partner:
        await call.answer("Диалог уже завершён.", show_alert=True)
        try:
            await call.message.edit_text("Диалог уже завершён.")
        except TelegramBadRequest:
            pass
        return

    log = list(dialog_log.get(user_id, []))
    await send_report(bot, user_id, partner, log)
    await leave(user_id)
    await call.answer("Жалоба отправлена.")
    try:
        await call.message.edit_text("✅ Жалоба отправлена. Диалог завершён.")
    except TelegramBadRequest:
        pass
    await safe_send(bot, user_id, "Можно искать нового собеседника.", kb_idle)
    # После жалобы вернуть этого собеседника нельзя
    last_partner.pop(user_id, None)
    last_partner.pop(partner, None)
    await notify_left(bot, partner, offer=False)


# ====================== КАПЧА (нажатие кнопки с ответом) ======================
@router.callback_query(F.data.startswith("cap:"))
async def on_captcha(call: CallbackQuery, bot: Bot) -> None:
    user_id = call.from_user.id
    expected = captcha_answers.get(user_id)

    if expected is None:
        await call.answer("Проверка уже пройдена или устарела.")
        return

    chosen = int(call.data.split(":")[1])
    if chosen == expected:
        captcha_answers.pop(user_id, None)
        need_captcha.discard(user_id)
        await call.answer("Верно!")
        try:
            await call.message.edit_text("✅ Проверка пройдена.")
        except TelegramBadRequest:
            pass
        await safe_send(bot, user_id, "Теперь можно искать собеседника.", kb_idle)
    else:
        # Неверно — выдаём новый пример
        text, kb = make_captcha(user_id)
        await call.answer("Неверно, попробуйте ещё раз.")
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass


# ====================== ПЛАТНЫЙ ВОЗВРАТ СОБЕСЕДНИКА (Telegram Stars) ======================
# Как это работает:
# 1. После диалога человек нажимает «Вернуть собеседника» -> «Вернуть за 50 ⭐».
# 2. Бывшему собеседнику уходит запрос. Вернуть человека БЕЗ его согласия нельзя.
# 3. Если он согласился, покупателю приходит счёт на 50 ⭐. Пока нет согласия, платить не нужно.
# 4. После оплаты оба снова соединяются. Если соединить не удалось, звёзды возвращаются автоматически.
def spawn(coro) -> None:
    """Запускает фоновую задачу и хранит на неё ссылку."""
    task = asyncio.create_task(coro)
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)


async def expire_request(bot: Bot, buyer: int, partner: int) -> None:
    """Если собеседник не ответил на запрос вовремя, отменяем его."""
    await asyncio.sleep(RETURN_REQUEST_TTL)
    if return_requests.get(partner) == buyer:
        return_requests.pop(partner, None)
        await safe_send(bot, buyer, "⌛ Собеседник не ответил на запрос. Платить не нужно.", kb_idle)
        await safe_send(bot, partner, "⌛ Запрос на возврат собеседника устарел.")


async def expire_payment(bot: Bot, buyer: int, partner: int) -> None:
    """Если покупатель не оплатил вовремя, отменяем возврат."""
    await asyncio.sleep(PAYMENT_TTL)
    if pay_wait.get(buyer) == partner:
        pay_wait.pop(buyer, None)
        await safe_send(bot, buyer, "⌛ Время на оплату вышло, звёзды не списаны. Можно запросить возврат заново.")
        await safe_send(bot, partner, "⌛ Собеседник не оплатил возврат. Диалог не продолжится.")


async def connect_pair(bot: Bot, a: int, b: int) -> bool:
    """Соединяет двух людей в диалог. False, если кто-то уже занят, заблокирован или недоступен."""
    async with lock:
        if a in partners or b in partners or a in banned or b in banned:
            return False
        for u in (a, b):
            if u in waiting:
                waiting.remove(u)
        partners[a] = b
        partners[b] = a
        log: deque = deque(maxlen=REPORT_HISTORY)
        dialog_log[a] = log
        dialog_log[b] = log
    last_partner.pop(a, None)
    last_partner.pop(b, None)

    text = "💫 Собеседник вернулся в диалог! Все сообщения анонимны."
    ok_a = await safe_send(bot, a, text, kb_chat)
    ok_b = await safe_send(bot, b, text, kb_chat)
    if not ok_a or not ok_b:
        await leave(a)
        return False
    return True


async def refund_stars(bot: Bot, user_id: int, charge_id: str) -> bool:
    """Возвращает звёзды за платёж. True, если получилось."""
    try:
        await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
        return True
    except Exception:  # noqa: BLE001 — любая ошибка API: логируем и сообщаем
        logging.exception("Не удалось вернуть звёзды (charge_id=%s)", charge_id)
        return False


@router.callback_query(F.data == "ret:info")
async def on_return_info(call: CallbackQuery) -> None:
    if call.from_user.id not in last_partner:
        await call.answer("Этого собеседника уже нельзя вернуть.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=f"💫 Вернуть за {RETURN_PRICE_STARS} ⭐", callback_data="ret:ask")
        ]]
    )
    await call.answer()
    try:
        await call.message.edit_text(
            "💫 Вернуть собеседника\n\n"
            "Мы отправим вашему прошлому собеседнику запрос. Если он согласится, "
            f"вы оплатите {RETURN_PRICE_STARS} ⭐ и диалог продолжится.\n"
            f"Если он откажется или не ответит за {RETURN_REQUEST_TTL} секунд, платить не придётся.\n\n"
            "Вопросы по оплате: /paysupport",
            reply_markup=kb,
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "ret:ask")
async def on_return_ask(call: CallbackQuery, bot: Bot) -> None:
    buyer = call.from_user.id
    partner = last_partner.get(buyer)

    if partner is None:
        await call.answer("Этого собеседника уже нельзя вернуть.", show_alert=True)
        return
    if buyer in banned or partner in banned:
        await call.answer("Вернуть этого собеседника нельзя.", show_alert=True)
        return
    if buyer in need_captcha:
        await call.answer("Сначала пройдите проверку.", show_alert=True)
        await send_captcha(bot, buyer)
        return
    if buyer in partners:
        await call.answer("Вы уже в диалоге.", show_alert=True)
        return
    if partner in partners:
        await call.answer("Собеседник уже общается с кем-то другим.", show_alert=True)
        return
    if partner in return_requests:
        await call.answer("Запрос уже отправлен, подождите ответа.", show_alert=True)
        return

    return_requests[partner] = buyer
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data="ret:yes"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data="ret:no"),
        ]]
    )
    sent = await safe_send(
        bot, partner,
        "💫 Ваш прошлый собеседник хочет продолжить диалог. Принять?",
        kb,
    )
    if not sent:
        return_requests.pop(partner, None)
        await call.answer("Собеседник сейчас недоступен.", show_alert=True)
        return

    await call.answer()
    try:
        await call.message.edit_text(
            f"⏳ Запрос отправлен. Ждём ответ до {RETURN_REQUEST_TTL} секунд. Платить пока не нужно."
        )
    except TelegramBadRequest:
        pass
    spawn(expire_request(bot, buyer, partner))


@router.callback_query(F.data == "ret:no")
async def on_return_decline(call: CallbackQuery, bot: Bot) -> None:
    partner = call.from_user.id
    buyer = return_requests.pop(partner, None)
    await call.answer()
    try:
        await call.message.edit_text("Вы отказались продолжать диалог.")
    except TelegramBadRequest:
        pass
    if buyer is not None:
        await safe_send(bot, buyer, "Собеседник отказался продолжать диалог. Платить не нужно.", kb_idle)


@router.callback_query(F.data == "ret:yes")
async def on_return_accept(call: CallbackQuery, bot: Bot) -> None:
    partner = call.from_user.id
    buyer = return_requests.pop(partner, None)

    if buyer is None:
        await call.answer("Запрос уже устарел.", show_alert=True)
        try:
            await call.message.edit_text("Запрос уже устарел.")
        except TelegramBadRequest:
            pass
        return
    if partner in need_captcha:
        return_requests[partner] = buyer  # запрос остаётся, пока человек проходит проверку
        await call.answer("Сначала пройдите проверку.", show_alert=True)
        await send_captcha(bot, partner)
        return
    if partner in partners or buyer in partners:
        await call.answer("Диалог уже недоступен.", show_alert=True)
        await safe_send(bot, buyer, "Не получилось вернуть собеседника. Платить не нужно.", kb_idle)
        return

    pay_wait[buyer] = partner
    await call.answer()
    try:
        await call.message.edit_text("✅ Вы согласились. Ждём, пока собеседник оплатит возврат.")
    except TelegramBadRequest:
        pass

    try:
        await bot.send_invoice(
            chat_id=buyer,
            title="Вернуть собеседника",
            description="Собеседник согласился продолжить диалог. Оплатите, чтобы вернуться в чат.",
            payload="return_partner",
            currency="XTR",  # Telegram Stars; provider_token для звёзд не нужен
            prices=[LabeledPrice(label="Вернуть собеседника", amount=RETURN_PRICE_STARS)],
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pay_wait.pop(buyer, None)
        await safe_send(bot, partner, "Не получилось отправить счёт собеседнику. Диалог не продолжится.")
        return
    spawn(expire_payment(bot, buyer, partner))


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    """Telegram спрашивает бота, можно ли принять оплату (нужно ответить за 10 секунд)."""
    buyer = query.from_user.id
    partner = pay_wait.get(buyer)
    if partner is None:
        await query.answer(ok=False, error_message="Запрос устарел. Оплата не списана.")
        return
    if buyer in partners or partner in partners:
        await query.answer(ok=False, error_message="Диалог уже недоступен. Оплата не списана.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot) -> None:
    buyer = message.from_user.id
    payment = message.successful_payment
    partner = pay_wait.pop(buyer, None)
    logging.info(
        "Оплата Stars: сумма=%s charge_id=%s", payment.total_amount, payment.telegram_payment_charge_id
    )

    if partner is not None and await connect_pair(bot, buyer, partner):
        return

    # Соединить не получилось — возвращаем звёзды автоматически
    if await refund_stars(bot, buyer, payment.telegram_payment_charge_id):
        await safe_send(
            bot, buyer,
            f"Не удалось вернуть собеседника, {payment.total_amount} ⭐ возвращены вам.",
            kb_idle,
        )
    else:
        await safe_send(
            bot, buyer,
            "Не удалось вернуть собеседника и автоматически вернуть звёзды. "
            "Напишите: /paysupport ваше сообщение",
            kb_idle,
        )
        if ADMIN_ID:
            await safe_send(
                bot, ADMIN_ID,
                f"⚠️ Не удалось вернуть звёзды.\nПокупатель: {buyer}\n"
                f"charge_id: {payment.telegram_payment_charge_id}\nСумма: {payment.total_amount} ⭐",
            )


@router.message(Command("paysupport"), F.chat.type == "private")
async def cmd_paysupport(message: Message, command: CommandObject, bot: Bot) -> None:
    """Поддержка по платежам (Telegram требует эту команду у ботов с оплатой)."""
    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "💳 Вопросы по оплате ⭐\n\n"
            "Если звёзды списались, а собеседник не вернулся, они возвращаются автоматически. "
            "Если этого не произошло, напишите так:\n/paysupport ваше сообщение"
        )
        return
    if not ADMIN_ID:
        await message.answer("Поддержка сейчас недоступна.")
        return
    try:
        await bot.send_message(ADMIN_ID, f"💳 Обращение по оплате от {message.from_user.id}:\n{text[:1000]}")
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer("Не удалось отправить сообщение, попробуйте позже.")
        return
    await message.answer("Сообщение отправлено администратору.")


@router.message(Command("return"), F.chat.type == "private")
async def cmd_return(message: Message, bot: Bot) -> None:
    """Команда /return: предложить вернуть последнего собеседника (платно)."""
    user_id = message.from_user.id
    if RETURN_PRICE_STARS <= 0:
        await message.answer("Эта функция сейчас отключена.")
        return
    if user_id in partners:
        await message.answer("Вы сейчас в диалоге. Завершите его (/stop), чтобы вернуть прошлого собеседника.")
        return
    if user_id not in last_partner:
        await message.answer("Пока некого возвращать: вернуть можно только собеседника из последнего диалога.")
        return
    await offer_return(bot, user_id)


# ====================== БЛОКИРОВКИ (только для администратора) ======================
def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


@router.message(Command("ban"), F.chat.type == "private")
async def cmd_ban(message: Message, command: CommandObject, bot: Bot) -> None:
    """/ban <id>: заблокировать пользователя. Остальным команда не отвечает."""
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: /ban 123456789 (ID из жалобы)")
        return
    target = int(arg)
    if target == ADMIN_ID:
        await message.answer("Себя заблокировать нельзя.")
        return

    banned.add(target)
    partner, _ = await leave(target)
    last_partner.pop(target, None)
    if partner is not None:
        last_partner.pop(partner, None)
    # Убираем незавершённые запросы на возврат с участием заблокированного
    for table in (return_requests, pay_wait):
        for key in [k for k, v in table.items() if k == target or v == target]:
            table.pop(key, None)

    await safe_send(bot, target, BAN_TEXT, kb_idle)
    if partner is not None:
        await notify_left(bot, partner, offer=False)
    await message.answer(
        f"🚫 Пользователь {target} заблокирован до перезапуска бота. "
        "Чтобы блокировка была постоянной, добавьте его ID в переменную BANNED_IDS на хостинге."
    )


@router.message(Command("unban"), F.chat.type == "private")
async def cmd_unban(message: Message, command: CommandObject) -> None:
    """/unban <id>: снять блокировку."""
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: /unban 123456789")
        return
    banned.discard(int(arg))
    await message.answer(
        f"✅ Пользователь {arg} разблокирован. Если его ID записан в BANNED_IDS на хостинге, "
        "удалите его оттуда, иначе после перезапуска бот заблокирует его снова."
    )


# ====================== ПЕРЕСЫЛКА СООБЩЕНИЙ ======================
@router.message(F.chat.type == "private")
async def relay(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    partner = partners.get(user_id)

    if partner is None:
        if user_id in need_captcha:
            await send_captcha(bot, user_id)
            return
        # Неизвестная команда или сообщение вне диалога
        await message.answer("Вы не в диалоге. Нажмите «Искать собеседника».", reply_markup=kb_idle)
        return

    if message.text and message.text.startswith("/"):
        await message.answer("Неизвестная команда. Доступно: /next, /stop, /report, /rules.")
        return

    try:
        # copy_to отправляет копию сообщения БЕЗ пометки «переслано от…»,
        # поэтому имя, username и ID отправителя полностью скрыты.
        # Работает для текста, стикеров, фото, голосовых, видео, кружков, файлов и т.д.
        await message.copy_to(partner)
        # Для жалоб запоминаем только id сообщения (не текст)
        log = dialog_log.get(user_id)
        if log is not None:
            log.append((user_id, message.message_id))
        # Запоминаем, что пользователь отправлял фото/видео/кружок в этом диалоге
        if message.photo or message.video or message.video_note:
            sent_video.add(user_id)
    except TelegramForbiddenError:
        # Собеседник заблокировал бота — завершаем диалог
        await leave(user_id)
        await message.answer("Собеседник недоступен. Диалог завершён.", reply_markup=kb_idle)
    except TelegramBadRequest:
        await message.answer("⚠️ Этот тип сообщения нельзя передать собеседнику.")


# ====================== ЗАПУСК ======================
async def start_web() -> None:
    """Мини HTTP-сервер для health check (нужен хостингам типа Koyeb Web Service)."""
    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()


async def setup_commands(bot: Bot) -> None:
    """Меню команд: кнопка «Меню» слева от поля ввода сообщения."""
    commands = [
        BotCommand(command="next", description="🎭 Поиск собеседника"),
        BotCommand(command="stop", description="❌ Завершить диалог"),
        BotCommand(command="report", description="🚨 Пожаловаться на собеседника"),
    ]
    if RETURN_PRICE_STARS > 0:
        commands.append(
            BotCommand(command="return", description=f"💫 Вернуть собеседника ({RETURN_PRICE_STARS} ⭐)")
        )
    commands += [
        BotCommand(command="start", description="💜 Перезапуск бота"),
        BotCommand(command="rules", description="📌 Правила общения"),
    ]
    if RETURN_PRICE_STARS > 0:
        commands.append(BotCommand(command="paysupport", description="💳 Вопросы по оплате"))
    try:
        await bot.set_my_commands(commands)
    except TelegramAPIError:
        logging.warning("Не удалось установить меню команд.", exc_info=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
    if not ADMIN_ID:
        logging.warning("ADMIN_ID не задан: жалобы (/report) будут недоступны.")
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await start_web()
    await setup_commands(bot)
    # Обязательно перед стартом поллинга: убираем вебхук и старые апдейты
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
