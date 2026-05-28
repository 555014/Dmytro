import os
import time
import json
import logging
import requests
import ccxt
import numpy as np
from datetime import datetime

# ============================================================
#  КОНФИГ
# ============================================================
TELEGRAM_TOKEN   = "8878348369:AAFek22T7qVFD55cYXIhkJWz9FwcLLL9DGw"
TELEGRAM_CHAT_ID = "6049805703"

SYMBOLS    = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
TIMEFRAMES = ["10m", "30m", "1h"]

CHECK_INTERVAL = 120   # секунды между проверками
ANTI_SPAM_SEC  = 600   # пауза между сигналами одной монеты (10 мин)
CANDLES_NEEDED = 100

STATS_FILE = "bot_stats.json"
LOG_FILE   = "bot.log"

# ============================================================
#  ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ============================================================
#  СОСТОЯНИЕ
# ============================================================
last_signal_time: dict = {}
signal_history:   list = []

# ============================================================
#  БИРЖА
# ============================================================
exchange = ccxt.mexc({
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# ============================================================
#  RETRY — устойчивость к обрывам интернета
# ============================================================
def retry(func, retries: int = 7, base_delay: float = 5.0):
    for attempt in range(retries):
        try:
            return func()
        except Exception as exc:
            wait = base_delay * (2 ** attempt)
            log.warning(f"Попытка {attempt+1}/{retries} не удалась: {exc}. "
                        f"Повтор через {wait:.0f}с...")
            time.sleep(wait)
    log.error("Все попытки исчерпаны, пропускаем операцию.")
    return None

# ============================================================
#  ИНДИКАТОРЫ
# ============================================================
def ema(prices: list, period: int) -> np.ndarray:
    arr = np.array(prices, dtype=float)
    k   = 2.0 / (period + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(prices: list, period: int = 14) -> float:
    arr    = np.array(prices, dtype=float)
    delta  = np.diff(arr)
    gains  = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    avg_g = np.mean(gains[:period])
    avg_l = np.mean(losses[:period])
    for i in range(period, len(delta)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)

# ============================================================
#  АНАЛИЗ ТАЙМФРЕЙМА
# ============================================================
def analyze_tf(symbol: str, tf: str) -> dict | None:
    data = retry(lambda: exchange.fetch_ohlcv(symbol, tf, limit=CANDLES_NEEDED))
    if data is None or len(data) < 55:
        log.warning(f"{symbol} {tf}: недостаточно данных")
        return None

    closes = [c[4] for c in data]
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)
    r   = rsi(closes, 14)

    bullish = (e9[-1] > e21[-1] > e50[-1]) and (e9[-2] <= e21[-2] or r < 38)
    bearish = (e9[-1] < e21[-1] < e50[-1]) and (e9[-2] >= e21[-2] or r > 62)

    signal = None
    if bullish and r < 68:
        signal = "BUY"
    elif bearish and r > 32:
        signal = "SELL"

    return {
        "tf":     tf,
        "signal": signal,
        "price":  closes[-1],
        "rsi":    round(r, 2),
        "ema9":   round(e9[-1], 6),
        "ema21":  round(e21[-1], 6),
        "ema50":  round(e50[-1], 6),
    }

# ============================================================
#  МУЛЬТИ-ТАЙМФРЕЙМ СИГНАЛ
# ============================================================
def multi_tf_signal(symbol: str) -> dict | None:
    results = {}
    for tf in TIMEFRAMES:
        r = analyze_tf(symbol, tf)
        if r:
            results[tf] = r

    if len(results) < 2:
        return None

    signals    = [v["signal"] for v in results.values() if v["signal"]]
    buy_count  = signals.count("BUY")
    sell_count = signals.count("SELL")

    if buy_count >= 2:
        direction  = "BUY"
        confidence = round(buy_count / len(TIMEFRAMES) * 100)
    elif sell_count >= 2:
        direction  = "SELL"
        confidence = round(sell_count / len(TIMEFRAMES) * 100)
    else:
        return None

    return {
        "symbol":     symbol,
        "direction":  direction,
        "confidence": confidence,
        "price":      list(results.values())[-1]["price"],
        "timeframes": results,
    }

# ============================================================
#  TELEGRAM
# ============================================================
def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    def _send():
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()

    retry(_send, retries=5, base_delay=4)


def format_message(sig: dict) -> str:
    direction = sig["direction"]
    symbol    = sig["symbol"].replace("/", "")
    price     = sig["price"]
    conf      = sig["confidence"]

    top_emoji = "🟢" if direction == "BUY" else "🔴"

    tf_lines = ""
    for tf, d in sig["timeframes"].items():
        if d["signal"] == direction:
            mark = "✅"
        elif d["signal"] is None:
            mark = "➖"
        else:
            mark = "❌"
        tf_lines += f"  {mark} {tf}: RSI {d['rsi']}\n"

    return (
        f"{top_emoji} <b>{direction} {symbol}</b>\n"
        f"💰 Цена: <b>{price}</b>\n"
        f"📊 Уверенность: <b>{conf}%</b>\n\n"
        f"<b>Таймфреймы:</b>\n{tf_lines}\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S %d.%m.%Y')}"
    )

# ============================================================
#  ANTI-SPAM
# ============================================================
def can_signal(symbol: str) -> bool:
    return time.time() - last_signal_time.get(symbol, 0) >= ANTI_SPAM_SEC

# ============================================================
#  СТАТИСТИКА
# ============================================================
def load_stats() -> None:
    global signal_history
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        signal_history = data.get("signals", [])
        log.info(f"Статистика загружена: {len(signal_history)} сигналов")
    except FileNotFoundError:
        log.info("Начинаем с нуля")
    except Exception as e:
        log.warning(f"Ошибка загрузки статистики: {e}")


def save_stats() -> None:
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump({"signals": signal_history[-50:]}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"Ошибка сохранения: {e}")

# ============================================================
#  ГЛАВНЫЙ ЦИКЛ
# ============================================================
def main() -> None:
    log.info("=" * 55)
    log.info("🚀 MEXC Trading Bot запускается...")
    log.info(f"   Символы:    {SYMBOLS}")
    log.info(f"   Таймфреймы: {TIMEFRAMES}")
    log.info("=" * 55)

    load_stats()
    send_telegram(
        "🚀 <b>MEXC Trading Bot запущен</b>\n"
        "📊 Таймфреймы: <b>10m | 30m | 1h</b>\n"
        f"💎 Монеты: BTC | ETH | SOL\n"
        f"🕐 {datetime.now().strftime('%H:%M %d.%m.%Y')}"
    )

    while True:
        cycle_start = time.time()
        log.info(f"--- Цикл {datetime.now().strftime('%H:%M:%S')} ---")

        for symbol in SYMBOLS:
            try:
                sig = multi_tf_signal(symbol)

                if sig is None:
                    log.info(f"  {symbol}: нет сигнала")
                    continue

                if not can_signal(symbol):
                    log.info(f"  {symbol}: anti-spam")
                    continue

                log.info(f"  ⚡ {symbol} {sig['direction']} {sig['confidence']}%")
                send_telegram(format_message(sig))

                last_signal_time[symbol] = time.time()
                signal_history.append({
                    "time":       datetime.now().isoformat(),
                    "symbol":     symbol,
                    "direction":  sig["direction"],
                    "price":      sig["price"],
                    "confidence": sig["confidence"],
                })
                save_stats()

            except Exception as exc:
                log.error(f"Ошибка {symbol}: {exc}", exc_info=True)

        elapsed   = time.time() - cycle_start
        sleep_for = max(0, CHECK_INTERVAL - elapsed)
        log.info(f"Следующий цикл через {sleep_for:.0f}с.")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
