import asyncio
import ssl
import re
import sqlite3
from urllib.request import urlopen
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Потрібен Python 3.9 або новіше

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import logging
from bs4 import BeautifulSoup

# -------------------- Налаштування -------------------- #
BOT_TOKEN = "TOKEN"  # Вкажіть тут свій справжній токен бота
DB_PATH = "products.db"  # Ім'я файлу бази даних

# Якщо TESTING=True, для тестування оновлення надсилатимуться кожну хвилину;
# Якщо TESTING=False, оновлення будуть надсилатися щодня у встановлений час.
TESTING = False

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# -------------------- Функції для роботи з базою даних -------------------- #
def init_db():
    """
    Створюємо таблиці для відстежуваних продуктів та налаштувань користувачів,
    якщо вони ще не існують. Це нам потрібно для збереження даних.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Таблиця для збереження інформації про продукти, які ви відстежуєте
    c.execute('''
        CREATE TABLE IF NOT EXISTS tracked_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            title TEXT,
            price TEXT,
            last_scrape TEXT
        )
    ''')
    # Таблиця для збереження часу оновлення, який встановлює користувач
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            update_time TEXT
        )
    ''')
    conn.commit()
    conn.close()

def add_tracked_product(user_id: int, url: str, title: str, price: str, timestamp: str):
    """Додаємо новий запис про відстежуваний продукт у базу даних."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO tracked_products (user_id, url, title, price, last_scrape)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, url, title, price, timestamp))
    conn.commit()
    conn.close()

def update_tracked_product(product_id: int, title: str, price: str, timestamp: str):
    """Оновлюємо інформацію про відстежуваний продукт у базі даних."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE tracked_products
        SET title = ?, price = ?, last_scrape = ?
        WHERE id = ?
    """, (title, price, timestamp, product_id))
    conn.commit()
    conn.close()

def delete_tracked_product(user_id: int, url: str) -> bool:
    """
    Видаляємо запис про продукт для конкретного користувача за URL.
    Повертаємо True, якщо запис успішно видалено.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tracked_products WHERE user_id = ? AND url = ?", (user_id, url))
    conn.commit()
    changes = c.rowcount
    conn.close()
    return changes > 0

def get_all_tracked_products():
    """Повертаємо всі записи про відстежувані продукти з бази."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, user_id, url, title, price, last_scrape FROM tracked_products")
    rows = c.fetchall()
    conn.close()
    return rows

def get_tracked_product(user_id: int, url: str):
    """
    Повертаємо запис про продукт для заданого користувача та URL,
    або повертаємо None, якщо такий запис не знайдено.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, price, last_scrape FROM tracked_products WHERE user_id = ? AND url = ?", (user_id, url))
    product = c.fetchone()
    conn.close()
    return product

def get_products_for_user(user_id: int):
    """Повертаємо всі продукти, які відстежує конкретний користувач."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, url, title, price, last_scrape FROM tracked_products WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# Функції для роботи з налаштуваннями користувачів
def set_user_update_time(user_id: int, update_time: str):
    """Встановлюємо або оновлюємо бажаний час оновлення для користувача (у форматі HH:MM)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_settings (user_id, update_time) VALUES (?, ?)", (user_id, update_time))
    conn.commit()
    conn.close()

def get_all_user_settings():
    """Повертаємо всі налаштування користувачів як список кортежів (user_id, update_time)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, update_time FROM user_settings")
    rows = c.fetchall()
    conn.close()
    return rows

# Ініціалізуємо базу даних (якщо таблиці ще не створені, вони будуть створені)
init_db()

# -------------------- Налаштування для скрейпінгу -------------------- #
# Використовуємо CSS-селектори через BeautifulSoup для кожного сайту
SCRAPING_CONFIG = {
    "pufetto": {
        "title": "#root > div:nth-child(4) > div > div.leftpart.mainleft > div.nm_bc > h1",
        "price": "#slidepanel > div:nth-of-type(3) > b"
    },
    "mebli-city": {
        "title": "#maincontent > div:nth-of-type(2) > div > div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1) > div:nth-of-type(2) > h1 > span",
        "price": "[id^='product-price-'] > span"
    },
    "fayni-mebli": {
        "title": "main header h1",          # більш загальний селектор для заголовку продукту
        "price": "main form dl dt span" 
    }
}

def detect_site(url: str):
    """
    Визначаємо, з якого сайту прийшов URL, і повертаємо відповідний ключ для налаштувань скрейпінгу.
    """
    if "pufetto.com.ua" in url:
        return "pufetto"
    elif "mebli-city.com.ua" in url:
        return "mebli-city"
    elif "fayni-mebli.com" in url:
        return "fayni-mebli"
    else:
        return None

# -------------------- Функція скрейпінгу (без lxml) -------------------- #
def scrape_custom(url: str, config: dict):
    """
    Скрейпимо сторінку за заданим URL, використовуючи кастомну конфігурацію.
    Для цього ми використовуємо requests та BeautifulSoup з CSS-селекторами.
    Повертаємо кортеж: (заголовок, текст ціни, час отримання даних).
    """
    try:
        import requests
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            return None, None, None

        content = response.content
        soup = BeautifulSoup(content, "html.parser")
        title_elem = soup.select_one(config["title"])
        title = title_elem.get_text(strip=True) if title_elem else "Заголовок не знайдено"

        price_elem = soup.select_one(config["price"])
        price_text = price_elem.get_text(strip=True) if price_elem else "Ціну не знайдено"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return title, price_text, timestamp

    except Exception as e:
        logging.error(f"Помилка скрейпінгу {url}: {e}")
        return None, None, None

def parse_price(price_text: str):
    """
    Обробляємо текст з ціною, залишаючи лише цифри та розділові знаки,
    і повертаємо число типу float.
    """
    price_text = re.sub(r"[^\d,\.]", "", price_text)
    price_text = price_text.replace(",", ".")
    try:
        return float(price_text)
    except ValueError:
        return None

# -------------------- Обробники команд бота -------------------- #
@dp.message(Command("start", "help"))
async def send_welcome(message: types.Message):
    welcome_text = (
        "Ласкаво просимо!\n\n"
        "Доступні команди:\n"
        "• /add <url> — Додати продукт для відстеження.\n"
        "• /delete <url> — Видалити продукт із відстеження.\n"
        "• /check [<url>] — Перевірити актуальні дані продукту (якщо URL не вказано, перевіряються всі відстежувані продукти).\n"
        "• /list — Показати всі продукти, які ви відстежуєте.\n"
        "• /settime <HH:MM> — Встановити час для щоденних оновлень.\n\n"
        "При кожному оновленні даних інформація та час зберігаються."
    )
    await message.reply(welcome_text)

@dp.message(Command("add"))
async def add_handler(message: types.Message):
    """
    Додаємо новий продукт до бази даних.
    Формат команди: /add <url>
    """
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Будь ласка, введіть команду у форматі: /add <url>")
        return

    url = parts[1].strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.reply("Переконайтеся, що URL починається з http:// або https://")
        return

    if get_tracked_product(message.from_user.id, url):
        await message.reply("Цей продукт уже відстежується.")
        return

    site = detect_site(url)
    if not site:
        await message.reply("❌ Невідомий сайт. Підтримуються: Pufetto, Mebli‑City, Файні меблі.")
        return

    config = SCRAPING_CONFIG[site]
    loop = asyncio.get_running_loop()
    title, price_text, timestamp = await loop.run_in_executor(None, scrape_custom, url, config)
    if title is None or price_text is None:
        await message.reply("Не вдалося отримати дані зі сторінки. Перевірте, будь ласка, посилання.")
        return

    price = parse_price(price_text)
    if price is None:
        await message.reply("Не вдалося визначити ціну.")
        return

    # Додаємо продукт з початковою зміною ціни 0%
    add_tracked_product(message.from_user.id, url, title, str(price), timestamp)
    await message.reply(
        f"Продукт додано!\nЗаголовок: {title}\nЦіна: {price} грн\nЗміна: {0:+.2f}%\nЧас: {timestamp}"
    )

@dp.message(Command("delete"))
async def delete_handler(message: types.Message):
    """
    Видаляємо продукт із відстеження.
    Формат команди: /delete <url>
    """
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Будь ласка, введіть команду у форматі: /delete <url>")
        return

    url = parts[1].strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.reply("Переконайтеся, що URL починається з http:// або https://")
        return

    if delete_tracked_product(message.from_user.id, url):
        await message.reply("Продукт успішно видалено з відстеження.")
    else:
        await message.reply("Не знайдено продукт у вашому списку відстеження.")

@dp.message(Command("check"))
async def check_handler(message: types.Message):
    """
    Перевіряємо актуальні дані продукту.
    Якщо ви вкажете URL, бот оновить інформацію для цього продукту.
    Якщо URL не задано, бот перевірить усі продукти, які ви відстежуєте.
    """
    parts = message.text.split(maxsplit=1)
    loop = asyncio.get_running_loop()

    # Якщо URL не вказано – беремо всі продукти користувача
    if len(parts) < 2:
        products = get_products_for_user(message.from_user.id)
        if not products:
            await message.reply("У вас немає відстежуваних продуктів. Додайте продукт командою /add <url>.")
            return

        results = []
        for prod in products:
            prod_id, url, old_title, old_price, old_timestamp = prod
            site = detect_site(url)
            if not site:
                results.append(f"URL: {url} - невідомий сайт.")
                continue

            config = SCRAPING_CONFIG[site]
            # Робимо асинхронний виклик функції скрейпінгу для даного продукту
            title, price_text, timestamp = await loop.run_in_executor(None, scrape_custom, url, config)
            if title is None or price_text is None:
                results.append(f"URL: {url} - не вдалося отримати дані.")
                continue

            price = parse_price(price_text)
            if price is None:
                results.append(f"URL: {url} - не вдалося визначити ціну.")
                continue

            old_price_val = parse_price(old_price)
            if old_price_val:
                price_change = ((price - old_price_val) / old_price_val) * 100
            else:
                price_change = 0

            # Оновлюємо інформацію про продукт у базі
            update_tracked_product(prod_id, title, str(price), timestamp)

            # Формуємо рядок з результатами для цього продукту
            results.append(
                f"URL: {url}\n"
                f"Заголовок: {title}\n"
                f"Ціна: {price} грн\n"
                f"Зміна: {price_change:+.2f}%\n"
                f"Оновлено: {timestamp}"
            )

        await message.reply("\n\n".join(results))

    # Якщо URL задано – працюємо тільки з цим продуктом
    else:
        url = parts[1].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await message.reply("Будь ласка, надайте коректну URL (починається з http:// або https://).")
            return

        product = get_tracked_product(message.from_user.id, url)
        if product is None:
            await message.reply("Продукт не знайдено у вашому списку відстеження. Спочатку додайте його через /add.")
            return

        site = detect_site(url)
        if not site:
            await message.reply("Невідомий сайт.")
            return

        config = SCRAPING_CONFIG[site]
        title, price_text, timestamp = await loop.run_in_executor(None, scrape_custom, url, config)
        if title is None or price_text is None:
            await message.reply("Не вдалося отримати дані зі сторінки. Перевірте, будь ласка, посилання.")
            return

        price = parse_price(price_text)
        if price is None:
            await message.reply("Не вдалося визначити ціну.")
            return

        old_price_val = parse_price(product[2])
        if old_price_val:
            price_change = ((price - old_price_val) / old_price_val) * 100
        else:
            price_change = 0

        update_tracked_product(product[0], title, str(price), timestamp)
        await message.reply(
            f"Актуальні дані:\n"
            f"URL: {url}\n"
            f"Заголовок: {title}\n"
            f"Ціна: {price} грн\n"
            f"Зміна: {price_change:+.2f}%\n"
            f"Оновлено: {timestamp}"
        )


@dp.message(Command("list"))
async def list_handler(message: types.Message):
    """Виводимо список усіх продуктів, які ви відстежуєте."""
    user_id = message.from_user.id
    products = get_products_for_user(user_id)
    if not products:
        await message.reply("У вас немає відстежуваних продуктів.")
        return

    response = "Ваші відстежувані продукти:\n\n"
    for prod in products:
        response += (
            f"ID: {prod[0]}\n"
            f"Заголовок: {prod[2]}\n"
            f"Ціна: {prod[3]} грн\n"
            f"URL: {prod[1]}\n"
            f"Оновлено: {prod[4]}\n\n"
        )
    await message.reply(response)

@dp.message(Command("settime"))
async def settime_handler(message: types.Message):
    """
    Встановлюємо бажаний час для щоденних оновлень.
    Формат команди: /settime <HH:MM> (наприклад, /settime 10:00)
    """
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Будь ласка, введіть команду у форматі: /settime <HH:MM>\nНаприклад: /settime 10:00")
        return

    time_str = parts[1].strip()
    try:
        hour, minute = map(int, time_str.split(":"))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError
        update_time = f"{hour:02d}:{minute:02d}"
    except Exception:
        await message.reply("Невірний формат часу. Використовуйте формат HH:MM (наприклад, 10:00)")
        return

    set_user_update_time(message.from_user.id, update_time)
    await message.reply(f"Ваш час оновлень встановлено на {update_time} за київським часом.")

# -------------------- Функції для відправки оновлень користувачу -------------------- #
async def send_updates_for_user(user_id: int):
    """
    Відправляємо оновлення по всіх продуктах, які відстежує користувач.
    Для кожного продукту робимо новий скрейпінг, оновлюємо базу і відправляємо повідомлення.
    """
    products = get_products_for_user(user_id)
    loop = asyncio.get_running_loop()
    for prod in products:
        prod_id, url, old_title, old_price, old_timestamp = prod
        site = detect_site(url)
        if not site:
            continue
        config = SCRAPING_CONFIG[site]
        title, price_text, timestamp = await loop.run_in_executor(None, scrape_custom, url, config)
        if title is None or price_text is None:
            continue
        new_price = parse_price(price_text)
        if new_price is None:
            continue
        update_tracked_product(prod_id, title, str(new_price), timestamp)
        old_price_val = parse_price(old_price)
        if old_price_val:
            price_change = ((new_price - old_price_val) / old_price_val) * 100
        else:
            price_change = 0
        message_text = (
            f"Щоденне оновлення:\n"
            f"URL: {url}\n"
            f"Заголовок: {title}\n"
            f"Ціна: {new_price} грн\n"
            f"Зміна: {price_change:+.2f}%\n"
            f"Оновлено: {timestamp}"
        )
        try:
            await bot.send_message(user_id, message_text)
        except Exception as e:
            logging.error(f"Не вдалося надіслати оновлення користувачу {user_id}: {e}")

# -------------------- Фоновий планувальник оновлень для користувачів -------------------- #
async def user_update_scheduler():
    """
    Фонове завдання, яке кожну хвилину перевіряє, чи настав час відправити оновлення
    для кожного користувача згідно з його встановленим часом (збереженим у user_settings).
    Якщо TESTING=True, оновлення можуть надсилатися частіше.
    """
    logging.info("Фоновий планувальник користувацьких оновлень запущено.")
    while True:
        await asyncio.sleep(60)  # Перевіряємо кожну хвилину
        now_str = datetime.now(ZoneInfo("Europe/Kiev")).strftime("%H:%M")
        for user_id, update_time in get_all_user_settings():
            # Якщо тестовий режим або час співпадає — надсилаємо оновлення
            if TESTING or now_str == update_time:
                logging.info(f"Надсилання оновлень користувачу {user_id} о {now_str}.")
                await send_updates_for_user(user_id)

@dp.message(Command("test_update"))
async def test_update(message: types.Message):
    logging.info("Користувач викликав ручне оновлення.")
    await send_updates_for_user(message.from_user.id)
    await message.reply("Оновлення виконано.")

# -------------------- Головна функція -------------------- #
async def main():
    logging.info("Головна функція запущена. Чекаємо команд від користувачів...")
    # Запускаємо фоновий планувальник оновлень для користувачів
    asyncio.create_task(user_update_scheduler())
    await dp.start_polling(bot)

if __name__ == '__main__':
    logging.info("Запускаємо бота...")
    asyncio.run(main())
