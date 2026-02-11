# main.py
import asyncio
import sqlite3
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# === НАСТРОЙКИ ИЗ config.py ===
import config

BOT_TOKEN = config.BOT_TOKEN
TEACHER_ID = config.TEACHER_ID
TEACHER_TIMEZONE_OFFSET = config.TEACHER_TIMEZONE_OFFSET
DEFAULT_CHANNEL = config.CHANNEL_ID

# === БОТ И ДИСПЕТЧЕР ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === СОСТОЯНИЕ БОТА ===
bot_active = True

# === ТЕКУЩИЙ КАНАЛ ===
current_channel = DEFAULT_CHANNEL

# === БАЗА ДАННЫХ ===
conn = sqlite3.connect("school_bot.db", check_same_thread=False)
cursor = conn.cursor()

# Таблицы
cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        role TEXT,
        status TEXT,
        reason TEXT,
        approved INTEGER DEFAULT 0
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS duty_message (
        id INTEGER PRIMARY KEY,
        message_id INTEGER
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
''')

# Загрузка настроек
def load_setting(key: str, default: str):
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default

def save_setting(key: str, value: str):
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

# Инициализация канала
current_channel = load_setting("channel", DEFAULT_CHANNEL)
conn.commit()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def save_duty_message_id(message_id: int):
    cursor.execute("INSERT OR REPLACE INTO duty_message (id, message_id) VALUES (1, ?)", (message_id,))
    conn.commit()

def get_duty_message_id() -> int:
    cursor.execute("SELECT message_id FROM duty_message WHERE id=1")
    row = cursor.fetchone()
    return row[0] if row else None

# === СОСТОЯНИЯ FSM ===
class Registration(StatesGroup):
    awaiting_name = State()
    awaiting_reason = State()
    awaiting_duty_name = State()
    awaiting_delete_name = State()
    awaiting_delete_confirm = State()

# === КЛАВИАТУРЫ ===
def get_student_kb():
    """Клавиатура для учеников — без помощи"""
    if bot_active:
        return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
            [KeyboardButton(text="✅ Приду в школу")],
            [KeyboardButton(text="❌ Не приду")],
            [KeyboardButton(text="🧹 Отчитаться о дежурстве")]
        ])
    else:
        return types.ReplyKeyboardRemove()

def get_teacher_kb():
    """Клавиатура для учителя — с кнопкой помощи"""
    if bot_active:
        return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
            [KeyboardButton(text="📋 Список класса")],
            [KeyboardButton(text="➕ Добавить дежурного")],
            [KeyboardButton(text="🗑️ Удалить ученика")],
            [KeyboardButton(text="📤 Повторить отчёт в канал")],
            [KeyboardButton(text="🔴 Стоп")],
            [KeyboardButton(text="ℹ️ Помощь")]
        ])
    else:
        return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
            [KeyboardButton(text="🟢 Старт")]
        ])

def get_approval_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Согласить", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_{user_id}")
        ]
    ])

def get_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить всё", callback_data="confirm_delete_all"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_delete")
        ]
    ])

# === ПРОВЕРКА ВЫХОДНЫХ ===
def is_weekend():
    return datetime.now().weekday() >= 5

# === ОТЧЁТ В 8:25 ===
async def send_teacher_report():
    if not bot_active or is_weekend():
        return
    cursor.execute("SELECT name, status, reason FROM users WHERE role='student' AND approved=1")
    students = cursor.fetchall()
    if not students:
        await bot.send_message(TEACHER_ID, "📝 Нет зарегистрированных учеников.")
        return

    lines = []
    for name, status, reason in students:
        if status == "present":
            lines.append(f"{name} — ✅ придёт")
        elif status == "absent":
            lines.append(f"{name} — ❌ не придёт ({reason})")
        else:
            lines.append(f"{name} — ❓ неизвестно")

    report = "\n".join(lines)
    await bot.send_message(TEACHER_ID, f"📋 Отчёт по приходу (8:25):\n\n{report}")

# === ВЫБОР ДЕЖУРНОГО В 8:45 ===
async def notify_teacher_to_assign_duty():
    if not bot_active or is_weekend():
        return

    cursor.execute("SELECT name FROM users WHERE role='student' AND approved=1 AND status='present'")
    present_students = [row[0] for row in cursor.fetchall()]

    if not present_students:
        await bot.send_message(TEACHER_ID, "🧹 Сегодня никто не приходит — дежурных нет.")
        return

    buttons = [[InlineKeyboardButton(text=name, callback_data=f"duty_{name}")] for name in present_students]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await bot.send_message(
        TEACHER_ID,
        "🧹 Пришло время назначить дежурного!\n\n"
        "Выберите, кто будет дежурить сегодня:",
        reply_markup=kb
    )

# === НАЗНАЧЕНИЕ ДЕЖУРНОГО ===
@dp.callback_query(F.data.startswith("duty_"))
async def select_duty_student(callback: types.CallbackQuery):
    if callback.from_user.id != TEACHER_ID:
        await callback.answer("⛔ Только классный руководитель может назначать дежурного.")
        return

    name = callback.data.split("_", 1)[1]
    cursor.execute("SELECT user_id FROM users WHERE name=?", (name,))
    row = cursor.fetchone()

    if row:
        user_id = row[0]
        msg = f"🧹 Дежурства на сегодня:\nДежурит: {name}"
        try:
            sent = await bot.send_message(current_channel, msg)
            save_duty_message_id(sent.message_id)
        except Exception as e:
            await bot.send_message(TEACHER_ID, f"❌ Ошибка отправки в канал <code>{current_channel}</code>: {e}", parse_mode="HTML")
            await callback.answer("Ошибка", show_alert=True)
            return

        await bot.send_message(user_id, "🧹 Вы назначены дежурным на сегодня! Удачи!")
        await callback.message.edit_text(f"✅ Дежурный <b>{name}</b> назначен.", parse_mode="HTML")
    else:
        await callback.message.edit_text("❌ Ученик не найден.")

    await callback.answer("Готово")

# === ПЛАНИРОВЩИК ===
async def run_scheduler():
    while True:
        if bot_active:
            now = datetime.now()
            hour_local = (now.hour + TEACHER_TIMEZONE_OFFSET) % 24
            minute, second = now.minute, now.second

            if not is_weekend():
                if hour_local == 8 and minute == 25 and second < 10:
                    await send_teacher_report()
                    await asyncio.sleep(60)
                elif hour_local == 8 and minute == 45 and second < 10:
                    await notify_teacher_to_assign_duty()
                    await asyncio.sleep(60)
        await asyncio.sleep(10)

# === /start ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id == TEACHER_ID:
        cursor.execute("INSERT OR IGNORE INTO users (user_id, name, role, approved) VALUES (?, 'Классный руководитель', 'teacher', 1)", (user_id,))
        conn.commit()
        await message.answer("👨‍🏫 Добро пожаловать!", reply_markup=get_teacher_kb())
        return

    cursor.execute("SELECT role, approved FROM users WHERE user_id=?", (user_id,))
    result = cursor.fetchone()

    if result:
        role, approved = result
        if role == "student":
            kb = get_student_kb() if approved else None
            await message.answer(
                "🎓 Добро пожаловать!" if approved else "⏳ Заявка на рассмотрении.",
                reply_markup=kb
            )
        return

    await message.answer("👋 Введите имя (например: Иван Иванов):")
    await state.set_state(Registration.awaiting_name)

# === Регистрация имени ===
@dp.message(Registration.awaiting_name)
async def process_name(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Ожидайте.")
        await state.clear()
        return

    name = message.text.strip()
    if not re.fullmatch(r"^[А-ЯЁA-Z][а-яёa-z]*(?:[- ][А-ЯЁA-Z][а-яёa-z]+)*$", name, re.IGNORECASE):
        await message.answer("📛 Имя: буквы, пробелы, дефисы. Пример: Анна-Мария")
        return

    user_id = message.from_user.id
    cursor.execute("INSERT OR REPLACE INTO users (user_id, name, role, status) VALUES (?, ?, 'student', 'unknown')", (user_id, name))
    conn.commit()

    await bot.send_message(
        TEACHER_ID,
        f"🆕 Заявка:\nИмя: {name}\nЮзер: @{message.from_user.username or 'нет'}",
        reply_markup=get_approval_kb(user_id)
    )
    await message.answer("📨 Заявка отправлена.")
    await state.clear()

# === Одобрение / Отклонение ===
@dp.callback_query(F.data.startswith("approve_"))
async def approve_student(callback: types.CallbackQuery):
    if not bot_active:
        await callback.answer("🔴 Бот остановлен.", show_alert=True)
        return
    user_id = int(callback.data.split("_")[1])
    cursor.execute("UPDATE users SET approved=1 WHERE user_id=?", (user_id,))
    conn.commit()
    await bot.send_message(user_id, "✅ Вы приняты!", reply_markup=get_student_kb())
    await callback.message.edit_text(f"{callback.message.text}\n\n✅ Принято.")
    await callback.answer("Принято")

@dp.callback_query(F.data.startswith("decline_"))
async def decline_student(callback: types.CallbackQuery):
    if not bot_active:
        await callback.answer("🔴 Бот остановлен.", show_alert=True)
        return
    user_id = int(callback.data.split("_")[1])
    cursor.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    conn.commit()
    await bot.send_message(user_id, "❌ Отклонено.")
    await callback.message.edit_text(f"{callback.message.text}\n\n❌ Отклонено.")
    await callback.answer("Отклонено")

# === Учитель: Команды ===
@dp.message(F.text == "📋 Список класса")
async def list_students(message: types.Message):
    if message.from_user.id != TEACHER_ID:
        return
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Но вы можете посмотреть список.", reply_markup=get_teacher_kb())
        return
    cursor.execute("SELECT name, status, reason FROM users WHERE role='student' AND approved=1")
    students = cursor.fetchall()
    if not students:
        await message.answer("📚 Класс пуст.")
        return
    lines = [
        f"{n} — ✅ идёт" if s == "present" else
        f"{n} — ❌ не идёт ({r})" if s == "absent" else
        f"{n} — ⏳ неизвестно"
        for n, s, r in students
    ]
    await message.answer("👥 Список класса:\n\n" + "\n".join(lines))

@dp.message(F.text == "➕ Добавить дежурного")
async def prompt_duty_name(message: types.Message, state: FSMContext):
    if message.from_user.id != TEACHER_ID:
        return
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        return
    await message.answer("✏️ Введите имя ученика:")
    await state.set_state(Registration.awaiting_duty_name)

@dp.message(Registration.awaiting_duty_name)
async def set_duty(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        await state.clear()
        return
    name = message.text.strip()
    cursor.execute("SELECT user_id FROM users WHERE name=? AND approved=1", (name,))
    row = cursor.fetchone()
    if row:
        await bot.send_message(row[0], "🧹 Вы назначены дежурным!")
        await message.answer(f"🧹 {name} назначен дежурным.")
    else:
        await message.answer("❌ Ученик не найден.")
    await state.clear()

@dp.message(F.text == "🗑️ Удалить ученика")
async def prompt_delete_name(message: types.Message, state: FSMContext):
    if message.from_user.id != TEACHER_ID:
        return
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        return
    await message.answer("✏️ Введите имя или <code>@all</code>:", parse_mode="HTML")
    await state.set_state(Registration.awaiting_delete_name)

@dp.message(Registration.awaiting_delete_name)
async def delete_student(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        await state.clear()
        return
    name = message.text.strip()
    if name == "@all":
        await message.answer("⚠️ Точно удалить всех?", reply_markup=get_confirm_kb(), parse_mode="HTML")
        await state.set_state(Registration.awaiting_delete_confirm)
    else:
        cursor.execute("DELETE FROM users WHERE name=? AND role='student'", (name,))
        conn.commit()
        await message.answer(f"✅ Удалён: {name}" if cursor.rowcount else "❌ Не найден.")
        await state.clear()

@dp.callback_query(F.data == "confirm_delete_all")
async def confirm_delete_all(callback: types.CallbackQuery, state: FSMContext):
    cursor.execute("DELETE FROM users WHERE role='student'")
    conn.commit()
    await callback.message.edit_text(f"✅ Удалено: {cursor.rowcount} учеников.")
    await callback.answer("Готово")
    await state.clear()

@dp.callback_query(F.data == "cancel_delete")
async def cancel_delete(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Отменено")
    await callback.answer("Отмена")
    await state.clear()

@dp.message(F.text == "📤 Повторить отчёт в канал")
async def resend_channel_report(message: types.Message):
    if message.from_user.id != TEACHER_ID:
        return
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        return
    await notify_teacher_to_assign_duty()
    await message.answer("📤 Отправлено учителю для назначения.")

# === Управление: Стоп / Старт ===
@dp.message(F.text == "🔴 Стоп")
async def stop_bot(message: types.Message):
    global bot_active
    if message.from_user.id != TEACHER_ID:
        return
    bot_active = False
    await message.answer("🔴 Бот остановлен. Ученики больше не могут отмечаться.", reply_markup=get_teacher_kb())

@dp.message(F.text == "🟢 Старт")
async def start_bot(message: types.Message):
    global bot_active
    if message.from_user.id != TEACHER_ID:
        return
    bot_active = True
    await message.answer("🟢 Бот запущен. Ученики могут отмечаться.", reply_markup=get_teacher_kb())

# === Команда /set_channel ===
@dp.message(Command("set_channel"))
async def set_channel(message: types.Message):
    if message.from_user.id != TEACHER_ID:
        return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await message.answer("📌 Используйте: <code>/set_channel @название_канала</code>", parse_mode="HTML")
        return
    channel = args[1].strip()
    if not (channel.startswith("@") or channel.startswith("https://t.me/")):
        await message.answer("📛 Некорректное имя канала. Пример: <code>@my_school_class</code>", parse_mode="HTML")
        return
    global current_channel
    current_channel = channel
    save_setting("channel", current_channel)
    await message.answer(f"✅ Канал изменён: {current_channel}")

# === Помощь — ТОЛЬКО для учителя ===
@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Помощь")
async def teacher_help(message: types.Message):
    if message.from_user.id != TEACHER_ID:
        return

    help_text = """
👨‍🏫 <b>Помощь для классного руководителя</b>

📌 <b>Команды:</b>

/start — запустить бота  
/set_channel @канал — изменить канал для отчётов  
/help — это сообщение

📌 <b>Кнопки:</b>

📋 Список класса — кто приходит/не приходит  
➕ Добавить дежурного — вручную назначить  
🗑️ Удалить ученика — по имени или @all  
📤 Повторить отчёт в канал — снова отправить выбор дежурного  
🔴 Стоп / 🟢 Старт — включить или выключить бота  
ℹ️ Помощь — эта подсказка

⏰ В 8:25 — приходит отчёт по посещаемости  
⏰ В 8:45 — можно выбрать дежурного из пришедших

🧹 После отчёта ученика — сообщение в канале меняется на "Дежурный не назначен"
"""

    await message.answer(help_text, parse_mode="HTML")

# === Ученик: Команды ===
@dp.message(F.text == "✅ Приду в школу")
async def mark_present(message: types.Message):
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Ожидайте команды от классного руководителя.")
        return
    cursor.execute("UPDATE users SET status='present', reason=NULL WHERE user_id=?", (message.from_user.id,))
    conn.commit()
    await message.answer("✅ Вы отметились как 'приду'.")

@dp.message(F.text == "❌ Не приду")
async def prompt_absent_reason(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Ожидайте.")
        return
    await message.answer("📝 Укажите причину отсутствия:")
    await state.set_state(Registration.awaiting_reason)

@dp.message(Registration.awaiting_reason)
async def mark_absent(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен.")
        await state.clear()
        return
    reason = message.text.strip()
    cursor.execute("UPDATE users SET status='absent', reason=? WHERE user_id=?", (reason, message.from_user.id))
    conn.commit()
    await message.answer(f"❌ Вы отмечены как 'не приду'. Причина: {reason}")
    await state.clear()

@dp.message(F.text == "🧹 Отчитаться о дежурстве")
async def report_duty(message: types.Message):
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Ожидайте.")
        return

    await message.answer("🧹 Вы отчитались о дежурстве! Молодец! 💪")

    msg_id = get_duty_message_id()
    if not msg_id:
        return

    try:
        await bot.edit_message_text(
            chat_id=current_channel,
            message_id=msg_id,
            text="🧹 Дежурства на сегодня:\nДежурный не назначен"
        )
    except Exception as e:
        print(f"[Ошибка редактирования] {e}")
        await bot.send_message(TEACHER_ID, f"⚠️ Не удалось изменить сообщение в канале: {e}")

# === ЗАПУСК ===
async def main():
    asyncio.create_task(run_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
