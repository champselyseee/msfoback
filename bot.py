import asyncio
import os
import json
import logging
import time
import random
from datetime import datetime
from urllib.parse import urljoin
from typing import List, Dict, Optional, Tuple, Set
from difflib import SequenceMatcher
from contextlib import contextmanager
from functools import lru_cache

from playwright.async_api import async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    ContextTypes,
    MessageHandler,
    filters
)
from aiohttp import web
import sqlite3

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГ ====================
class Config:
    BASE_URL = "https://msfo.valiullin.uk"
    DB_FILE = "reports.db"
    CHECK_INTERVAL = 3600  # 1 час
    COMPANIES_FILE = "russian_stocks_ids.json"
    MAX_RETRIES = 3
    RETRY_DELAY = 5
    TIMEOUT = 120000
    CACHE_TTL = 300  # 5 минут кэширования
    SEARCH_TIMEOUT = 300  # 5 минут на выбор компании
    MAX_MESSAGE_LENGTH = 4000  # Лимит Telegram
    RATE_LIMIT_DELAY = (5, 8)  # Задержка между компаниями при проверке
    
    TOKEN = os.getenv("BOT_TOKEN")
    PORT = int(os.getenv("PORT", 8080))

# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================
COMPANIES: List[Dict] = []
BY_TICKER: Dict[str, Dict] = {}
BY_NAME: Dict[str, Dict] = {}

# Кэш и состояние
parse_cache: Dict[str, Tuple[float, List[Dict]]] = {}
search_results: Dict[str, Dict] = {}  # chat_id -> {"companies": [...], "timestamp": time.time()}

# ==================== ЗАГРУЗКА КОМПАНИЙ ====================
def load_companies() -> Tuple[List[Dict], Dict[str, Dict], Dict[str, Dict]]:
    """Загружает компании из JSON и строит индексы"""
    global BY_TICKER, BY_NAME
    
    try:
        with open(Config.COMPANIES_FILE, "r", encoding="utf-8") as f:
            companies = json.load(f)
        
        if not isinstance(companies, list):
            raise ValueError("JSON должен содержать массив компаний")
        
        by_ticker = {}
        by_name = {}
        valid_companies = []
        
        for company in companies:
            if not isinstance(company, dict) or 'id' not in company:
                logger.warning(f"Пропущена некорректная запись: {company}")
                continue
            
            # Проверяем обязательные поля
            company_id = company.get('id')
            if not company_id:
                continue
                
            ticker = company.get("ticker", "").upper().strip()
            name = company.get("name", "").strip()
            
            if not name:
                logger.warning(f"Компания с ID {company_id} не имеет названия")
                continue
            
            valid_companies.append(company)
            
            if ticker:
                by_ticker[ticker] = company
            if name:
                by_name[name.lower()] = company
        
        BY_TICKER = by_ticker
        BY_NAME = by_name
        
        logger.info(f"Загружено {len(valid_companies)} компаний")
        return valid_companies, by_ticker, by_name
        
    except FileNotFoundError:
        logger.error(f"Файл {Config.COMPANIES_FILE} не найден")
        return [], {}, {}
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON: {e}")
        return [], {}, {}
    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке компаний: {e}")
        return [], {}, {}


def search_companies(query: str) -> List[Dict]:
    """Поиск компаний по тикеру или названию с частичным совпадением"""
    if not query or not query.strip():
        return []
    
    query = query.strip().upper()
    results = []
    seen_ids = set()
    
    # Точное совпадение по тикеру
    if query in BY_TICKER:
        company = BY_TICKER[query]
        results.append(company)
        seen_ids.add(company['id'])
    
    # Частичное совпадение по тикеру
    for ticker, company in BY_TICKER.items():
        if company['id'] not in seen_ids and query in ticker:
            results.append(company)
            seen_ids.add(company['id'])
    
    # Поиск по названию
    query_lower = query.lower()
    for name, company in BY_NAME.items():
        if company['id'] not in seen_ids and query_lower in name:
            results.append(company)
            seen_ids.add(company['id'])
    
    # Сортировка по релевантности
    def relevance(company: Dict) -> float:
        name = company.get("name", "").lower()
        ticker = company.get("ticker", "").lower()
        name_score = SequenceMatcher(None, query_lower, name).ratio()
        ticker_score = SequenceMatcher(None, query_lower, ticker).ratio()
        return max(name_score, ticker_score)
    
    results.sort(key=relevance, reverse=True)
    return results


def get_company_by_id(company_id: int) -> Optional[Dict]:
    """Находит компанию по ID"""
    for company in COMPANIES:
        if company["id"] == company_id:
            return company
    return None


def format_company_info(company: Dict) -> str:
    """Форматирует информацию о компании"""
    name = company.get('name', 'Н/Д')
    ticker = company.get('ticker', '')
    if ticker:
        return f"<b>{name}</b> ({ticker})"
    return f"<b>{name}</b>"


# ==================== РАБОТА С БАЗОЙ ДАННЫХ ====================
@contextmanager
def get_db():
    """Контекстный менеджер для работы с БД"""
    conn = sqlite3.connect(Config.DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка БД: {e}")
        raise
    finally:
        conn.close()


def init_db():
    """Инициализация базы данных"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Основная таблица отчетов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                file_id TEXT,
                company_id INTEGER,
                report_num TEXT,
                doc_type TEXT,
                period TEXT,
                foundation_date TEXT,
                publish_date TEXT,
                first_seen TEXT,
                last_seen TEXT,
                PRIMARY KEY (file_id, company_id)
            )
        """)
        
        # Таблица подписок
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id TEXT,
                company_id INTEGER,
                subscribed_at TEXT,
                PRIMARY KEY (chat_id, company_id)
            )
        """)
        
        # Таблица пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                username TEXT,
                first_seen TEXT,
                last_active TEXT
            )
        """)
        
        # Индексы для ускорения запросов
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_company 
            ON reports(company_id)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_chat 
            ON subscriptions(chat_id)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_company 
            ON subscriptions(company_id)
        """)
        
        conn.commit()
        logger.info("База данных инициализирована")


def get_known_ids(conn, company_id: int) -> Set[str]:
    """Получает известные file_id для компании"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT file_id FROM reports WHERE company_id = ?", 
        (company_id,)
    )
    return {row[0] for row in cursor.fetchall()}


def save_new_reports(conn, new_reports: List[Dict], company_id: int):
    """Сохраняет новые отчеты"""
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for report in new_reports:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO reports 
                (file_id, company_id, report_num, doc_type, period, 
                 foundation_date, publish_date, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                report['file_id'], company_id, report['num'], 
                report['doc_type'], report['period'], 
                report['foundation'], report['publish'],
                now, now
            ))
        except Exception as e:
            logger.error(f"Ошибка сохранения отчета: {e}")
    
    conn.commit()


def update_last_seen(conn, file_ids: Set[str], company_id: int):
    """Обновляет время последнего обнаружения"""
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for file_id in file_ids:
        cursor.execute(
            "UPDATE reports SET last_seen = ? WHERE file_id = ? AND company_id = ?",
            (now, file_id, company_id)
        )
    
    conn.commit()


def get_subscribers_for_company(conn, company_id: int) -> List[str]:
    """Получает всех подписчиков конкретной компании"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT chat_id FROM subscriptions WHERE company_id = ?", 
        (company_id,)
    )
    return [row[0] for row in cursor.fetchall()]


def get_user_subscriptions(conn, chat_id: str) -> List[int]:
    """Получает все подписки пользователя"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT company_id FROM subscriptions WHERE chat_id = ?", 
        (chat_id,)
    )
    return [row[0] for row in cursor.fetchall()]


def subscribe_user_to_company(conn, chat_id: str, company_id: int):
    """Подписывает пользователя на компанию"""
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT OR REPLACE INTO subscriptions (chat_id, company_id, subscribed_at)
        VALUES (?, ?, ?)
    """, (chat_id, company_id, now))
    conn.commit()
    logger.info(f"Пользователь {chat_id} подписан на компанию {company_id}")


def unsubscribe_user_from_company(conn, chat_id: str, company_id: int):
    """Отписывает пользователя от компании"""
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM subscriptions WHERE chat_id = ? AND company_id = ?",
        (chat_id, company_id)
    )
    conn.commit()
    logger.info(f"Пользователь {chat_id} отписан от компании {company_id}")


def update_user_activity(conn, chat_id: str, username: str):
    """Обновляет активность пользователя"""
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO users (chat_id, username, first_seen, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET 
            username = excluded.username,
            last_active = excluded.last_active
    """, (chat_id, username, now, now))
    conn.commit()


def cleanup_search_state():
    """Очищает устаревшие состояния поиска"""
    current_time = time.time()
    expired_chats = [
        chat_id for chat_id, data in search_results.items()
        if current_time - data.get('timestamp', 0) > Config.SEARCH_TIMEOUT
    ]
    for chat_id in expired_chats:
        del search_results[chat_id]
    if expired_chats:
        logger.debug(f"Очищено {len(expired_chats)} устаревших состояний поиска")


# ==================== ПАРСЕР ====================
async def parse_reports(company_id: int) -> List[Dict]:
    """Парсит отчеты для конкретной компании"""
    if not get_company_by_id(company_id):
        logger.warning(f"Компания с ID {company_id} не найдена")
        return []
    
    url = f"https://www.e-disclosure.ru/portal/files.aspx?id={company_id}&type=3"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU"
        )
        page = await context.new_page()
        
        try:
            # Попытки загрузить страницу
            for attempt in range(Config.MAX_RETRIES):
                try:
                    logger.debug(f"Попытка {attempt + 1} загрузить компанию {company_id}")
                    await page.goto(url, wait_until="networkidle", timeout=Config.TIMEOUT)
                    await page.wait_for_timeout(5000)
                    
                    await page.wait_for_selector("table tbody tr", timeout=30000)
                    logger.debug(f"Таблица найдена для компании {company_id}")
                    break
                    
                except Exception as e:
                    logger.warning(f"Попытка {attempt + 1} для компании {company_id} не удалась: {e}")
                    if attempt == Config.MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(Config.RETRY_DELAY)
            
            rows = await page.locator("table tbody tr").all()
            logger.info(f"Найдено строк для компании {company_id}: {len(rows)}")
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
                    
                    # Фильтруем только бухгалтерские отчеты
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
                    logger.error(f"Ошибка парсинга строки для компании {company_id}: {e}")
            
            logger.info(f"Найдено отчетов для компании {company_id}: {len(reports)}")
            return reports
            
        finally:
            try:
                await context.close()
            except Exception:
                pass
            await browser.close()


async def parse_reports_cached(company_id: int) -> List[Dict]:
    """Кэширует результаты парсинга"""
    cache_key = f"company_{company_id}"
    
    # Проверяем кэш
    if cache_key in parse_cache:
        cached_time, cached_data = parse_cache[cache_key]
        if time.time() - cached_time < Config.CACHE_TTL:
            logger.debug(f"Использован кэш для компании {company_id}")
            return cached_data
    
    # Парсим
    data = await parse_reports(company_id)
    parse_cache[cache_key] = (time.time(), data)
    return data


# ==================== УВЕДОМЛЕНИЯ ПОДПИСЧИКАМ ====================
async def check_and_notify(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая проверка для всех компаний с подписками"""
    logger.info("Запуск фоновой проверки")
    
    with get_db() as conn:
        try:
            # Получаем все уникальные company_id из подписок
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT company_id FROM subscriptions")
            company_ids = [row[0] for row in cursor.fetchall()]
            
            if not company_ids:
                logger.info("Нет активных подписок")
                return
            
            logger.info(f"Проверяю {len(company_ids)} компаний...")
            
            for i, company_id in enumerate(company_ids):
                try:
                    company = get_company_by_id(company_id)
                    if not company:
                        logger.warning(f"Компания с ID {company_id} не найдена")
                        continue
                    
                    logger.info(f"Проверяю {company.get('name', 'Н/Д')} ({i+1}/{len(company_ids)})")
                    reports = await parse_reports(company_id)
                    
                    if not reports:
                        logger.info(f"Нет отчетов для {company.get('name', 'Н/Д')}")
                        continue
                    
                    known_ids = get_known_ids(conn, company_id)
                    new_reports = [r for r in reports if r['file_id'] not in known_ids]
                    
                    # Обновляем last_seen для всех отчетов
                    all_file_ids = {r['file_id'] for r in reports}
                    update_last_seen(conn, all_file_ids, company_id)
                    
                    if new_reports:
                        save_new_reports(conn, new_reports, company_id)
                        
                        # Формируем сообщение
                        message = f"🔥 <b>Новые отчёты {format_company_info(company)}</b> ({len(new_reports)}):\n\n"
                        
                        for j, report in enumerate(new_reports[:20], start=1):  # Ограничиваем до 20
                            message += (
                                f"{j}. <b>{report['period']}</b>\n"
                                f"   📄 {report['doc_type']}\n"
                                f"   📅 {report['publish']}\n\n"
                            )
                        
                        # Создаем кнопки
                        keyboard = []
                        for j, report in enumerate(new_reports[:10], start=1):
                            keyboard.append([
                                InlineKeyboardButton(
                                    f"Открыть: {report['period'][:30]}",
                                    callback_data=f"open_{report['file_id']}"
                                )
                            ])
                        
                        # Отправляем только подписчикам этой компании
                        subscribers = get_subscribers_for_company(conn, company_id)
                        sent_count = 0
                        
                        for chat_id in subscribers:
                            try:
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=message[:Config.MAX_MESSAGE_LENGTH],
                                    parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
                                )
                                sent_count += 1
                                await asyncio.sleep(0.05)  # Rate limiting для Telegram
                            except Exception as e:
                                logger.error(f"Не удалось отправить {chat_id}: {e}")
                                if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                                    unsubscribe_user_from_company(conn, chat_id, company_id)
                        
                        logger.info(f"Уведомлений для {company.get('name')}: {sent_count}/{len(subscribers)}")
                    else:
                        logger.info(f"Новых отчётов для {company.get('name')} нет")
                    
                    # Задержка между компаниями
                    if i < len(company_ids) - 1:
                        delay = random.uniform(*Config.RATE_LIMIT_DELAY)
                        await asyncio.sleep(delay)
                    
                except Exception as e:
                    logger.error(f"Ошибка при проверке компании {company_id}: {e}")
                    continue
                
        except Exception as e:
            logger.error(f"Ошибка при проверке: {e}")
    
    # Очищаем кэш парсера
    parse_cache.clear()
    logger.info("Фоновая проверка завершена")


# ==================== КОМАНДЫ БОТА ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начальная команда"""
    await update.message.reply_text(
        "👋 <b>Привет! Я парсер бухгалтерских отчётов.</b>\n\n"
        "🔍 <b>Поиск отчётов:</b>\n"
        "/check Газпром — посмотреть отчёты\n\n"
        "📬 <b>Подписки:</b>\n"
        "/subscribe Газпром — подписаться на уведомления\n"
        "/unsubscribe Газпром — отписаться\n"
        "/subscriptions — мои подписки\n\n"
        "💡 <b>Советы:</b>\n"
        "• Можно искать по тикеру: /check GAZP\n"
        "• Можно искать по части названия: /check газ\n"
        "• При частичном поиске бот покажет список\n\n"
        "❓ /help — подробная помощь",
        parse_mode="HTML"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    await update.message.reply_text(
        "📚 <b>Помощь по боту</b>\n\n"
        "🔍 <b>Поиск отчётов:</b>\n"
        "/check Компания — показать бухгалтерские отчёты\n"
        "Пример: /check Газпром\n\n"
        "📬 <b>Подписки:</b>\n"
        "/subscribe Компания — подписаться на уведомления\n"
        "/unsubscribe Компания — отписаться\n"
        "/subscriptions — мои подписки\n\n"
        "💡 <b>Советы:</b>\n"
        "• Можно искать по тикеру: GAZP, SBER, VTBR\n"
        "• Можно искать по части названия: 'газ', 'нефть'\n"
        "• При частичном поиске бот покажет список совпадений\n"
        "• Уведомления приходят только по новым отчётам\n\n"
        "🔄 Проверка новых отчётов — каждый час\n"
        "⏱ Отчеты кэшируются на 5 минут",
        parse_mode="HTML"
    )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка отчетов конкретной компании"""
    chat_id = str(update.effective_chat.id)
    
    # Обновляем активность пользователя
    username = update.effective_user.username or update.effective_user.first_name or "unknown"
    with get_db() as conn:
        update_user_activity(conn, chat_id, username)
    
    if not context.args:
        await update.message.reply_text(
            "❌ Укажите компанию. Например:\n"
            "/check Газпром\n"
            "/check GAZP"
        )
        return
    
    query = " ".join(context.args)
    results = search_companies(query)
    
    if not results:
        await update.message.reply_text(
            f"❌ Компания '{query}' не найдена\n"
            "Проверьте название или используйте тикер"
        )
        return
    
    if len(results) == 1:
        # Одна компания - сразу показываем отчеты
        await show_company_reports(update, context, results[0])
    else:
        # Несколько компаний - предлагаем выбрать
        cleanup_search_state()  # Очищаем старые состояния
        
        search_results[chat_id] = {
            "companies": results,
            "timestamp": time.time()
        }
        
        message = f"🔍 <b>Найдено несколько компаний по запросу '{query}':</b>\n\n"
        for i, company in enumerate(results[:20], 1):  # Ограничиваем до 20
            message += f"{i}. {format_company_info(company)}\n"
        
        if len(results) > 20:
            message += f"\n... и ещё {len(results) - 20}"
        
        message += "\nВведите номер компании или уточните запрос.\n/search отмена — отменить поиск"
        
        await update.message.reply_text(message, parse_mode="HTML")


async def show_company_reports(update: Update, context: ContextTypes.DEFAULT_TYPE, company: Dict):
    """Показывает отчеты выбранной компании"""
    msg = await update.message.reply_text(
        f"🔍 Проверяю {format_company_info(company)}...", 
        parse_mode="HTML"
    )
    
    try:
        reports = await parse_reports_cached(company["id"])
    except Exception as e:
        logger.error(f"Ошибка при загрузке отчетов для {company.get('name')}: {e}")
        await msg.edit_text(
            f"❌ Ошибка при загрузке отчетов для {format_company_info(company)}\n"
            f"Попробуйте позже",
            parse_mode="HTML"
        )
        return
    
    if not reports:
        await msg.edit_text(
            f"📊 {format_company_info(company)}\n"
            f"❌ Бухгалтерские отчёты не найдены",
            parse_mode="HTML"
        )
        return
    
    with get_db() as conn:
        known_ids = get_known_ids(conn, company["id"])
        new_reports = [r for r in reports if r['file_id'] not in known_ids]
        
        # Обновляем last_seen
        all_file_ids = {r['file_id'] for r in reports}
        update_last_seen(conn, all_file_ids, company["id"])
        
        # Сохраняем новые
        if new_reports:
            save_new_reports(conn, new_reports, company["id"])
        
        # Формируем сообщение
        message = f"📊 {format_company_info(company)}\n"
        if new_reports:
            message += f"🔥 Новых: {len(new_reports)}\n\n"
        else:
            message += "\n"
        
        message += f"📚 <b>Всего отчётов: {len(reports)}</b>\n\n"
        
        keyboard = []
        for i, report in enumerate(reports[:20], start=1):  # Ограничиваем до 20
            is_new = "⭐ " if report['file_id'] not in known_ids else ""
            message += (
                f"{is_new}{i}. <b>{report['period']}</b>\n"
                f"   📄 {report['doc_type']}\n"
                f"   📅 {report['publish']}\n\n"
            )
            
            if i <= 10:
                keyboard.append([
                    InlineKeyboardButton(
                        f"{'🆕 ' if report['file_id'] not in known_ids else ''}{report['period'][:30]}",
                        callback_data=f"open_{report['file_id']}"
                    )
                ])
        
        # Удаляем сообщение "Проверяю..."
        await msg.delete()
        
        # Проверяем длину сообщения
        if len(message) > Config.MAX_MESSAGE_LENGTH:
            # Разбиваем на части
            parts = []
            current_part = ""
            
            for line in message.split('\n'):
                if len(current_part) + len(line) + 1 > Config.MAX_MESSAGE_LENGTH:
                    parts.append(current_part)
                    current_part = line + '\n'
                else:
                    current_part += line + '\n'
            
            if current_part:
                parts.append(current_part)
            
            # Отправляем все части кроме последней
            for part in parts[:-1]:
                await update.message.reply_text(part, parse_mode="HTML")
            
            # Последнюю часть с кнопками
            await update.message.reply_text(
                parts[-1],
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
            )
        else:
            await update.message.reply_text(
                message,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
            )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    chat_id = str(update.effective_chat.id)
    text = update.message.text.strip()
    
    # Очищаем старые состояния
    cleanup_search_state()
    
    # Проверяем команду отмены
    if text.lower() in ['/search отмена', 'отмена', 'cancel']:
        if chat_id in search_results:
            del search_results[chat_id]
            await update.message.reply_text("❌ Поиск отменен")
        else:
            await update.message.reply_text("Нет активного поиска для отмены")
        return
    
    # Проверяем, есть ли активный поиск
    if chat_id in search_results:
        try:
            index = int(text) - 1
            companies = search_results[chat_id]["companies"]
            if 0 <= index < len(companies):
                company = companies[index]
                del search_results[chat_id]
                await show_company_reports(update, context, company)
                return
            else:
                await update.message.reply_text(
                    f"❌ Неверный номер. Выберите от 1 до {len(companies)}"
                )
                return
        except ValueError:
            # Не число - очищаем состояние
            del search_results[chat_id]
    
    # Если это не выбор из списка
    await update.message.reply_text(
        "Используйте команды:\n"
        "/check Компания — посмотреть отчёты\n"
        "/subscribe Компания — подписаться\n"
        "/help — помощь"
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("open_"):
        file_id = query.data.replace("open_", "")
        link = f"{Config.BASE_URL}/view/{file_id}"
        await query.message.reply_text(
            f"🔗 <a href='{link}'>Открыть отчёт</a>",
            parse_mode="HTML"
        )


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подписка на компанию"""
    chat_id = str(update.effective_chat.id)
    username = update.effective_user.username or update.effective_user.first_name or "unknown"
    
    if not context.args:
        await update.message.reply_text(
            "❌ Укажите компанию. Например:\n"
            "/subscribe Газпром\n"
            "/subscribe GAZP"
        )
        return
    
    query = " ".join(context.args)
    results = search_companies(query)
    
    if not results:
        await update.message.reply_text(
            f"❌ Компания '{query}' не найдена\n"
            "Проверьте название или используйте тикер"
        )
        return
    
    if len(results) == 1:
        company = results[0]
        with get_db() as conn:
            update_user_activity(conn, chat_id, username)
            subscribe_user_to_company(conn, chat_id, company["id"])
        
        ticker = company.get('ticker', '')
        unsub_hint = f" /unsubscribe {ticker}" if ticker else ""
        
        await update.message.reply_text(
            f"✅ Подписка оформлена на {format_company_info(company)}\n"
            f"Новые отчёты будут приходить автоматически.{unsub_hint}",
            parse_mode="HTML"
        )
    else:
        # Проверяем, подписан ли уже на кого-то из списка
        with get_db() as conn:
            user_subs = get_user_subscriptions(conn, chat_id)
            already_subscribed = [c for c in results if c["id"] in user_subs]
        
        if already_subscribed and len(already_subscribed) == len(results):
            await update.message.reply_text(
                f"❌ Вы уже подписаны на все компании по запросу '{query}'"
            )
            return
        
        message = f"🔍 <b>Найдено несколько компаний по запросу '{query}':</b>\n\n"
        for i, company in enumerate(results[:20], 1):
            status = "✅" if company in already_subscribed else "➕"
            message += f"{i}. {status} {format_company_info(company)}\n"
        
        if len(results) > 20:
            message += f"\n... и ещё {len(results) - 20}"
        
        message += "\nУточните запрос, например:\n/subscribe Газпром нефть"
        
        await update.message.reply_text(message, parse_mode="HTML")


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отписка от компании"""
    chat_id = str(update.effective_chat.id)
    
    if not context.args:
        # Показываем все подписки
        with get_db() as conn:
            user_subs = get_user_subscriptions(conn, chat_id)
            
            if not user_subs:
                await update.message.reply_text(
                    "❌ У вас нет активных подписок.\n"
                    "/subscribe Компания — подписаться"
                )
                return
            
            message = "📬 <b>Ваши подписки:</b>\n\n"
            for company_id in user_subs:
                company = get_company_by_id(company_id)
                if company:
                    ticker = company.get('ticker', '')
                    message += f"• {format_company_info(company)}\n"
                    if ticker:
                        message += f"  /unsubscribe {ticker}\n"
                    else:
                        message += f"  /unsubscribe {company['name']}\n"
            
            message += "\nИспользуйте /unsubscribe с тикером или названием компании"
            await update.message.reply_text(message, parse_mode="HTML")
        return
    
    query = " ".join(context.args)
    results = search_companies(query)
    
    if not results:
        await update.message.reply_text(
            f"❌
