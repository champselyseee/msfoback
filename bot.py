import asyncio
import os
from datetime import datetime
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from telegram.ext import Application, CommandHandler, ContextTypes
import sqlite3

# ==================== КОНФИГ ====================
url = "https://www.e-disclosure.ru/portal/files.aspx?id=38533&type=3"
BASE_URL = "https://msfo.valiullin.uk"
DB_FILE = "reports.db"
CHECK_INTERVAL = 3600  # 1 час

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8000))

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Таблица отчётов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            file_id TEXT PRIMARY KEY,
            report_num TEXT,
            doc_type TEXT,
            period TEXT,
            foundation_date TEXT,
            publish_date TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    
    # Таблица подписчиков
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            subscribed_at TEXT
        )
    """)
    
    conn.commit()
    return conn


def get_known_ids(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT file_id FROM reports")
    return {row[0] for row in cursor.fetchall()}


def save_new_reports(conn, new_reports):
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for report in new_reports:
        cursor.execute("""
            INSERT OR IGNORE INTO reports 
            (file_id, report_num, doc_type, period, foundation_date, publish_date, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report['file_id'], report['num'], report['doc_type'],
            report['period'], report['foundation'], report['publish'],
            now, now
        ))
    
    conn.commit()


def update_last_seen(conn, file_ids):
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for file_id in file_ids:
        cursor.execute(
            "UPDATE reports SET last_seen = ? WHERE file_id = ?",
            (now, file_id)
        )
    
    conn.commit()


def get_subscribers(conn):
    """Возвращает список chat_id подписчиков"""
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM subscribers")
    return [row[0] for row in cursor.fetchall()]


def subscribe_user(conn, chat_id, username):
    """Подписывает пользователя на уведомления"""
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT OR REPLACE INTO subscribers (chat_id, username, subscribed_at)
        VALUES (?, ?, ?)
    """, (str(chat_id), username or "unknown", now))
    conn.commit()


def unsubscribe_user(conn, chat_id):
    """Отписывает пользователя"""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscribers WHERE chat_id = ?", (str(chat_id),))
    conn.commit()


# ==================== ПАРСЕР ====================
async def parse_reports():
    """Парсит отчёты"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            await page.wait_for_selector("table tbody tr", timeout=20000)
            
            rows = await page.locator("table tbody tr").all()
            reports = []
            
            for row in rows:
                try:
                    cells = await row.locator("td").all()
                    
                    if len(cells) < 6:
                        continue
                    
                    report_num = (await cells[0].inner_text()).strip()
                    doc_type = (await cells[1].inner_text()).strip()
                    period = (await cells[2].inner_text()).strip()
                    foundation_date = (await cells[3].inner_text()).strip()
                    publish_date = (await cells[4].inner_text()).strip()
                    
                    if "бухгалтер" not in doc_type.lower():
                        continue
                    
                    link = cells[5].locator("a")
                    if await link.count() == 0:
                        continue
                    
                    href = await link.first.get_attribute("href")
                    if not href:
                        continue
                    
                    file_url = urljoin(url, href)
                    if "Fileid=" not in file_url:
                        continue
                    
                    file_id = file_url.split("Fileid=")[1]
                    
                    reports.append({
                        "num": report_num,
                        "doc_type": doc_type,
                        "period": period,
                        "foundation": foundation_date,
                        "publish": publish_date,
                        "file_id": file_id
                    })
                    
                except Exception as e:
                    print(f"Ошибка парсинга строки: {e}")
            
            return reports
            
        finally:
            await browser.close()


# ==================== УВЕДОМЛЕНИЯ ВСЕМ ПОДПИСЧИКАМ ====================
async def check_and_notify(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача: проверяет отчёты и уведомляет всех подписчиков"""
    conn = init_db()
    
    try:
        print(f"[{datetime.now()}] Проверяю отчёты...")
        reports = await parse_reports()
        
        if not reports:
            print("Отчёты не найдены")
            return
        
        known_ids = get_known_ids(conn)
        new_reports = [r for r in reports if r['file_id'] not in known_ids]
        
        # Обновляем last_seen
        all_file_ids = {r['file_id'] for r in reports}
        update_last_seen(conn, all_file_ids)
        
        if new_reports:
            save_new_reports(conn, new_reports)
            
            # Формируем сообщение
            message = f"🔥 <b>Новые отчёты ({len(new_reports)}):</b>\n\n"
            
            for i, report in enumerate(new_reports, start=1):
                link = f"{BASE_URL}/view/{report['file_id']}"
                message += (
                    f"{i}. <b>{report['period']}</b>\n"
                    f"   📄 {report['doc_type']}\n"
                    f"   📅 Опубликован: {report['publish']}\n"
                    f"   🔗 <a href='{link}'>Открыть отчёт</a>\n\n"
                )
            
            # Отправляем всем подписчикам
            subscribers = get_subscribers(conn)
            sent_count = 0
            
            for chat_id in subscribers:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )
                    sent_count += 1
                except Exception as e:
                    print(f"Не удалось отправить пользователю {chat_id}: {e}")
                    # Если пользователь заблокировал бота — удаляем из подписчиков
                    if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                        unsubscribe_user(conn, chat_id)
            
            print(f"Отправлено уведомлений: {sent_count}/{len(subscribers)}")
        else:
            print("Новых отчётов нет")
            
    except Exception as e:
        print(f"Ошибка при проверке: {e}")
    finally:
        conn.close()


# ==================== КОМАНДЫ БОТА ====================
async def start_command(update, context):
    """Команда /start"""
    await update.message.reply_text(
        "👋 Привет! Я бот-парсер бухгалтерских отчётов.\n\n"
        "📖 <b>Команды:</b>\n"
        "/list — показать все отчёты\n"
        "/find &lt;год&gt; — найти отчёты за год\n"
        "/check — проверить прямо сейчас\n"
        "/subscribe — подписаться на уведомления\n"
        "/unsubscribe — отписаться\n"
        "/stats — статистика\n"
        "/help — помощь\n\n"
        "🔔 Чтобы получать уведомления о новых отчётах — введи /subscribe",
        parse_mode="HTML"
    )


async def list_command(update, context):
    """Показывает все отчёты (пагинация по 10 штук)"""
    conn = init_db()
    
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT period, doc_type, publish_date, file_id FROM reports ORDER BY first_seen DESC"
        )
        all_reports = cursor.fetchall()
        
        if not all_reports:
            await update.message.reply_text("📭 В базе пока нет отчётов. Ждём первой проверки...")
            return
        
        # Пагинация: показываем по 10 отчётов
        page = 0
        if context.args:
            try:
                page = int(context.args[0]) - 1
            except ValueError:
                pass
        
        per_page = 10
        total_pages = (len(all_reports) + per_page - 1) // per_page
        start = page * per_page
        end = start + per_page
        reports = all_reports[start:end]
        
        message = f"📚 <b>Всего отчётов: {len(all_reports)}</b> (стр. {page + 1}/{total_pages})\n\n"
        
        for i, (period, doc_type, publish_date, file_id) in enumerate(reports, start=start + 1):
            link = f"{BASE_URL}/view/{file_id}"
            message += f"{i}. <b>{period}</b> | {doc_type}\n   📅 {publish_date} | <a href='{link}'>Открыть</a>\n"
        
        if total_pages > 1:
            message += f"\n/list {page + 2} — следующая страница"
        
        await update.message.reply_text(
            message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        
    finally:
        conn.close()


async def check_command(update, context):
    """Принудительная проверка"""
    msg = await update.message.reply_text("🔍 Запускаю проверку...")
    await check_and_notify(context)
    await msg.edit_text("✅ Проверка завершена! Если были новые отчёты — уведомления уже отправлены.")


async def subscribe_command(update, context):
    """Подписка на уведомления"""
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name
    
    conn = init_db()
    try:
        subscribe_user(conn, chat_id, username)
        await update.message.reply_text(
            "✅ Ты подписан на уведомления о новых отчётах!\n"
            "Я буду присылать их автоматически каждый час.\n\n"
            "Отписаться: /unsubscribe"
        )
    finally:
        conn.close()


async def unsubscribe_command(update, context):
    """Отписка от уведомлений"""
    chat_id = update.effective_chat.id
    
    conn = init_db()
    try:
        unsubscribe_user(conn, chat_id)
        await update.message.reply_text(
            "❌ Ты отписан от уведомлений.\n"
            "Подписаться снова: /subscribe"
        )
    finally:
        conn.close()


async def find_command(update, context):
    """Поиск по году"""
    if not context.args:
        await update.message.reply_text("Укажи год. Пример: /find 2024")
        return
    
    year = context.args[0]
    conn = init_db()
    
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT period, doc_type, publish_date, file_id FROM reports WHERE period LIKE ? ORDER BY first_seen DESC",
            (f"%{year}%",)
        )
        reports = cursor.fetchall()
        
        if not reports:
            await update.message.reply_text(f"📭 Нет отчётов за {year} год.")
            return
        
        message = f"📚 <b>Отчёты за {year} год: {len(reports)}</b>\n\n"
        
        for i, (period, doc_type, publish_date, file_id) in enumerate(reports, start=1):
            link = f"{BASE_URL}/view/{file_id}"
            message += f"{i}. <b>{period}</b> | {doc_type}\n   📅 {publish_date} | <a href='{link}'>Открыть</a>\n"
        
        await update.message.reply_text(
            message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        
    finally:
        conn.close()


async def stats_command(update, context):
    """Статистика"""
    conn = init_db()
    
    try:
        cursor = conn.cursor()
        
        # Отчёты
        cursor.execute("SELECT COUNT(*) FROM reports")
        total_reports = cursor.fetchone()[0]
        
        cursor.execute("SELECT MIN(first_seen), MAX(first_seen) FROM reports")
        oldest, newest = cursor.fetchone()
        
        cursor.execute(
            "SELECT SUBSTR(period, 1, 4) as year, COUNT(*) FROM reports GROUP BY year ORDER BY year DESC"
        )
        by_year = cursor.fetchall()
        
        # Подписчики
        cursor.execute("SELECT COUNT(*) FROM subscribers")
        total_subscribers = cursor.fetchone()[0]
        
        message = (
            f"📊 <b>Статистика:</b>\n\n"
            f"📄 Всего отчётов: <b>{total_reports}</b>\n"
            f"👥 Подписчиков: <b>{total_subscribers}</b>\n"
            f"📅 Первый отчёт: {oldest}\n"
            f"📅 Последний: {newest}\n\n"
            f"<b>По годам:</b>\n"
        )
        
        for year, count in by_year:
            message += f"• {year}: {count}\n"
        
        await update.message.reply_text(message, parse_mode="HTML")
        
    finally:
        conn.close()


async def help_command(update, context):
    """Помощь"""
    await update.message.reply_text(
        "📖 <b>Все команды:</b>\n\n"
        "/start — приветствие\n"
        "/list [страница] — все отчёты\n"
        "/find &lt;год&gt; — поиск по году\n"
        "/check — проверить сейчас\n"
        "/subscribe — подписаться на уведомления\n"
        "/unsubscribe — отписаться\n"
        "/stats — статистика\n"
        "/help — это сообщение\n\n"
        "🤖 Бот автоматически проверяет новые отчёты каждый час "
        "и уведомляет всех подписчиков.",
        parse_mode="HTML"
    )


# ==================== ЗАПУСК ====================
async def post_init(application: Application):
    """Запускается после старта бота"""
    # Фоновая проверка каждый час
    application.job_queue.run_repeating(
        check_and_notify,
        interval=CHECK_INTERVAL,
        first=10
    )
    print("Фоновая проверка запущена")


async def main():
    if not TOKEN:
        print("ОШИБКА: BOT_TOKEN не задан!")
        return
    
    # Инициализируем базу
    init_db()
    
    # Бот
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    
    # Команды
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # Веб-сервер для Railway (health check)
    from aiohttp import web
    
    async def health_check(request):
        return web.Response(text="OK")
    
    app = web.Application()
    app.router.add_get("/", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health-check на порту {PORT}")
    
    # Поехали!
    print("Бот запущен!")
    await application.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
