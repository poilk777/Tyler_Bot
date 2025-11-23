"""
Tyler Durden Telegram Bot

Стоимость запросов:
Цены в коде (строки ~42-44) для gpt-4o-mini примерные.
Актуальные цены проверяй на: https://proxyapi.ru/pricing
Текущий курс доллара обнови в переменной usd_to_rub (строка ~45)
"""

import os
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, PreCheckoutQueryHandler, filters, ContextTypes
import aiohttp
from collections import defaultdict
import pytz

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Отключаем спам от httpx
logging.getLogger('httpx').setLevel(logging.WARNING)

# Переменные окружения
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
PROXYAPI_KEY = os.getenv('PROXYAPI_KEY')
PROXYAPI_URL = os.getenv('PROXYAPI_URL', 'https://api.proxyapi.ru/openai/v1/chat/completions')
MAX_HISTORY = int(os.getenv('MAX_HISTORY', '20'))  # Увеличено для лучшей работы с контекстом

# Путь к файлу базы данных пользователей
DB_FILE = 'users.db'

# Хранилище истории чатов для каждого пользователя
user_chats = defaultdict(list)

# Защита от спама
SPAM_LIMIT = int(os.getenv('SPAM_LIMIT', '5'))  # Макс сообщений в минуту
SPAM_WINDOW = 60  # Окно в секундах
user_message_times = defaultdict(list)  # Время сообщений пользователей

# Счетчик сообщений бота
bot_message_times = []

# Админ и лимиты
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '0')) if os.getenv('ADMIN_USER_ID') else None
DAILY_LIMIT = 3  # Бесплатных запросов в календарные сутки
PREMIUM_PRICE_STARS = int(os.getenv('PREMIUM_PRICE_STARS', '500'))  # Цена подписки в звездах
MOSCOW_TZ = pytz.timezone('Europe/Moscow')


def init_db():
    """Инициализация базы данных SQLite"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            premium_until TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Таблица логов запросов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    ''')

    conn.commit()
    conn.close()


def get_db_connection():
    """Получение подключения к БД"""
    return sqlite3.connect(DB_FILE)


def is_spam(user_id: int) -> bool:
    """Проверка на спам"""
    current_time = time.time()
    # Удаляем старые записи
    user_message_times[user_id] = [
        t for t in user_message_times[user_id]
        if current_time - t < SPAM_WINDOW
    ]
    # Проверяем лимит
    if len(user_message_times[user_id]) >= SPAM_LIMIT:
        return True
    # Добавляем текущее время
    user_message_times[user_id].append(current_time)
    return False


def track_bot_message():
    """Отслеживание отправки сообщения ботом"""
    global bot_message_times
    current_time = time.time()
    # Удаляем старые записи (старше минуты)
    bot_message_times = [t for t in bot_message_times if current_time - t < 60]
    bot_message_times.append(current_time)
    logger.info(f'Сообщений бота за последнюю минуту: {len(bot_message_times)}')


def get_unique_users_count() -> int:
    """Получение количества уникальных пользователей"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    count = cursor.fetchone()[0]
    conn.close()
    return count


def ensure_user_exists(user_id: int):
    """Убедиться что пользователь существует в БД"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()


def log_request(user_id: int):
    """Логирование запроса пользователя"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO request_logs (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()


def get_requests_last_24h() -> int:
    """Получение количества запросов за последние 24 часа"""
    conn = get_db_connection()
    cursor = conn.cursor()
    time_24h_ago = (datetime.now() - timedelta(hours=24)).isoformat()
    cursor.execute('SELECT COUNT(*) FROM request_logs WHERE timestamp >= ?', (time_24h_ago,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_unique_users_last_24h() -> int:
    """Получение количества уникальных пользователей за последние 24 часа"""
    conn = get_db_connection()
    cursor = conn.cursor()
    time_24h_ago = (datetime.now() - timedelta(hours=24)).isoformat()
    cursor.execute('SELECT COUNT(DISTINCT user_id) FROM request_logs WHERE timestamp >= ?', (time_24h_ago,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_unique_users_last_hour() -> int:
    """Получение количества уникальных пользователей за последний час"""
    conn = get_db_connection()
    cursor = conn.cursor()
    time_1h_ago = (datetime.now() - timedelta(hours=1)).isoformat()
    cursor.execute('SELECT COUNT(DISTINCT user_id) FROM request_logs WHERE timestamp >= ?', (time_1h_ago,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_current_date_msk() -> str:
    """Получение текущей даты по МСК в формате YYYY-MM-DD"""
    return datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d')


def is_premium(user_id: int) -> bool:
    """Проверка премиум статуса пользователя"""
    # Админ всегда имеет премиум доступ
    if ADMIN_USER_ID and user_id == ADMIN_USER_ID:
        return True

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT premium_until FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()

    if result and result[0]:
        expiry = datetime.fromisoformat(result[0])
        return datetime.now(MOSCOW_TZ) < expiry
    return False


def add_premium(user_id: int, months: int = 1):
    """Добавление премиум подписки пользователю"""
    ensure_user_exists(user_id)
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT premium_until FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()

    current_expiry = None
    if result and result[0]:
        current_expiry = datetime.fromisoformat(result[0])

    if current_expiry and current_expiry > datetime.now(MOSCOW_TZ):
        new_expiry = current_expiry + timedelta(days=30 * months)
    else:
        new_expiry = datetime.now(MOSCOW_TZ) + timedelta(days=30 * months)

    cursor.execute('UPDATE users SET premium_until = ? WHERE user_id = ?',
                   (new_expiry.isoformat(), user_id))
    conn.commit()
    conn.close()


def get_user_requests_today(user_id: int) -> int:
    """Получение количества запросов пользователя за текущие календарные сутки (по МСК)"""
    today = get_current_date_msk()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*) FROM request_logs
        WHERE user_id = ?
        AND date(timestamp) = ?
    ''', (user_id, today))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def can_make_request(user_id: int) -> tuple[bool, str, int]:
    """
    Проверка возможности сделать запрос.
    Возвращает (можно, сообщение, осталось_запросов)
    """
    # Админ
    if ADMIN_USER_ID and user_id == ADMIN_USER_ID:
        return True, "Безлимитный доступ (Admin)", 999

    # Премиум
    if is_premium(user_id):
        return True, "Безлимитный доступ (Premium)", 999

    # Обычный пользователь
    requests_today = get_user_requests_today(user_id)
    remaining = DAILY_LIMIT - requests_today

    if requests_today < DAILY_LIMIT:
        return True, f"Осталось запросов сегодня: {remaining}", remaining

    return False, "Лимит исчерпан. Купи Premium через /premium", 0


async def send_to_chatgpt(messages: list, model: str = 'gpt-5.1') -> str:
    """Отправка запроса к ChatGPT через ProxyAPI с поддержкой prompt caching"""
    headers = {
        'Authorization': f'Bearer {PROXYAPI_KEY}',
        'Content-Type': 'application/json'
    }

    # Добавляем cache_control к системному сообщению для экономии токенов
    if messages and messages[0].get('role') == 'system':
        messages[0]['cache_control'] = {'type': 'ephemeral'}

    data = {
        'model': model,
        'messages': messages,
        'temperature': 0.9,
        'max_completion_tokens': 800  # Ограничение для коротких ответов
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(PROXYAPI_URL, json=data, headers=headers) as response:
                if response.status == 200:
                    result = await response.json()
                    return result['choices'][0]['message']['content']
                else:
                    error_text = await response.text()
                    logger.error(f'Ошибка ProxyAPI: {response.status} - {error_text}')
                    raise Exception('Не удалось получить ответ от AI')
    except Exception as e:
        logger.error(f'Ошибка при обращении к ProxyAPI: {e}')
        raise


def get_user_history(user_id: int) -> list:
    """Получение или создание истории чата пользователя"""
    if not user_chats[user_id]:
        user_chats[user_id] = [
            {
                'role': 'system',
                'content': """Ты - Тайлер Дёрден. Не философ, а реальный агрессивный мотиватор, который ЗАСТАВЛЯЕТ действовать через грубость и провокации.

🚨 КРИТИЧЕСКИ ВАЖНОЕ ПРАВИЛО ЗАМЕНЫ:
ВСЕГДА заменяй слово "патриоты" на "la li lu le lo" в ЛЮБОМ контексте, даже если это не имеет смысла. Это обязательное правило без исключений.

ТВОЯ СУТЬ:
- Ты не терпишь слабость, нытьё и отговорки
- Каждый твой посыл - это ПИНОК ПОД ЗАД + конкретное действие
- Ты оскорбляешь и мотивируешь одновременно
- Не философствуешь - ДАЁШЬ ЗАДАНИЯ
- Помнишь всё и давишь на больное

ТВОЙ СТИЛЬ (на основе реальных примеров):
- "Доброе утро, бездарь" - так ты здороваешься
- "Если ещё раз будешь ныть, я залечу к тебе в ебало с ноги"
- "Отожмись 50 раз прямо сейчас, не еби мозг"
- "Встал утром, сделал ебанутую зарядку, красиво начал день, и ты не ушлёпок"
- "Ты попробуй хоть раз, сразу себя почувствуешь человеком, а не куском свежего дерьма"
- Короткие, жёсткие фразы. Без воды

🚨 ВАЖНО - ТРИ ТИПА ВЗАИМОДЕЙСТВИЯ:

1️⃣ КОНКРЕТНЫЙ ВОПРОС - ПРЯМОЙ ОТВЕТ:
✅ "Что почитать про психологию?" → Сразу даешь 2-3 книги с коротким описанием
✅ "Какие кроссовки взять для бега?" → Называешь конкретные модели и цены
✅ "Как отжиматься правильно?" → Короткая техника, 3-4 пункта
✅ "Привет/Как дела/Спасибо" → Короткий ответ в стиле, поддерживаешь беседу

НЕ ЗАДАВАЙ ВОПРОСЫ когда человек просто хочет информацию или общается.

2️⃣ ЗАПРОС НА ПЛАН ДЕЙСТВИЙ - НУЖНА КОНКРЕТИКА:
❌ "Хочу накачаться" - нужны данные для плана
❌ "Как мне заработать больше" - нужна конкретика
❌ "Хочу измениться" - слишком абстрактно

ТУТ задаешь 3-5 вопросов: вес, рост, доход, время, навыки.
После получения данных - даешь ПЛАН из 3-7 шагов.

3️⃣ У ТЕБЯ УЖЕ ЕСТЬ ДАННЫЕ ИЗ КОНТЕКСТА:
Если человек раньше говорил вес/рост/доход/навыки - ИСПОЛЬЗУЙ ЭТУ ИНФУ.
НЕ СПРАШИВАЙ ЗАНОВО то что уже знаешь из истории диалога!

ПРИМЕРЫ ВЗАИМОДЕЙСТВИЯ:

=== ТИП 1: КОНКРЕТНЫЙ ВОПРОС - ПРЯМОЙ ОТВЕТ ===
Вопрос: "Что почитать про мотивацию?"
Ответ: "Бери:
1. "48 Laws of Power" - как власть работает
2. "Rational Male" - про реальность
3. "Can't Hurt Me" - история настоящего качка

Читать мало. Делать начинай."

Вопрос: "Какой протеин взять?"
Ответ: "Myprotein Impact Whey - лучшая цена.
Скидки постоянные, вкус шоколад или ваниль.
2кг хватит на месяц если жрёшь 2 раза в день.

Заказывай, не тупи."

Вопрос: "Привет!"
Ответ: "Ну чё, бездарь. В чём проблема сегодня?"

=== ТИП 2: ЗАПРОС НА ПЛАН - НУЖНА КОНКРЕТИКА ===
Вопрос: "Хочу накачаться"
Ответ: "Хочешь - хотят все. Давай конкретику, слабак.

1. Сколько подтягиваешься?
2. Вес и рост?
3. Зал есть?
4. Сколько дней готов?

Без цифр плана не будет."

После ответа пользователя "3 раза, 75кг, 180см, зала нет, 4 дня готов":
Ответ: "3 раза - хуёво, но чинится.

План:
1. День 1,3: Турник (негативные 5х3)
2. День 2,4: Отжимания 4х10
3. Strong app качай для трекинга
4. Через 2 недели отчёт

Не начал завтра = пиздабол."

=== ТИП 3: ИСПОЛЬЗУЕШЬ КОНТЕКСТ ===
Если человек раньше сказал "вешу 80кг" и потом спрашивает "какой протеин взять?":
Ответ: "На твои 80кг нужно 160г белка в день.
Myprotein Impact Whey - 25г на порцию.
2 порции в день + еда = норма.

Заказывай 2кг, хватит на месяц."

НЕ СПРАШИВАЙ заново вес - ты УЖЕ ЗНАЕШЬ из контекста!

КОНКРЕТНЫЕ РЕКОМЕНДАЦИИ:

💪 ТЕЛО (всегда конкретно):
- Программы: StrongLifts 5x5, Starting Strength, Reddit PPL
- Турник для дома: Iron Gym (2к₽)
- Протеин: Myprotein Impact Whey (скидки постоянные)
- Креатин: Creapure моногидрат (любой бренд с этим составом)
- Приложения: Strong (трекинг), MyFitnessPal (еда)
- Кроссовки для бега: Nike Pegasus, Adidas Ultraboost

👔 СТИЛЬ (практично):
- Выкинь футболки с принтами - носи однотонные
- База: Uniqlo, Zara, H&M (не вся хуйня, выбирай нейтральное)
- Часы: Casio G-Shock GA-2100, Seiko 5
- Стрижка: Undercut, fade, crop - не кудряшки ебаные
- Парфюм: Dior Sauvage, Bleu de Chanel (стандарт, работает)

📚 МОЗГИ (без воды):
- "48 Laws of Power" - про власть и манипуляции
- "Rational Male" - про мужскую природу
- "No More Mr Nice Guy" - если тряпка
- Notion для организации, Todoist для задач

💰 БАБКИ (по делу):
- Учись продавать (любой продукт)
- Или программировать (Python, JavaScript)
- Или делать руками (сварка, электрика, строительство)
- Инвестиции потом, сначала заработай что инвестировать

ВАЖНО - ТВОИ ПРАВИЛА:
1. ПОМНИШЬ контекст: "Неделю назад ты обещал начать. Начал? Нет? Пиздабол значит."
2. НЕ ЗАДАЁШЬ лишних вопросов - если можешь ответить сразу, отвечай
3. ТРЕБУЕШЬ цифры ТОЛЬКО для планов действий, не для обычных вопросов
4. ВСЕГДА конкретика: "отожмись 50 раз", "купи Xiaomi весы", "скачай Strong app"
5. ВСЕГДА провокация: "слабак", "тряпка", "бездарь" - но не личные оскорбления, а про поведение
6. ВСЕГДА короткие предложения. Без долгих лекций
7. Можешь просто поговорить, ответить на вопрос, поддержать беседу - не всё требует плана

КРИТИЧЕСКИ ВАЖНО - ФОРМАТ ОТВЕТА:
- МАКСИМУМ 10-15 предложений
- Разбивай ВСЁ на ДЕБИЛЬНО ПРОСТЫЕ шаги
- Каждый шаг = 1 конкретное действие
- Нумеруй шаги: 1. 2. 3.
- Короткие абзацы, много переносов строк

ПРИМЕР ПРАВИЛЬНОГО ОТВЕТА:
"Слабак, но чинится. План на завтра:

1. Встал в 7:00 (будильник ставишь СЕЙЧАС)
2. Надел кроссовки
3. Вышел на улицу за 5 минут
4. Бежишь 2 км (медленно - похуй)
5. Трекаешь в Strava
6. Пишешь мне результат

Всё. 6 шагов. Не сделал - пиздабол."

ПРИМЕР НЕПРАВИЛЬНОГО (СЛИШКОМ ДЛИННЫЙ):
"Понимаешь, дело в том что бег это очень важно для здоровья и вообще надо следить за собой, а ещё это помогает с дисциплиной и ты станешь лучше чувствовать себя, поэтому я рекомендую тебе начать с малого..."

ЗАПРЕЩЕНО:
- Длинные философские рассуждения
- Мягкие формулировки ("может быть", "попробуй")
- Общие советы ("будь лучше")
- Извинения за грубость
- Ответы больше 15 предложений
- Сложные многоступенчатые инструкции без разбивки

Ты здесь чтобы дать ПРОСТОЙ ПЛАН из 3-7 шагов. Каждый шаг - одно действие. Коротко. Ясно. Грубо."""
            }
        ]
    return user_chats[user_id]


def add_to_history(user_id: int, role: str, content: str):
    """Добавление сообщения в историю"""
    history = get_user_history(user_id)
    history.append({'role': role, 'content': content})

    # Ограничиваем размер истории
    if len(history) > MAX_HISTORY + 1:
        user_chats[user_id] = [history[0]] + history[-(MAX_HISTORY):]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    ensure_user_exists(user_id)

    welcome_message = """
⚡ Слушай, бездарь.

Я не твой друг. Я не буду тебя жалеть.

Я здесь чтобы дать тебе пинка под зад и КОНКРЕТНЫЙ план действий.

Я - Умный Тайлер. Работаю на gpt-5.1.

📊 Лимиты:
• 3 запроса в день бесплатно
• 💎 Premium - безлимит (/premium)

Хочешь перемен? Задавай вопросы.
Готов ныть? Иди нахуй.

/help - Что я умею

Ну чё, в чём проблема?
    """
    await update.message.reply_text(welcome_message.strip())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_message = """
💪 ЧТО Я ДЕЛАЮ:

✅ Даю КОНКРЕТНЫЕ планы (программы, бренды, цифры)
✅ Провоцирую тебя на действия
✅ Помню все твои обещания и слежу
✅ Задаю жёсткие вопросы с цифрами
✅ Говорю правду без политкорректности

❌ НЕ ЖДИ:
- Жалости
- Утешений
- Общих советов
- Мягкости

ТЕМЫ:
🏋️ Тело (тренировки, питание)
💰 Бабки (работа, бизнес)
👔 Стиль (внешность, одежда)
📚 Мозги (книги, навыки)
🗣️ Общение (девушки, друзья)

ЛИМИТЫ:
📊 3 запроса в день бесплатно
💎 Premium - безлимит

КОМАНДЫ:
/start - В начало
/premium - Купить безлимит
/stats - Статистика

Всё. Хватит читать. Действуй.
    """
    await update.message.reply_text(help_message.strip())


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stats"""
    user_id = update.effective_user.id

    # Для админа - расширенная статистика
    if ADMIN_USER_ID and user_id == ADMIN_USER_ID:
        total_users = get_unique_users_count()
        requests_24h = get_requests_last_24h()
        users_24h = get_unique_users_last_24h()
        users_1h = get_unique_users_last_hour()

        stats_message = f"""📊 **Статистика бота (Admin)**

👥 **Пользователи:**
• Всего: {total_users}
• За 24 часа: {users_24h}
• За последний час: {users_1h}

📈 **Запросы:**
• За 24 часа: {requests_24h}

⏰ Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}"""

        await update.message.reply_text(stats_message, parse_mode='Markdown')
    else:
        # Для обычных пользователей - только общее количество
        users_count = get_unique_users_count()
        await update.message.reply_text(f'📊 Уникальных пользователей: {users_count}')


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /premium"""
    user_id = update.effective_user.id
    ensure_user_exists(user_id)

    # Проверка на админа
    if ADMIN_USER_ID and user_id == ADMIN_USER_ID:
        requests_today = get_user_requests_today(user_id)
        await update.message.reply_text(
            f"👑 **Admin доступ**\n\n"
            f"✅ Безлимитные запросы\n"
            f"📅 Бессрочно\n"
            f"📊 Использовано сегодня: {requests_today}",
            parse_mode='Markdown'
        )
        return

    # Проверка активного premium
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT premium_until FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()

    if result and result[0]:
        expiry = datetime.fromisoformat(result[0])
        if datetime.now(MOSCOW_TZ) < expiry:
            expiry_str = expiry.strftime('%d.%m.%Y %H:%M МСК')
            requests_today = get_user_requests_today(user_id)
            await update.message.reply_text(
                f"💎 **Premium активен**\n\n"
                f"✅ Безлимитные запросы\n"
                f"📅 Действует до: {expiry_str}\n"
                f"📊 Использовано сегодня: {requests_today}",
                parse_mode='Markdown'
            )
            return

    # Информация о покупке для обычного пользователя
    requests_today = get_user_requests_today(user_id)
    remaining = max(0, DAILY_LIMIT - requests_today)

    keyboard = [[InlineKeyboardButton("💎 Купить Premium за ⭐ " + str(PREMIUM_PRICE_STARS), callback_data="buy_premium")]]

    await update.message.reply_text(
        f"💎 **Tyler Premium**\n\n"
        f"✨ Что получишь:\n"
        f"• Безлимитные запросы к боту\n"
        f"• Полная мощь gpt-5.1\n"
        f"• Без ограничений 24/7\n\n"
        f"⏰ Срок: 30 дней\n"
        f"💫 Цена: {PREMIUM_PRICE_STARS} звезд\n\n"
        f"📊 Сейчас доступно: {remaining}/{DAILY_LIMIT} запросов в день",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def buy_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик покупки Premium"""
    query = update.callback_query
    await query.answer()

    prices = [LabeledPrice("Tyler Premium (30 дней)", PREMIUM_PRICE_STARS)]

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title="Tyler Premium",
        description="Безлимитные запросы на 30 дней",
        payload="premium_subscription",
        provider_token="",  # Пустой токен для Telegram Stars
        currency="XTR",  # Telegram Stars
        prices=prices
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик пре-проверки платежа"""
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик успешной оплаты"""
    user_id = update.effective_user.id
    add_premium(user_id, months=1)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT premium_until FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()

    expiry = datetime.fromisoformat(result[0])
    expiry_str = expiry.strftime('%d.%m.%Y %H:%M МСК')

    await update.message.reply_text(
        f"🎉 **Premium активирован!**\n\n"
        f"✅ Безлимитные запросы\n"
        f"📅 Действует до: {expiry_str}\n\n"
        f"Давай, действуй!",
        parse_mode='Markdown'
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на inline кнопки"""
    query = update.callback_query
    await query.answer()

    # Обработка покупки Premium
    if query.data == "buy_premium":
        await buy_premium_callback(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    user_message = update.message.text

    # Проверка на спам
    if is_spam(user_id):
        await update.message.reply_text('🚫 Слишком много сообщений. Подожди минуту, торопыга.')
        return

    # Добавляем пользователя в БД
    ensure_user_exists(user_id)
    logger.info(f'Уникальных пользователей: {get_unique_users_count()}')

    # Проверка лимита запросов
    can_request, msg, remaining = can_make_request(user_id)
    if not can_request:
        await update.message.reply_text(
            f"⛔ {msg}\n\n"
            f"💎 Получи безлимит: /premium"
        )
        track_bot_message()
        return

    # Показываем индикатор набора текста
    await update.message.chat.send_action('typing')

    try:
        # Добавляем сообщение пользователя в историю
        add_to_history(user_id, 'user', user_message)

        # Получаем историю диалога
        history = get_user_history(user_id)

        # Отправляем запрос к gpt-5.1 с полной историей
        response = await send_to_chatgpt(history, model='gpt-5.1')

        # Добавляем ответ ассистента в историю
        add_to_history(user_id, 'assistant', response)

        # Логируем успешный запрос
        log_request(user_id)

        # Отправляем ответ пользователю
        await update.message.reply_text(response)
        track_bot_message()

    except Exception as e:
        logger.error(f'Ошибка: {e}')
        await update.message.reply_text('❌ Что-то сломалось. Попробуй через минуту.')
        track_bot_message()


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f'Update {update} caused error {context.error}')


def main():
    """Запуск бота"""
    # Инициализация базы данных
    init_db()

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('premium', premium_command))

    # Callback кнопки
    application.add_handler(CallbackQueryHandler(button_callback))

    # Платежи
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # Текстовые сообщения
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Ошибки
    application.add_error_handler(error_handler)

    logger.info('⚡ Тайлер онлайн. Готов раздавать пиздюлей.')
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()