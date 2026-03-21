import asyncio
import logging
import aiohttp
import aiosqlite
import csv
from datetime import datetime, timedelta
from cachetools import TTLCache
from tenacity import retry, stop_after_attempt, wait_exponential

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, \
    KeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings

# =========================
# LOGGING
# =========================
# FIX: логирование в файл bot.log с utf-8
logging.basicConfig(
    filename="bot.log",
    encoding="utf-8",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# =========================
# INIT
# =========================
bot = Bot(token=settings.tg_bot_token)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

gpt_session: aiohttp.ClientSession | None = None
db: aiosqlite.Connection | None = None

# Кэш для GPT (5 минут)
gpt_cache: TTLCache = TTLCache(maxsize=100, ttl=300)

# Контекст диалога на пользователя (макс 5 сообщений)
conversation_history: TTLCache = TTLCache(maxsize=1000, ttl=3600)


# =========================
# DATABASE
# =========================
async def init_db():
    global db
    db = await aiosqlite.connect("bot.db")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            service TEXT,
            master TEXT,
            date_time TEXT,
            name TEXT,
            phone TEXT,
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            name TEXT,
            phone TEXT,
            visits_count INTEGER DEFAULT 0,
            last_visit TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.commit()
    logger.info("База данных инициализирована")


async def save_lead(user_id: int, username: str, service: str, master: str,
                    date_time: str, name: str, phone: str):
    await db.execute(
        """INSERT INTO leads (user_id, username, service, master, date_time, name, phone) 
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, username, service, master, date_time, name, phone)
    )
    await db.execute(
        """INSERT OR REPLACE INTO clients (user_id, username, name, phone, visits_count, last_visit)
           VALUES (?, ?, ?, ?, COALESCE((SELECT visits_count FROM clients WHERE user_id = ?), 0) + 1, ?)""",
        (user_id, username, name, phone, user_id, datetime.now().isoformat())
    )
    await db.commit()
    logger.info(f"Лид сохранён: {name}, {phone}, {service}")


async def log_action(user_id: int, action: str):
    await db.execute(
        "INSERT INTO stats (user_id, action) VALUES (?, ?)",
        (user_id, action)
    )
    await db.commit()
    # NEW: отдельный лог действий владельца
    if user_id == settings.owner_contact:
        logger.info(f"Owner action: {action}")


async def get_user_leads(user_id: int, limit: int = 5):
    cursor = await db.execute(
        "SELECT service, master, date_time, status, created_at FROM leads WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    return await cursor.fetchall()


async def get_stats():
    cursor = await db.execute("SELECT COUNT(*) FROM leads")
    leads_count = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COUNT(DISTINCT user_id) FROM leads")
    clients_count = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COUNT(*) FROM leads WHERE status = 'new'")
    new_leads = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COUNT(DISTINCT user_id) FROM stats")
    active_users = (await cursor.fetchone())[0]

    return leads_count, clients_count, new_leads, active_users


# =========================
# FSM
# =========================
class RoleFSM(StatesGroup):
    role = State()


class BookingFSM(StatesGroup):
    service = State()
    master = State()
    date = State()
    name = State()
    phone = State()
    confirm = State()


class ClientQuestionFSM(StatesGroup):
    question = State()


# NEW: FSM для вопросов владельца
class OwnerQuestionFSM(StatesGroup):
    question = State()


# =========================
# KEYBOARDS
# =========================
def role_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💅 Я клиент", callback_data="role_client")],
        [InlineKeyboardButton(text="👔 Я владелец салона", callback_data="role_owner")]
    ], resize_keyboard=True)


def client_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Записаться", callback_data="book")],
        [InlineKeyboardButton(text="💅 Услуги", callback_data="services")],
        [InlineKeyboardButton(text="👤 Мои записи", callback_data="my_bookings")],
        [InlineKeyboardButton(text="❓ Вопрос", callback_data="client_question")],
        [InlineKeyboardButton(text="🔄 В начало", callback_data="start")]
    ], resize_keyboard=True)


def owner_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="owner_stats")],
        [InlineKeyboardButton(text="📋 Заявки", callback_data="owner_leads")],
        [InlineKeyboardButton(text="❓ Вопрос", callback_data="owner_question")],
        [InlineKeyboardButton(text="💰 Цены", callback_data="owner_price")],
        [InlineKeyboardButton(text="🚀 Заказать бота", callback_data="owner_request")],
        [InlineKeyboardButton(text="🔄 В начало", callback_data="start")]
    ], resize_keyboard=True)


def services_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💅 Маникюр", callback_data="service_manicure")],
        [InlineKeyboardButton(text="🦶 Педикюр", callback_data="service_pedicure")],
        [InlineKeyboardButton(text="👁 Брови", callback_data="service_brows")],
        [InlineKeyboardButton(text="👁 Ресницы", callback_data="service_lashes")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="client_menu")]
    ], resize_keyboard=True)


def masters_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👩 Анна (Топ-мастер)", callback_data="master_anna")],
        [InlineKeyboardButton(text="👩 Мария (Мастер)", callback_data="master_maria")],
        [InlineKeyboardButton(text="👩 Елена (Мастер)", callback_data="master_elena")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="book")]
    ], resize_keyboard=True)


def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_booking")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="client_menu")]
    ], resize_keyboard=True)


def back_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True, one_time_keyboard=True)


def name_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👤 Использовать из профиля")]
    ], resize_keyboard=True, one_time_keyboard=True)


def phone_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📱 Отправить контакт", request_contact=True)]
    ], resize_keyboard=True, one_time_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❌ Отмена")]
    ], resize_keyboard=True, one_time_keyboard=True)


# =========================
# GPT SERVICE С КОНТЕКСТОМ
# =========================
def get_user_history(user_id: int) -> list:
    """Получить историю диалога пользователя"""
    return conversation_history.get(user_id, [])


def add_to_history(user_id: int, role: str, content: str):
    """Добавить сообщение в историю (макс 5 пар сообщений)"""
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    conversation_history[user_id].append({"role": role, "content": content})

    # Оставляем только последние 10 сообщений (5 диалогов)
    if len(conversation_history[user_id]) > 10:
        conversation_history[user_id] = conversation_history[user_id][-10:]


def clear_user_history(user_id: int):
    """Очистить историю пользователя"""
    if user_id in conversation_history:
        del conversation_history[user_id]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def ask_gpt(prompt: str, mode: str, user_id: int) -> str:
    global gpt_session

    cache_key = f"{mode}:{user_id}:{prompt}"
    if cache_key in gpt_cache:
        logger.info(f"Кэш hit для пользователя {user_id}")
        return gpt_cache[cache_key]

    if gpt_session is None or gpt_session.closed:
        gpt_session = aiohttp.ClientSession()

    if mode == "client":
        system_prompt = (
            "Ты администратор салона красоты Beauty. "
            "Отвечай кратко (2-3 предложения), дружелюбно, с эмодзи. "
            "Всегда предлагай записаться. "
            "Не упоминай AI, ботов, автоматизацию. "
            "Помни контекст предыдущих вопросов клиента. "
            "Не используй Markdown, звёздочки (**), жирный текст. Пиши обычным текстом с эмодзи."
        )
    elif mode == "owner_question":
        system_prompt = (
            "Ты эксперт по автоматизации салонов красоты. Продаёшь AI-решение владельцам. "
            "Говори о выгодах: заявки 24/7, разгрузка админа, клиенты не теряются, автоматическая запись. "
            "Коротко, по делу, с цифрами и фактами. Не упоминай AI, ботов, автоматизацию — говори о результатах. "
            "Не используй Markdown, звёздочки (**), жирный текст. Пиши обычным текстом с эмодзи."
        )
    else:
        system_prompt = (
            "Ты продаёшь решение для владельцев салонов. "
            "Говори о выгодах: заявки 24/7, разгрузка админа, клиенты не теряются. "
            "Коротко и по делу. "
            "Не используй Markdown, звёздочки (**), жирный текст. Пиши обычным текстом с эмодзи."
        )

    # Формируем сообщения с историей диалога
    messages = [{"role": "system", "content": system_prompt}]

    if mode == "client" and user_id in conversation_history:
        messages.extend(conversation_history[user_id])

    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": settings.gpt_model,
        "temperature": settings.gpt_temperature,
        "max_tokens": settings.gpt_max_tokens,
        "messages": messages
    }

    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json"
    }

    async with gpt_session.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=settings.gpt_timeout)
    ) as resp:
        if resp.status != 200:
            error_text = await resp.text()
            raise Exception(f"OpenAI API error: {resp.status}")

        data = await resp.json()
        answer = data["choices"][0]["message"]["content"].strip()

        # Сохраняем в историю
        add_to_history(user_id, "user", prompt)
        add_to_history(user_id, "assistant", answer)

        gpt_cache[cache_key] = answer
        return answer


# =========================
# ADMIN COMMANDS
# =========================
@router.message(Command("stats"))
async def stats_cmd(message: Message):
    if message.from_user.id != settings.owner_contact:
        await message.answer("Доступ только для владельца")
        return

    leads, clients, new_leads, active = await get_stats()

    await message.answer(
        f"Статистика салона\n\n"
        f"Всего заявок: {leads}\n"
        f"Клиентов: {clients}\n"
        f"Новых заявок: {new_leads}\n"
        f"Активных пользователей: {active}\n\n"
        f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )


@router.message(Command("leads"))
async def leads_cmd(message: Message):
    if message.from_user.id != settings.owner_contact:
        return

    cursor = await db.execute(
        "SELECT id, name, phone, service, master, date_time, status FROM leads ORDER BY id DESC LIMIT 10"
    )
    leads = await cursor.fetchall()

    if not leads:
        await message.answer("Заявок пока нет")
        return

    text = "Последние заявки:\n\n"
    for lead in leads:
        status_emoji = "" if lead[6] == "new" else "✅" if lead[6] == "confirmed" else "❌"
        text += f"{status_emoji} #{lead[0]} | {lead[1]}\n"
        text += f"   Телефон: {lead[2]} | Услуга: {lead[3]}\n"
        text += f"   Мастер: {lead[4]} | Дата: {lead[5]}\n\n"

    await message.answer(text)


@router.message(Command("export"))
async def export_cmd(message: Message):
    if message.from_user.id != settings.owner_contact:
        return

    cursor = await db.execute(
        "SELECT id, name, phone, service, master, date_time, status, created_at FROM leads"
    )
    leads = await cursor.fetchall()

    if not leads:
        await message.answer("Нет данных для экспорта")
        return

    filename = f"leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "Имя", "Телефон", "Услуга", "Мастер", "Дата", "Статус", "Создано"])
        writer.writerows(leads)

    with open(filename, "rb") as f:
        await message.answer_document(f, caption=f"Экспорт заявок ({len(leads)} записей)")

    logger.info(f"Экспорт: {filename}")


@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Справка по боту\n\n"
        "Для клиентов:\n"
        "/start — Главное меню\n"
        "Записаться — Форма записи\n"
        "Услуги — Список услуг\n"
        "Мои записи — История записей\n"
        "Вопрос — Задать вопрос AI\n\n"
        "Для владельца:\n"
        "/stats — Статистика\n"
        "/leads — Последние заявки\n"
        "/export — Выгрузка в CSV\n\n"
        "Разработка: @mut08031982"
    )


@router.message(Command("clear"))
async def clear_cmd(message: Message):
    """Очистить историю диалога с GPT"""
    clear_user_history(message.from_user.id)
    await message.answer("История диалога очищена ")


# =========================
# START
# =========================
@router.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(RoleFSM.role)
    await log_action(message.from_user.id, "start")

    # Очищаем историю GPT при новом старте
    clear_user_history(message.from_user.id)

    await message.answer(
        "Добро пожаловать в Beauty Salon!\n\n"
        "Я ваш персональный помощник для записи в салон.\n"
        "Работаю 24/7 без выходных\n\n"
        "Что я умею:\n"
        "- Запись на услуги онлайн\n"
        "- Ответы на вопросы\n"
        "- История ваших записей\n\n"
        "Выберите кто вы:",
        reply_markup=role_keyboard()
    )


@router.callback_query(F.data == "start")
async def start_cb(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await state.set_state(RoleFSM.role)
    clear_user_history(call.from_user.id)
    await call.message.answer(
        "Добро пожаловать в Beauty Salon!\n\n"
        "Выберите кто вы:",
        reply_markup=role_keyboard()
    )


# =========================
# ROLE
# =========================
@router.callback_query(F.data == "role_client")
async def role_client(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await log_action(call.from_user.id, "role_client")
    await call.message.answer(
        "Меню клиента\n\n"
        "Выберите действие:",
        reply_markup=client_menu()
    )


@router.callback_query(F.data == "role_owner")
async def role_owner(call: CallbackQuery, state: FSMContext):
    await call.answer()
    # FIX: ограничили доступ к меню владельца только owner_contact
    if isinstance(call, CallbackQuery) and call.from_user.id != settings.owner_contact:
        await call.message.answer("Доступ только для владельца")
        return
    await state.clear()
    await log_action(call.from_user.id, "role_owner")
    await call.message.answer(
        "Меню владельца\n\n"
        "Управление салоном:",
        reply_markup=owner_menu()
    )


# =========================
# CLIENT — SERVICES
# =========================
@router.callback_query(F.data == "services")
async def services_cb(call: CallbackQuery):
    await call.answer()
    await log_action(call.from_user.id, "services_viewed")
    await call.message.answer(
        "Наши услуги:\n\n"
        "Маникюр — от 1500 руб\n"
        "Педикюр — от 2000 руб\n"
        "Брови — от 800 руб\n"
        "Ресницы — от 2500 руб\n\n"
        "Выберите услугу или запишитесь:",
        reply_markup=services_keyboard()
    )


@router.callback_query(F.data.startswith("service_"))
async def service_selected(call: CallbackQuery, state: FSMContext):
    await call.answer()
    service_map = {
        "service_manicure": "Маникюр",
        "service_pedicure": "Педикюр",
        "service_brows": "Брови",
        "service_lashes": "Ресницы"
    }
    # FIX: исправлен выбор услуги по полному callback_data
    service = service_map.get(call.data, "Услуга")
    await state.update_data(service=service)
    await state.set_state(BookingFSM.master)
    await call.message.answer(
        f"Выбрано: {service}\n\n"
        "Выберите мастера:",
        reply_markup=masters_keyboard()
    )


# =========================
# CLIENT — BOOKING
# =========================
@router.callback_query(F.data == "book")
async def book_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(BookingFSM.service)
    await log_action(call.from_user.id, "booking_started")
    await call.message.answer(
        "Запись на услугу\n\n"
        "Выберите услугу:",
        reply_markup=services_keyboard()
    )


@router.callback_query(F.data.startswith("master_"))
async def master_selected(call: CallbackQuery, state: FSMContext):
    await call.answer()
    master_map = {
        "master_anna": "Анна (Топ-мастер)",
        "master_maria": "Мария (Мастер)",
        "master_elena": "Елена (Мастер)"
    }
    # FIX: исправлен выбор мастера по полному callback_data
    master = master_map.get(call.data, "Мастер")
    await state.update_data(master=master)
    await state.set_state(BookingFSM.date)
    await call.message.answer(
        f"Мастер: {master}\n\n"
        "На какую дату и время удобно?\n"
        "Пример: Завтра 15:00 или 12.03 14:30",
        reply_markup=back_keyboard()
    )


@router.callback_query(F.data == "back_step")
@router.message(F.text == "⬅️ Назад")
async def back_step(call: CallbackQuery | Message, state: FSMContext):
    await call.answer() if isinstance(call, CallbackQuery) else None

    current_state = await state.get_state()

    if current_state == BookingFSM.date:
        await state.set_state(BookingFSM.master)
        msg = call.message if isinstance(call, CallbackQuery) else call
        await msg.answer("Выберите мастера:", reply_markup=masters_keyboard())
    elif current_state == BookingFSM.name:
        await state.set_state(BookingFSM.date)
        msg = call.message if isinstance(call, CallbackQuery) else call
        await msg.answer(
            "На какую дату и время удобно?",
            reply_markup=back_keyboard()
        )
    elif current_state == BookingFSM.phone:
        await state.set_state(BookingFSM.name)
        msg = call.message if isinstance(call, CallbackQuery) else call
        await msg.answer(
            "Как вас зовут?",
            reply_markup=name_keyboard()
        )
    elif current_state == BookingFSM.confirm:
        await state.set_state(BookingFSM.phone)
        msg = call.message if isinstance(call, CallbackQuery) else call
        await msg.answer(
            "Оставьте номер телефона",
            reply_markup=phone_keyboard()
        )
    else:
        msg = call.message if isinstance(call, CallbackQuery) else call
        await msg.answer("Выберите действие:", reply_markup=client_menu())


@router.message(BookingFSM.date)
async def book_date(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        return
    await state.update_data(date=message.text)
    await state.set_state(BookingFSM.name)
    await message.answer(
        "Как вас зовут?",
        reply_markup=name_keyboard()
    )


@router.callback_query(F.data == "use_profile_name")
@router.message(F.text == "👤 Использовать из профиля")
async def use_profile_name(call: CallbackQuery | Message, state: FSMContext):
    if isinstance(call, CallbackQuery):
        await call.answer()
        name = call.from_user.full_name or "Клиент"
        msg = call.message
    else:
        name = call.from_user.full_name or "Клиент"
        msg = call

    await state.update_data(name=name)
    await state.set_state(BookingFSM.phone)
    await msg.answer(
        f"Имя: {name}\n\n"
        "Оставьте номер телефона\n"
        "Пример: +7 999 000-00-00",
        reply_markup=phone_keyboard()
    )


@router.message(BookingFSM.name)
async def book_name(message: Message, state: FSMContext):
    if message.text in ["👤 Использовать из профиля", "⬅️ Назад"]:
        return

    if message.contact:
        return

    logger.info(f"Получено имя: {message.text}")

    await state.update_data(name=message.text)
    await state.set_state(BookingFSM.phone)
    await message.answer(
        "Оставьте номер телефона\n"
        "Пример: +7 999 000-00-00",
        reply_markup=phone_keyboard()
    )


@router.message(BookingFSM.phone, F.contact)
async def book_phone_contact(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = message.contact.phone_number

    await state.update_data(phone=phone)
    await state.set_state(BookingFSM.confirm)

    logger.info(f"Получен контакт: {phone}")

    await message.answer(
        f"Проверьте данные:\n\n"
        f"Услуга: {data.get('service', 'Не указано')}\n"
        f"Мастер: {data.get('master', 'Не указано')}\n"
        f"Дата: {data.get('date', 'Не указано')}\n"
        f"Имя: {data.get('name', 'Не указано')}\n"
        f"Телефон: {phone}\n\n"
        "Подтвердить запись?",
        reply_markup=confirm_keyboard()
    )


@router.message(BookingFSM.phone)
async def book_phone_text(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Запись отменена", reply_markup=client_menu())
        return

    data = await state.get_data()
    phone = message.text

    await state.update_data(phone=phone)
    await state.set_state(BookingFSM.confirm)

    logger.info(f"Получен телефон: {phone}")

    await message.answer(
        f"Проверьте данные:\n\n"
        f"Услуга: {data.get('service', 'Не указано')}\n"
        f"Мастер: {data.get('master', 'Не указано')}\n"
        f"Дата: {data.get('date', 'Не указано')}\n"
        f"Имя: {data.get('name', 'Не указано')}\n"
        f"Телефон: {phone}\n\n"
        "Подтвердить запись?",
        reply_markup=confirm_keyboard()
    )


@router.callback_query(F.data == "confirm_booking")
async def confirm_booking(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    await save_lead(
        user_id=call.from_user.id,
        username=call.from_user.username or "unknown",
        service=data.get('service', 'Не указано'),
        master=data.get('master', 'Не указано'),
        date_time=data.get('date', 'Не указано'),
        name=data.get('name', 'Не указано'),
        phone=data.get('phone', 'Не указано')
    )

    await bot.send_message(
        settings.owner_contact,
        f"Новая запись!\n\n"
        f"Услуга: {data.get('service')}\n"
        f"Мастер: {data.get('master')}\n"
        f"Дата: {data.get('date')}\n"
        f"Имя: {data.get('name')}\n"
        f"Телефон: {data.get('phone')}\n"
        f"Username: @{call.from_user.username or 'нет'}"
    )

    await call.message.answer(
        "Запись подтверждена!\n\n"
        "Мы свяжемся с вами для подтверждения.\n"
        "Ждём вас в салоне!",
        reply_markup=client_menu()
    )

    await state.clear()
    await log_action(call.from_user.id, "booking_completed")


# =========================
# MY BOOKINGS
# =========================
@router.callback_query(F.data == "my_bookings")
async def my_bookings(call: CallbackQuery):
    await call.answer()
    await log_action(call.from_user.id, "my_bookings_viewed")

    leads = await get_user_leads(call.from_user.id)

    if not leads:
        await call.message.answer(
            "У вас пока нет записей\n\n"
            "Запишитесь на услугу!",
            reply_markup=client_menu()
        )
        return

    text = "Ваши записи:\n\n"
    for i, lead in enumerate(leads, 1):
        status_emoji = "🆕" if lead[3] == "new" else "✅" if lead[3] == "confirmed" else "❌"
        text += f"{i}. {status_emoji} {lead[0]} | {lead[2]}\n"
        text += f"   Услуга: {lead[0]} | Мастер: {lead[1]}\n\n"

    await call.message.answer(text, reply_markup=client_menu())


# =========================
# CLIENT — QUESTIONS (С КОНТЕКСТОМ!)
# =========================
@router.callback_query(F.data == "client_question")
async def client_question(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(ClientQuestionFSM.question)
    await log_action(call.from_user.id, "question_mode_entered")

    # Очищаем историю при входе в режим вопроса
    clear_user_history(call.from_user.id)

    await call.message.answer(
        "Задайте ваш вопрос\n\n"
        "Я отвечу в течение нескольких секунд\n"
        "Можно задавать уточняющие вопросы 👇",
        reply_markup=cancel_keyboard()
    )


@router.message(ClientQuestionFSM.question)
async def client_gpt(message: Message, state: FSMContext):
    """Обработчик вопросов к GPT с контекстом диалога"""

    # Обработка кнопки отмены
    if message.text == "❌ Отмена":
        await state.clear()
        clear_user_history(message.from_user.id)
        await message.answer("Возвращаюсь в меню", reply_markup=client_menu())
        return

    # Игнорируем кнопки меню
    if message.text in ["Записаться", "Услуги", "Мои записи", "Вопрос", "В начало"]:
        return

    thinking = await message.answer("Думаю...")
    try:
        answer = await ask_gpt(message.text, "client", message.from_user.id)
        await thinking.delete()
        await message.answer(
            f"Ответ:\n\n{answer}",
            reply_markup=client_menu()
        )
        await log_action(message.from_user.id, "question_asked")
    except Exception as e:
        await thinking.delete()
        await message.answer(
            "Ошибка связи\n\n"
            "Попробуйте ещё раз через минуту",
            reply_markup=client_menu()
        )
        logger.error(f"GPT error: {e}")
    # Состояние НЕ сбрасываем — можно задавать ещё вопросы


@router.callback_query(F.data == "client_menu")
@router.message(F.text == "❌ Отмена")
async def exit_question_mode(call: CallbackQuery | Message, state: FSMContext):
    """Выход из режима вопроса"""
    await state.clear()
    clear_user_history(call.from_user.id if isinstance(call, CallbackQuery) else call.from_user.id)

    msg = call.message if isinstance(call, CallbackQuery) else call
    await msg.answer("Меню клиента", reply_markup=client_menu())


# =========================
# NAVIGATION
# =========================
@router.callback_query(F.data == "owner_stats")
async def owner_stats_cb(call: CallbackQuery):
    await call.answer()
    # FIX: ограничили доступ к статистике только owner_contact
    if isinstance(call, CallbackQuery) and call.from_user.id != settings.owner_contact:
        await call.message.answer("Доступ только для владельца")
        return
    leads, clients, new_leads, active = await get_stats()
    await call.message.answer(
        f"Статистика салона\n\n"
        f"Всего заявок: {leads}\n"
        f"Клиентов: {clients}\n"
        f"Новых: {new_leads}\n"
        f"Активных: {active}",
        reply_markup=owner_menu()
    )


@router.callback_query(F.data == "owner_leads")
async def owner_leads_cb(call: CallbackQuery):
    await call.answer()
    # FIX: ограничили доступ к заявкам только owner_contact
    if isinstance(call, CallbackQuery) and call.from_user.id != settings.owner_contact:
        await call.message.answer("Доступ только для владельца")
        return
    cursor = await db.execute(
        "SELECT id, name, phone, service, master, date_time, status FROM leads WHERE status='new' ORDER BY id DESC LIMIT 5"
    )
    leads = await cursor.fetchall()

    if not leads:
        await call.message.answer("Новых заявок нет", reply_markup=owner_menu())
        return

    text = "Новые заявки:\n\n"
    for lead in leads:
        text += f"#{lead[0]} | {lead[1]} | {lead[2]}\n"
        text += f"   Услуга: {lead[3]} | Мастер: {lead[4]} | Дата: {lead[5]}\n\n"

    await call.message.answer(text, reply_markup=owner_menu())


@router.callback_query(F.data == "owner_price")
async def owner_price_cb(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "Стоимость разработки бота:\n\n"
        "Базовый — 30 000 руб\n"
        "  Запись, уведомления, база\n\n"
        "PRO — 50 000 руб\n"
        "  + GPT с контекстом, аналитика, экспорт\n\n"
        "Premium — 80 000 руб\n"
        "  + CRM, оплата, веб-админка\n\n"
        "Поддержка: от 3 000 руб/мес",
        reply_markup=owner_menu()
    )


@router.callback_query(F.data == "owner_request")
async def owner_request(call: CallbackQuery):
    await call.answer()
    await bot.send_message(
        settings.owner_contact,
        f"Заинтересован в боте!\n\n"
        f"Username: @{call.from_user.username or 'нет'}\n"
        f"ID: {call.from_user.id}"
    )
    await call.message.answer(
        "Заявка отправлена!\n\n"
        "Я свяжусь с вами в ближайшее время",
        reply_markup=owner_menu()
    )
    await log_action(call.from_user.id, "owner_request")


# NEW: вход в режим вопроса владельца
@router.callback_query(F.data == "owner_question")
async def owner_question_cb(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if isinstance(call, CallbackQuery) and call.from_user.id != settings.owner_contact:
        await call.message.answer("Доступ только для владельца")
        return

    await state.set_state(OwnerQuestionFSM.question)
    await call.message.answer(
        "Задайте вопрос о возможностях бота",
        reply_markup=cancel_keyboard()
    )


# NEW: обработка вопроса владельца через ask_gpt
@router.message(OwnerQuestionFSM.question)
async def owner_gpt(message: Message, state: FSMContext):
    if message.from_user.id != settings.owner_contact:
        await state.clear()
        await message.answer("Доступ только для владельца")
        return

    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Возвращаюсь в меню владельца", reply_markup=owner_menu())
        return

    thinking = await message.answer("Думаю...")
    try:
        answer = await ask_gpt(message.text, "owner_question", message.from_user.id)
        await thinking.delete()
        await message.answer(f"Ответ:\n\n{answer}", reply_markup=owner_menu())
    except Exception as e:
        await thinking.delete()
        await message.answer(
            "Ошибка связи\n\nПопробуйте ещё раз через минуту",
            reply_markup=owner_menu()
        )
        # FIX: унифицированный формат ошибки GPT
        logger.error(f"GPT error: {e}")
    finally:
        await state.clear()


# =========================
# RUN
# =========================
async def main():
    await init_db()
    try:
        logger.info("Бот запускается...")
        # NEW: лог успешного старта бота
        logger.info("Бот запущен")
        await dp.start_polling(bot)
    finally:
        if gpt_session and not gpt_session.closed:
            await gpt_session.close()
        if db:
            await db.close()
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())