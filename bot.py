import asyncio
import ssl
import sqlite3
from urllib.request import urlopen
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Потребує Python 3.9+

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import logging
from bs4 import BeautifulSoup

# -------------------- Конфігурація -------------------- #
BOT_TOKEN = "TOKEN"  # Замініть на ваш актуальний токен
DB_PATH = "products.db"

# TESTING = True – для тестування (оновлення кожну хвилину)
# TESTING = False – для продуктивного середовища (оновлення щодня о 10:00 за київським часом)
TESTING = True

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# -------------------- Функції роботи з базою даних -------------------- #
def init_db():
    """Створює базу даних та таблицю tracked_products, якщо їх ще немає."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
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
    conn.commit()
    conn.close()

def add_tracked_product(user_id: int, url: str, title: str, price: str, timestamp: str):
    """Додає новий запис відслідковуваного продукту."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO tracked_products (user_id, url, title, price, last_scrape)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, url, title, price, timestamp))
    conn.commit()
    conn.close()

def update_tracked_product(product_id: int, title: str, price: str, timestamp: str):
    """Оновлює дані запису відслідковуваного продукту."""
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
    """Видаляє запис з бази за користувачем та URL. Повертає True, якщо запис видалено."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tracked_products WHERE user_id = ? AND url = ?", (user_id, url))
    conn.commit()
    changes = c.rowcount
    conn.close()
    return changes > 0

def get_all_tracked_products():
    """Повертає всі записи відслідковуваних продуктів."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, user_id, url, title, price, last_scrape FROM tracked_products")
    rows = c.fetchall()
    conn.close()
    return rows

def get_tracked_product(user_id: int, url: str):
    """Повертає запис продукту для заданого користувача та URL або None, якщо не знайдено."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, price, last_scrape FROM tracked_products WHERE user_id = ? AND url = ?", (user_id, url))
    product = c.fetchone()
    conn.close()
    return product

# Ініціалізація бази даних
init_db()

# -------------------- Функція скрейпінгу -------------------- #
def scrape_product(url: str):
    """
    Скрейпить задану URL-адресу для:
      - Заголовку за селектором:
        "#root > div:nth-child(4) > div > div.leftpart.mainleft > div.nm_bc > h1"
      - Ціни за селектором (аналог XPath: //*[@id="slidepanel"]/div[3]/b/text()):
        "#slidepanel > div:nth-child(3) > b"
    Повертає кортеж: (title, price, timestamp) або (None, None, None) при помилці.
    """
    try:
        # Вимикаємо перевірку SSL-сертифіката (лише для тестування!)
        ssl._create_default_https_context = ssl._create_unverified_context

        response = urlopen(url)
        html = response.read().decode("utf-8")
        soup = BeautifulSoup(html, "html.parser")

        # Отримуємо заголовок
        title_elem = soup.select_one("#root > div:nth-child(4) > div > div.leftpart.mainleft > div.nm_bc > h1")
        title = title_elem.get_text(strip=True) if title_elem else "Заголовок не знайдено"

        # Отримуємо ціну
        price_elem = soup.select_one("#slidepanel > div:nth-child(3) > b")
        price = price_elem.get_text(strip=True) if price_elem else "Ціну не знайдено"

        # Отримуємо поточний час
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return title, price, timestamp
    except Exception as e:
        logging.error(f"Помилка скрейпінгу {url}: {e}")
        return None, None, None

# -------------------- Обробники команд бота -------------------- #
@dp.message(Command("start", "help"))
async def send_welcome(message: types.Message):
    welcome_text = (
        "Ласкаво просимо!\n\n"
        "Команди:\n"
        "• /add <url> — Додати продукт для відслідковування.\n"
        "• /delete <url> — Видалити продукт із відслідковування.\n"
        "• /check <url> — Перевірити поточні дані продукту (оновлює запис).\n\n"
        "При кожному скрейпінгу дані та час оновлюються."
    )
    await message.reply(welcome_text)

@dp.message(Command("add"))
async def add_handler(message: types.Message):
    """
    Додає новий продукт до бази.
    Приклад використання:
      /add https://www.example.com/product-page
    """
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Використання: /add <url>")
        return

    url = parts[1].strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.reply("Будь ласка, надайте коректну URL (починається з http:// або https://).")
        return

    # Перевірка чи вже існує запис для даного користувача і URL
    existing = get_tracked_product(message.from_user.id, url)
    if existing is not None:
        await message.reply("Цей продукт уже відслідковується.")
        return

    loop = asyncio.get_running_loop()
    title, price, timestamp = await loop.run_in_executor(None, scrape_product, url)
    if title is None:
        await message.reply("Не вдалося скрейпити URL. Перевірте посилання та спробуйте знову.")
        return

    add_tracked_product(message.from_user.id, url, title, price, timestamp)
    await message.reply(f"Продукт додано:\nЗаголовок: {title}\nЦіна: {price}\nЧас: {timestamp}")

@dp.message(Command("delete"))
async def delete_handler(message: types.Message):
    """
    Видаляє продукт із відслідковування.
    Використання: /delete <url>
    """
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Використання: /delete <url>")
        return

    url = parts[1].strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.reply("Будь ласка, надайте коректну URL (починається з http:// або https://).")
        return

    success = delete_tracked_product(message.from_user.id, url)
    if success:
        await message.reply("Продукт видалено з відслідковування.")
    else:
        await message.reply("Продукт не знайдено у вашому списку відслідковування.")

@dp.message(Command("check"))
async def check_handler(message: types.Message):
    """
    Перевіряє поточні дані продукту.
    Використання: /check <url>
    Скрейпить URL, оновлює запис у базі та повертає актуальні дані.
    """
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Використання: /check <url>")
        return

    url = parts[1].strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.reply("Будь ласка, надайте коректну URL (починається з http:// або https://).")
        return

    product = get_tracked_product(message.from_user.id, url)
    if product is None:
        await message.reply("Продукт не знайдено у вашому списку відслідковування. Спочатку додайте його через /add.")
        return

    loop = asyncio.get_running_loop()
    title, price, timestamp = await loop.run_in_executor(None, scrape_product, url)
    if title is None:
        await message.reply("Не вдалося скрейпити URL. Перевірте посилання та спробуйте знову.")
        return

    # Оновлюємо запис у базі з актуальними даними
    prod_id = product[0]
    update_tracked_product(prod_id, title, price, timestamp)
    message_text = (
        f"Актуальні дані:\n"
        f"URL: {url}\n"
        f"Заголовок: {title}\n"
        f"Ціна: {price}\n"
        f"Оновлено: {timestamp}"
    )
    await message.reply(message_text)

# -------------------- Функція для щоденного/тестового оновлення -------------------- #
async def scheduled_job():
    """
    Скрейпить усі відслідковувані продукти, оновлює записи в базі та надсилає повідомлення користувачам.
    """
    logging.info("Запуск оновлення для всіх відслідковуваних продуктів...")
    products = get_all_tracked_products()
    loop = asyncio.get_running_loop()

    for prod in products:
        prod_id, user_id, url, old_title, old_price, old_timestamp = prod
        title, price, timestamp = await loop.run_in_executor(None, scrape_product, url)
        if title is None:
            continue  # пропускаємо, якщо скрейпінг не вдався

        update_tracked_product(prod_id, title, price, timestamp)
        message_text = (
            f"Щоденне оновлення:\n"
            f"URL: {url}\n"
            f"Заголовок: {title}\n"
            f"Ціна: {price}\n"
            f"Оновлено: {timestamp}"
        )
        try:
            await bot.send_message(user_id, message_text)
        except Exception as e:
            logging.error(f"Не вдалося надіслати оновлення користувачу {user_id}: {e}")

# -------------------- Функція планувальника за допомогою asyncio -------------------- #
async def daily_update_scheduler():
    """
    Фонове завдання, яке у циклі очікує потрібний інтервал і запускає scheduled_job.
    """
    logging.info("Фоновий планувальник запущено.")
    while True:
        if TESTING:
            wait_seconds = 60  # для тестування – кожна хвилина
            logging.info(f"Тестовий режим: очікування {wait_seconds} секунд...")
        else:
            now = datetime.now(ZoneInfo("Europe/Kiev"))
            target_time = now.replace(hour=10, minute=0, second=0, microsecond=0)
            if now >= target_time:
                target_time += timedelta(days=1)
            wait_seconds = (target_time - now).total_seconds()
            logging.info(f"Очікування {wait_seconds:.0f} секунд до 10:00...")

        await asyncio.sleep(wait_seconds)
        logging.info("Запуск scheduled_job()...")
        await scheduled_job()


@dp.message(Command("test_update"))
async def test_update(message: types.Message):
    logging.info("Користувач викликав ручне оновлення.")
    await scheduled_job()
    await message.reply("Оновлення виконано.")

# -------------------- on_startup -------------------- #
async def on_startup(_):
    logging.info("on_startup: Функція викликана, запуск планувальника...")
    asyncio.create_task(daily_update_scheduler())

# -------------------- Головна функція -------------------- #
async def main():
    logging.info("Головна функція запущена. Очікування команд...")
    asyncio.create_task(daily_update_scheduler())
    await dp.start_polling(bot, on_startup=on_startup)

if __name__ == '__main__':
    logging.info("Запуск бота...")
    asyncio.run(main())

