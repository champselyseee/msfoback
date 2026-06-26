FROM python:3.11-slim

WORKDIR /app

# Системные зависимости (без ttf- пакетов, их заменили на fonts-)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    libnss3 \
    libnspr4 \
    libatk1.0-0t64 \
    libatk-bridge2.0-0t64 \
    libcups2t64 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2t64 \
    fonts-unifont \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Установка Chromium (только браузер, deps уже установлены выше)
RUN playwright install chromium
# Не вызываем install-deps — они уже стоят

# Код
COPY . .

# Запуск
CMD ["python", "bot.py"]
