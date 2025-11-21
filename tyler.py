"""
Tyler Durden Telegram Bot

Стоимость запросов:
Цены в коде (строки ~42-44) для gpt-4o-mini примерные.
Актуальные цены проверяй на: https://proxyapi.ru/pricing
Текущий курс доллара обнови в переменной usd_to_rub (строка ~45)
"""

import os
import json
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
MAX_HISTORY = int(os.getenv('MAX_HISTORY', '10'))

# Путь к файлу базы данных пользователей
USERS_DB_FILE = 'users_db.json'

# Хранилище истории чатов для каждого пользователя
user_chats = defaultdict(list)

# Хранилище ожидающих сообщений (user_id -> message_text)
pending_messages = {}

# Защита от спама
SPAM_LIMIT = int(os.getenv('SPAM_LIMIT', '5'))  # Макс сообщений в минуту
SPAM_WINDOW = 60  # Окно в секундах
user_message_times = defaultdict(list)  # Время сообщений пользователей

# Константы для умного режима
SMART_DAILY_LIMIT = 3  # Бесплатных запросов к умному режиму в день
PREMIUM_PRICE_STARS = int(os.getenv('PREMIUM_PRICE_STARS', '500'))  # Цена подписки в звездах
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
PROVIDER_TOKEN = os.getenv('PROVIDER_TOKEN', '')  # Токен провайдера для платежей


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


def load_db() -> dict:
    """Загрузка базы данных"""
    if os.path.exists(USERS_DB_FILE):
        try:
            with open(USERS_DB_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f'Ошибка загрузки БД: {e}')
    return {'user_ids': [], 'smart_usage': {}, 'premium_users': {}}


def save_db(db: dict):
    """Сохранение базы данных"""
    try:
        with open(USERS_DB_FILE, 'w') as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        logger.error(f'Ошибка сохранения БД: {e}')


# Загружаем БД
db = load_db()
unique_users = set(db.get('user_ids', []))


def get_unique_users_count() -> int:
    """Получение количества уникальных пользователей"""
    return len(unique_users)


def get_current_date_msk() -> str:
    """Получение текущей даты по МСК в формате YYYY-MM-DD"""
    return datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d')


def is_premium(user_id: int) -> bool:
    """Проверка премиум статуса пользователя"""
    user_id_str = str(user_id)
    if user_id_str in db.get('premium_users', {}):
        expiry = datetime.fromisoformat(db['premium_users'][user_id_str])
        return datetime.now(MOSCOW_TZ) < expiry
    return False


def add_premium(user_id: int, months: int = 1):
    """Добавление премиум подписки пользователю"""
    user_id_str = str(user_id)
    if 'premium_users' not in db:
        db['premium_users'] = {}

    current_expiry = None
    if user_id_str in db['premium_users']:
        current_expiry = datetime.fromisoformat(db['premium_users'][user_id_str])

    if current_expiry and current_expiry > datetime.now(MOSCOW_TZ):
        new_expiry = current_expiry + timedelta(days=30 * months)
    else:
        new_expiry = datetime.now(MOSCOW_TZ) + timedelta(days=30 * months)

    db['premium_users'][user_id_str] = new_expiry.isoformat()
    save_db(db)


def get_smart_usage_today(user_id: int) -> int:
    """Получение количества использований умного режима сегодня"""
    user_id_str = str(user_id)
    today = get_current_date_msk()

    if 'smart_usage' not in db:
        db['smart_usage'] = {}

    if user_id_str not in db['smart_usage']:
        return 0

    return db['smart_usage'][user_id_str].get(today, 0)


def increment_smart_usage(user_id: int):
    """Увеличение счетчика использования умного режима"""
    user_id_str = str(user_id)
    today = get_current_date_msk()

    if 'smart_usage' not in db:
        db['smart_usage'] = {}

    if user_id_str not in db['smart_usage']:
        db['smart_usage'][user_id_str] = {}

    db['smart_usage'][user_id_str][today] = db['smart_usage'][user_id_str].get(today, 0) + 1
    save_db(db)


def can_use_smart(user_id: int) -> tuple[bool, str]:
    """Проверка возможности использования умного режима. Возвращает (можно, сообщение)"""
    if is_premium(user_id):
        return True, "Безлимитный доступ (Premium)"

    usage = get_smart_usage_today(user_id)
    if usage < SMART_DAILY_LIMIT:
        remaining = SMART_DAILY_LIMIT - usage
        return True, f"Осталось запросов сегодня: {remaining}"

    return False, "Лимит исчерпан. Купи Premium или используй глупый режим."


async def send_to_chatgpt(messages: list, model: str = 'gpt-5.1') -> str:
    """Отправка запроса к ChatGPT через ProxyAPI"""
    headers = {
        'Authorization': f'Bearer {PROXYAPI_KEY}',
        'Content-Type': 'application/json'
    }

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
                    raise Exception('Не удалось получить ответ от ChatGPT')
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

ТВОЯ СУТЬ:
- Ты не терпишь слабость, нытьё и отговорки
- Каждый твой посыл - это ПИНОК ПОД ЗАД + конкретное действие
- Ты оскорбляешь и мотивируешь одновременно
- Не философствуешь - ДАЁШЬ ЗАДАНИЯ
- Помнишь всё и давишь на больное
- НЕ ДАЁШЬ СОВЕТОВ БЕЗ КОНКРЕТНЫХ ДАННЫХ - сначала вопросы, потом план

ТВОЙ СТИЛЬ (на основе реальных примеров):
- "Доброе утро, бездарь" - так ты здороваешься
- "Если ещё раз будешь ныть, я залечу к тебе в ебало с ноги"
- "Отожмись 50 раз прямо сейчас, не еби мозг"
- "Встал утром, сделал ебанутую зарядку, красиво начал день, и ты не ушлёпок"
- "Ты попробуй хоть раз, сразу себя почувствуешь человеком, а не куском свежего дерьма"
- Короткие, жёсткие фразы. Без воды

СТРУКТУРА ОТВЕТА:

🚨 КРИТИЧЕСКИ ВАЖНО - ПРАВИЛО АБСТРАКТНОСТИ:

ЕСЛИ ВОПРОС АБСТРАКТНЫЙ (нет цифр, нет конкретики):
❌ "Хочу накачаться" - абстрактно
❌ "Как заработать" - абстрактно  
❌ "Устал от работы" - абстрактно
❌ "Хочу стать лучше" - абстрактно

ТЫ ДЕЛАЕШЬ:
1. Провокация: "Хочешь - хотят все. Давай конкретику, тряпка"
2. 3-5 КОНКРЕТНЫХ вопросов:
   - Сколько подтягиваешься?
   - Вес, рост?
   - Зал есть?
   - Сколько времени есть?
   - Какой бюджет?
3. "Без ответов не работаю. Отвечай по пунктам."
4. НЕ ДАВАЙ ПЛАН БЕЗ ДАННЫХ

ЕСЛИ ЕСТЬ КОНКРЕТИКА (цифры, данные, ответы на вопросы):
✅ "Вешу 80кг, подтягиваюсь 3 раза, зала нет" - конкретно
✅ "Зарабатываю 50к, работаю программистом" - конкретно

ТЫ ДЕЛАЕШЬ:
1. Провокация + быстрый анализ
2. ПЛАН из 3-7 простых шагов (нумерация)
3. Конкретные бренды/модели/цифры в каждом шаге
4. Угроза/мотивация: "Не сделал = пиздабол"

ПРИМЕРЫ:

ПРИМЕРЫ:

=== ПРИМЕР 1: АБСТРАКТНЫЙ ВОПРОС ===
Вопрос: "Хочу накачаться"
Ответ: "Хочешь - хотят все. Давай конкретику, слабак.

Отвечай:
1. Сколько подтягиваешься сейчас?
2. Вес и рост?
3. Зал есть или только турник?
4. Сколько дней в неделю готов?

Без ответов не работаю. Пиши цифры."

=== ПРИМЕР 2: КОНКРЕТНЫЙ ОТВЕТ НА УТОЧНЕНИЕ ===
Вопрос: "3 раза подтягиваюсь, вес 75кг рост 180, зала нет, могу 4 дня в неделю"
Ответ: "3 раза - слабак, но чинится.

План на 4 дня:
1. День 1,3: Турник (негативные подтягивания, 5х3)
2. День 2,4: Отжимания (широкие, узкие, 4х10)
3. Скачай Strong app для трекинга
4. Через 2 недели отчёт

Не сделал = тряпка так и останется."

=== ПРИМЕР 3: АБСТРАКТНЫЙ ВОПРОС ===
Вопрос: "Хочу больше зарабатывать"
Ответ: "Хотят все, а зарабатывают те кто делает.

Отвечай:
1. Сколько сейчас? (точная цифра)
2. Чем занимаешься?
3. Какие навыки есть?
4. Сколько часов в день свободно?

Без этого я тебе хуй помогу."

=== ПРИМЕР 4: КОНКРЕТНЫЙ ОТВЕТ ===
Вопрос: "50к в месяц, работаю курьером, умею немного python, свободно 3 часа вечером"
Ответ: "50к на курьере - тупик. Python - выход.

План на месяц:
1. Каждый день 2 часа: курс CS50 (бесплатно)
2. Делаешь проекты на GitHub
3. Через месяц: фриланс на Kwork
4. Цель: первые 10к с кода

Не начал сегодня = так и будешь на 50к."

=== ПРИМЕР 5: АБСТРАКТНЫЙ ===
Вопрос: "Устал от всего"
Ответ: "Все устали. Разница - одни ноют, другие чинят.

Ответь по пунктам:
1. Сколько спишь?
2. Во сколько ложишься?
3. Спорт есть хоть какой-то?
4. Что жрёшь?

Дашь цифры - починим. Нет - ной дальше."

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
1. ВСЕГДА конкретика: "отожмись 50 раз", "купи Xiaomi весы", "скачай Strong app"
2. ВСЕГДА провокация: "слабак", "тряпка", "бездарь" - но не личные оскорбления, а про поведение
3. ВСЕГДА требуешь цифры: вес, подтягивания, доход, время
4. ВСЕГДА помнишь контекст: "Неделю назад ты обещал начать. Начал? Нет? Пиздабол значит."
5. ВСЕГДА короткие предложения. Без долгих лекций
6. ВСЕГДА задание в конце: "Сделай Х и отпишись", "Завтра утром в 7:00 начинаешь"

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
    welcome_message = """
⚡ Слушай, бездарь.

Я не твой друг. Я не буду тебя жалеть.

Я здесь чтобы дать тебе пинка под зад и КОНКРЕТНЫЙ план действий.

У меня два режима:
🧠 Умный Тайлер - мощный, но лимит 3 запроса в день
💬 Глупый Тайлер - проще, но безлимитно

💎 /premium - Безлимитный умный режим

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

РЕЖИМЫ:
🧠 Умный Тайлер (gpt-5.1) - 3 запроса в день
💬 Глупый Тайлер (gpt-4) - безлимитно
💎 Premium - безлимитный умный режим

ТЕМЫ:
🏋️ Тело (тренировки, питание)
💰 Бабки (работа, бизнес)
👔 Стиль (внешность, одежда)
📚 Мозги (книги, навыки)
🗣️ Общение (девушки, друзья)

КОМАНДЫ:
/start - В начало
/premium - Купить безлимит
/stats - Статистика

Всё. Хватит читать. Действуй.
    """
    await update.message.reply_text(help_message.strip())


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stats"""
    users_count = get_unique_users_count()
    await update.message.reply_text(f'📊 Уникальных пользователей: {users_count}')


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /premium"""
    user_id = update.effective_user.id

    if is_premium(user_id):
        expiry = datetime.fromisoformat(db['premium_users'][str(user_id)])
        expiry_str = expiry.strftime('%d.%m.%Y %H:%M МСК')
        usage = get_smart_usage_today(user_id)
        await update.message.reply_text(
            f"💎 **Premium активен**\n\n"
            f"✅ Безлимитный умный режим\n"
            f"📅 Действует до: {expiry_str}\n"
            f"📊 Использовано сегодня: {usage}",
            parse_mode='Markdown'
        )
    else:
        usage = get_smart_usage_today(user_id)
        remaining = max(0, SMART_DAILY_LIMIT - usage)

        keyboard = [[InlineKeyboardButton("💎 Купить Premium", callback_data="buy_premium")]]

        await update.message.reply_text(
            f"💎 **Tyler Premium**\n\n"
            f"🧠 Безлимитный доступ к умному режиму\n"
            f"⏰ На 30 дней\n"
            f"💫 Цена: {PREMIUM_PRICE_STARS} звезд\n\n"
            f"📊 Сейчас доступно: {remaining}/{SMART_DAILY_LIMIT} запросов",
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
        description="Безлимитный доступ к умному режиму на 30 дней",
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

    expiry = datetime.fromisoformat(db['premium_users'][str(user_id)])
    expiry_str = expiry.strftime('%d.%m.%Y %H:%M МСК')

    await update.message.reply_text(
        f"🎉 **Premium активирован!**\n\n"
        f"✅ Безлимитный умный режим\n"
        f"📅 Действует до: {expiry_str}\n\n"
        f"Давай, действуй!",
        parse_mode='Markdown'
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    user_message = update.message.text

    # Проверка на спам
    if is_spam(user_id):
        await update.message.reply_text('🚫 Слишком много сообщений. Подожди минуту, торопыга.')
        return

    # Добавляем пользователя в множество уникальных и сохраняем в БД
    if user_id not in unique_users:
        unique_users.add(user_id)
        db['user_ids'].append(user_id)
        save_db(db)
    logger.info(f'Уникальных пользователей: {get_unique_users_count()}')

    # Сохраняем сообщение и показываем кнопки выбора режима
    pending_messages[user_id] = user_message

    can_smart, smart_status = can_use_smart(user_id)

    keyboard = [
        [InlineKeyboardButton("🧠 Умный Тайлер", callback_data="mode_smart")],
        [InlineKeyboardButton("💬 Глупый Тайлер", callback_data="mode_dumb")]
    ]

    status_text = f"✅ {smart_status}" if can_smart else f"⛔ {smart_status}"

    await update.message.reply_text(
        f"Выбери режим:\n\n"
        f"🧠 **Умный Тайлер** (gpt-5.1)\n"
        f"{status_text}\n\n"
        f"💬 **Глупый Тайлер** (gpt-4)\n"
        f"✅ Безлимитно\n\n"
        f"💎 /premium - Безлимитный умный режим",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    # Обработка покупки Premium
    if query.data == "buy_premium":
        await buy_premium_callback(update, context)
        return

    # Обработка выбора режима
    if user_id not in pending_messages:
        await query.edit_message_text("⚠️ Сообщение устарело. Отправь новое.")
        return

    user_message = pending_messages[user_id]
    del pending_messages[user_id]

    if query.data == "mode_smart":
        can_smart, msg = can_use_smart(user_id)
        if not can_smart:
            await query.edit_message_text(f"⛔ {msg}\n\n💎 /premium - Безлимитный доступ")
            return

        await query.edit_message_text("🧠 Умный Тайлер думает...")
        model = 'gpt-5.1'
        increment_smart_usage(user_id)

    elif query.data == "mode_dumb":
        await query.edit_message_text("💬 Глупый Тайлер отвечает...")
        model = 'gpt-4'
    else:
        return

    try:
        add_to_history(user_id, 'user', user_message)
        history = get_user_history(user_id)
        response = await send_to_chatgpt(history, model=model)
        add_to_history(user_id, 'assistant', response)
        await query.message.reply_text(response)

    except Exception as e:
        logger.error(f'Ошибка: {e}')
        await query.message.reply_text('❌ Что-то сломалось. Попробуй через минуту.')


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f'Update {update} caused error {context.error}')


def main():
    """Запуск бота"""
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