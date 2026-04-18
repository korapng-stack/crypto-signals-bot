import requests
import time
import logging
from datetime import datetime, timedelta
from threading import Thread, Lock

# ═══════════════════════════════════════════════════
# Logging — يحفظ كل شيء في ملف + يعرضه في الـ terminal
# ═══════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
# إعداداتك الشخصية — غيّر هذين السطرين فقط
# ═══════════════════════════════════════════════════
import os

TELEGRAM_TOKEN =os.environ.get("8686033782:AAERHDdnvDC6kIYBG1TqTYt8Dau7QxLDBkA")
CHAT_ID        = os.environ.get("2143639881")

# ═══════════════════════════════════════════════════
# الإعدادات
# ═══════════════════════════════════════════════════
EMA_FAST        = 11      # EMA السريع
EMA_SLOW        = 26      # EMA البطيء
CONFIRM_MIN_PCT = 0.002   # 0.2% ارتفاع مطلوب للتأكيد
WAIT_CANDLES    = 2       # انتظار شمعتين (10 دقائق) قبل التأكيد
MAX_CHECKS      = 30      # أقصى محاولات تأكيد = 150 دقيقة
SELL_WAIT_MIN   = 10      # انتظار دقائق قبل SELL
BUY_EXPIRY_HRS  = 24      # حذف BUY بعد 24 ساعة
COOLDOWN_MIN    = 30      # cooldown بعد كل إشارة BUY

# تأخير مخصص لكل منصة (ثانية)
REQUEST_DELAY = {
    "Binance" : 0.15,
    "MEXC"    : 0.25,
    "Bybit"   : 0.25,
    "Gate"    : 0.35,
    "KuCoin"  : 0.35,
}

# ═══════════════════════════════════════════════════
# Sessions — session واحد لكل منصة يقلل latency
# ═══════════════════════════════════════════════════
# Headers موحدة — تمنع رفض الطلبات من Gate / KuCoin
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CryptoBot/1.0)",
    "Accept"    : "application/json",
}

SESSION_BINANCE = requests.Session()
SESSION_MEXC    = requests.Session()
SESSION_BYBIT   = requests.Session()
SESSION_GATE    = requests.Session()
SESSION_KUCOIN  = requests.Session()

for _s in [SESSION_BINANCE, SESSION_MEXC, SESSION_BYBIT, SESSION_GATE, SESSION_KUCOIN]:
    _s.headers.update(_HEADERS)

# ═══════════════════════════════════════════════════
# Retry مع Backoff — يمنع توقف البوت عند 429
# ═══════════════════════════════════════════════════

def retry_get(session, url, params, retries=3, timeout=5):
    """
    يحاول 3 مرات مع انتظار تصاعدي:
    محاولة 1 → فورًا
    محاولة 2 → بعد 2 ثانية
    محاولة 3 → بعد 5 ثوانٍ
    عند 429 (rate limit) → ينتظر تلقائياً
    """
    waits = [0, 2, 5]
    for attempt in range(retries):
        try:
            if waits[attempt] > 0:
                time.sleep(waits[attempt])
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                log.warning("⏸️  Rate limit — انتظار 10 ثوانٍ")
                time.sleep(10)
                continue
            if r.status_code != 200:
                log.warning(f"⚠️  HTTP {r.status_code} من {url}")
                continue
            return r
        except Exception as e:
            log.warning(f"⚠️  retry {attempt+1}: {e}")
    return None

# ═══════════════════════════════════════════════════
# دوال جلب البيانات
# ═══════════════════════════════════════════════════

def get_closes_binance(symbol, limit=152):
    # بنية Binance kline: [openTime, open, high, low, CLOSE(4), ...]
    # ترتيب: أقدم → أحدث
    # نأخذ closes[-2] فقط (آخر شمعة مغلقة) — [-1] قد تكون لا تزال تتشكل
    try:
        r = retry_get(SESSION_BINANCE, "https://api.binance.com/api/v3/klines",
                      {"symbol": symbol, "interval": "5m", "limit": limit})
        if r is None: return None
        data = r.json()
        if isinstance(data, list) and len(data) >= EMA_SLOW + 5:
            closes = [float(c[4]) for c in data[:-1]]
            if all(isinstance(x, float) and x > 0 for x in closes[-5:]):
                return closes
    except Exception as e:
        log.warning(f"⚠️ Binance {symbol}: {e}")
    return None

def get_closes_mexc(symbol, limit=152):
    # نفس بنية Binance — c[4] = close
    try:
        r = retry_get(SESSION_MEXC, "https://api.mexc.com/api/v3/klines",
                      {"symbol": symbol, "interval": "5m", "limit": limit})
        if r is None: return None
        data = r.json()
        if isinstance(data, list) and len(data) >= EMA_SLOW + 5:
            closes = [float(c[4]) for c in data[:-1]]
            if all(isinstance(x, float) and x > 0 for x in closes[-5:]):
                return closes
    except Exception as e:
        log.warning(f"⚠️ MEXC {symbol}: {e}")
    return None

def get_closes_bybit(symbol, limit=152):
    # Bybit v5 بنية الشمعة:
    # [startTime(0), open(1), high(2), low(3), close(4), volume(5), turnover(6)]
    # الترتيب: أحدث → أقدم — نعكس
    # نحذف أول عنصر بعد العكس (= آخر شمعة غير مكتملة)
    try:
        r = retry_get(SESSION_BYBIT, "https://api.bybit.com/v5/market/kline",
                      {"symbol": symbol, "interval": "5", "limit": limit, "category": "spot"})
        if r is None: return None
        data = r.json()
        if data.get("retCode") == 0:
            candles = data["result"]["list"]
            if len(candles) >= EMA_SLOW + 5:
                # reversed → أقدم→أحدث، ثم نحذف الأخيرة (غير مكتملة)
                ordered = list(reversed(candles))
                closes = [float(c[4]) for c in ordered[:-1]]
                if all(isinstance(x, float) and x > 0 for x in closes[-5:]):
                    return closes
    except Exception as e:
        log.warning(f"⚠️ Bybit {symbol}: {e}")
    return None

def get_closes_gate(symbol, limit=152):
    # Gate.io candlesticks v4 بنية الاستجابة:
    # [timestamp(0), volume(1), close(2), high(3), low(4), open(5)]
    # close = index 2 ✅
    # الترتيب: أقدم → أحدث (لا حاجة للعكس)
    # نحذف آخر شمعة (غير مكتملة)
    try:
        pair = symbol.replace("USDT", "_USDT")
        r = retry_get(SESSION_GATE, "https://api.gateio.ws/api/v4/spot/candlesticks",
                      {"currency_pair": pair, "interval": "5m", "limit": limit})
        if r is None: return None
        data = r.json()
        if isinstance(data, list) and len(data) >= EMA_SLOW + 5:
            closes = [float(c[2]) for c in data[:-1]]
            # تحقق إضافي: هل القيم منطقية؟ (يكشف index خاطئ)
            if all(v > 0 for v in closes[-5:]):
                return closes
    except Exception as e:
        log.warning(f"⚠️ Gate {symbol}: {e}")
    return None

def get_closes_kucoin(symbol, limit=152):
    # KuCoin candles بنية الاستجابة:
    # [timestamp(0), open(1), close(2), high(3), low(4), volume(5), amount(6)]
    # close = index 2 ✅
    # الترتيب: أحدث → أقدم — نعكس
    # نحذف آخر شمعة (غير مكتملة)
    try:
        pair = symbol.replace("USDT", "-USDT")
        r = retry_get(SESSION_KUCOIN, "https://api.kucoin.com/api/v1/market/candles",
                      {"symbol": pair, "type": "5min"})
        if r is None: return None
        data = r.json()
        if data.get("code") == "200000" and data.get("data"):
            candles = data["data"][:limit]
            if len(candles) >= EMA_SLOW + 5:
                ordered = list(reversed(candles))
                closes = [float(c[2]) for c in ordered[:-1]]
                if all(isinstance(x, float) and x > 0 for x in closes[-5:]):
                    return closes
    except Exception as e:
        log.warning(f"⚠️ KuCoin {symbol}: {e}")
    return None

# ═══════════════════════════════════════════════════
# العملات لكل منصة
# ═══════════════════════════════════════════════════

BINANCE_SYMBOLS = [
    "BTCUSDT",    "ETHUSDT",    "XRPUSDT",    "ADAUSDT",    "SOLUSDT",
    "DOTUSDT",    "DOGEUSDT",   "AVAXUSDT",   "LTCUSDT",    "LINKUSDT",
    "ATOMUSDT",   "XLMUSDT",    "FILUSDT",    "TRXUSDT",    "ALGOUSDT",
    "XMRUSDT",    "ICPUSDT",    "EGLDUSDT",   "HBARUSDT",   "NEARUSDT",
    "APEUSDT",    "DASHUSDT",   "ZILUSDT",    "ZECUSDT",    "ZENUSDT",
    "STORJUSDT",  "RAREUSDT",   "OPUSDT",     "ARBUSDT",    "SEIUSDT",
    "TIAUSDT",    "WLDUSDT",    "ORDIUSDT",   "RENDERUSDT", "PHAUSDT",
    "POLUSDT",    "TRBUSDT",    "VIRTUALUSDT","WALUSDT",    "APTUSDT",
    "BCHUSDT",    "BIOUSDT",    "CHRUSDT",    "GRTUSDT",    "ARKMUSDT",
    "AGLDUSDT",   "OPENUSDT",   "PLUMEUSDT",  "SAHARAUSDT", "SUSDT",
    "LINEAUSDT",  "XPLUSDT",
]

MEXC_SYMBOLS = [
    "XCNUSDT",     "COREUSDT",    "PIUSDT",      "XDCUSDT",     "RIOUSDT",
    "PLAYUSDT",    "STABLEUSDT",  "BLESSUSDT",   "COAIUSDT",    "CROSSUSDT",
    "FHEUSDT",     "GRASSUSDT",   "GRIFFAINUSDT","HUSDT",       "LIGHTUSDT",
    "ALEOUSDT",    "PINUSDT",     "PORT3USDT",   "KGENUSDT",    "ABUSDT",
    "ATHUSDT",     "ARCUSDT",     "AIOUSDT",     "A8USDT",      "ALUUSDT",
    "XPRUSDT",     "OMGUSDT",
]

BYBIT_SYMBOLS = [
    "UXLINKUSDT",  "KASUSDT",     "MNTUSDT",     "FLOCKUSDT",   "PAALUSDT",
    "L3USDT",      "ALCHUSDT",    "ZIGUSDT",     "MONUSDT",     "CSPRUSDT",
    "INSPUSDT",    "MOVEUSDT",    "COOKIEUSDT",  "LRCUSDT",     "ZROUSDT",
    "MOVRUSDT",    "TONUSDT",     "FETUSDT",     "SUIUSDT",     "GALAUSDT",
    "TAOUSDT",     "QNTUSDT",     "SANDUSDT",    "ETCUSDT",     "TNSRUSDT",
    "KAIAUSDT",    "PYTHUSDT",    "AIXBTUSDT",   "BLURUSDT",    "ZKUSDT",
    "JASMYUSDT",   "PARTIUSDT",   "THETAUSDT",   "BICOUSDT",    "POLUSDT",
]

GATE_SYMBOLS = [
    "AKTUSDT",     "RADUSDT",     "ALTUSDT",     "BATUSDT",     "MINAUSDT",
    "IDUSDT",      "MTLUSDT",     "BANDUSDT",    "ICXUSDT",     "STGUSDT",
    "PROVEUSDT",   "STXUSDT",     "SKLUSDT",     "GLMUSDT",     "XTZUSDT",
    "IQUSDT",      "HOTUSDT",     "LAUSDT",      "RLCUSDT",     "VANAUSDT",
    "BEAMUSDT",    "PONDUSDT",    "LPTUSDT",     "MIRAUSDT",    "GUSDT",
    "POWRUSDT",
]

KUCOIN_SYMBOLS = [
    "AIOZUSDT",    "DUSKUSDT",    "IOTXUSDT",    "MANTAUSDT",   "NIGHTUSDT",
    "CELRUSDT",    "ANKRUSDT",    "ENSUSDT",     "API3USDT",    "WUSDT",
    "MANAUSDT",    "CELOUSDT",    "EIGENUSDT",   "GASUSDT",     "ENJUSDT",
    "GMTUSDT",     "IOUSDT",      "KAITOUSDT",   "ACTUSDT",     "CHZUSDT",
    "DEXEUSDT",    "HNTUSDT",     "FLUXUSDT",    "PORTALUSDT",  "EDUUSDT",
    "IOSTUSDT",    "VETUSDT",
]

# SYMBOL_MAP: symbol → (exchange_name, fetch_function)
SYMBOL_MAP = {}
for sym in BINANCE_SYMBOLS : SYMBOL_MAP[sym] = ("Binance", get_closes_binance)
for sym in MEXC_SYMBOLS     : SYMBOL_MAP[sym] = ("MEXC",    get_closes_mexc)
for sym in BYBIT_SYMBOLS    : SYMBOL_MAP[sym] = ("Bybit",   get_closes_bybit)
for sym in GATE_SYMBOLS     : SYMBOL_MAP[sym] = ("Gate",    get_closes_gate)
for sym in KUCOIN_SYMBOLS   : SYMBOL_MAP[sym] = ("KuCoin",  get_closes_kucoin)

ALL_SYMBOLS = list(SYMBOL_MAP.keys())

# ═══════════════════════════════════════════════════
# قوائم الحالة + Lock
# ═══════════════════════════════════════════════════
pending_buy  = {}
active_buy   = {}
pending_sell = {}
cooldown     = {}
state_lock   = Lock()

# ═══════════════════════════════════════════════════
# Telegram
# ═══════════════════════════════════════════════════

def send_message(text, reply_to=None):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    try:
        r   = requests.post(url, data=data, timeout=5)
        res = r.json()
        if res.get("ok"):
            return res["result"]["message_id"]
    except Exception as e:
        log.error(f"❌ Telegram error: {e}", exc_info=True)
    return None

# ═══════════════════════════════════════════════════
# حساب EMA — incremental
# ═══════════════════════════════════════════════════

def calc_ema_series(closes, period):
    if len(closes) < period + 1:
        return None, None
    k    = 2 / (period + 1)
    ema  = sum(closes[:period]) / period
    prev = ema
    for price in closes[period:]:
        prev = ema
        ema  = price * k + ema * (1 - k)
    return ema, prev

# ═══════════════════════════════════════════════════
# تنسيق
# ═══════════════════════════════════════════════════

def fmt_symbol(symbol):
    if symbol.endswith("USDT"):
        return symbol[:-4] + "/USDT"
    return symbol

def fmt_price(price):
    if price >= 1000:
        return f"{price:,.2f}"
    elif price >= 1:
        return f"{price:.4f}"
    else:
        return f"{price:.6f}"

# ═══════════════════════════════════════════════════
# Cooldown
# ═══════════════════════════════════════════════════

def can_signal(symbol):
    """يُستدعى دائماً داخل state_lock"""
    if symbol not in cooldown:
        return True
    return (datetime.now() - cooldown[symbol]) >= timedelta(minutes=COOLDOWN_MIN)

def cleanup_cooldown():
    with state_lock:
        for sym in list(cooldown):
            if datetime.now() - cooldown[sym] > timedelta(hours=2):
                del cooldown[sym]

# ═══════════════════════════════════════════════════
# المنطق الرئيسي
# ═══════════════════════════════════════════════════

def check_symbol(symbol, exchange, fetch_func):
    now = datetime.now()

    # ── 1) تنظيف BUY المنتهية (24 ساعة) ──
    with state_lock:
        if symbol in active_buy:
            if now - active_buy[symbol]["buy_time"] > timedelta(hours=BUY_EXPIRY_HRS):
                log.info(f"🗑️  {symbol} حُذف (24h)")
                del active_buy[symbol]
                pending_sell.pop(symbol, None)
                return

    # ── 2) SELL معلق — تحقق بعد 10 دقائق ──
    with state_lock:
        in_sell  = symbol in pending_sell
        sell_age = (
            (now - pending_sell[symbol]["sell_time"]).total_seconds() / 60
            if in_sell else 0
        )

    if in_sell:
        if sell_age >= SELL_WAIT_MIN:
            closes = fetch_func(symbol)
            if closes and len(closes) >= EMA_SLOW + 2:
                ema11, _ = calc_ema_series(closes, EMA_FAST)
                ema26, _ = calc_ema_series(closes, EMA_SLOW)
                # ✅ فحص صريح بـ is not None بدل if ema11
                if ema11 is not None and ema26 is not None and ema11 < ema26:
                    with state_lock:
                        pair   = fmt_symbol(symbol)
                        msg_id = active_buy.get(symbol, {}).get("message_id")
                    sell_msg = f"<b>{pair}</b>\nSELL NOW ❌"
                    if msg_id:
                        send_message(sell_msg, reply_to=msg_id)
                    else:
                        send_message(sell_msg)
                    log.info(f"🔴 SELL: {symbol}")
            with state_lock:
                pending_sell.pop(symbol, None)
                active_buy.pop(symbol, None)
        return

    # ── 3) جلب البيانات ──
    closes = fetch_func(symbol)
    if not closes or len(closes) < EMA_SLOW + 5:
        return

    # ── 4) السعر من آخر شمعة مغلقة مؤكدة ──
    # closes[-1] محذوفة مسبقاً في دوال الجلب (شمعة غير مكتملة)
    price = closes[-1]

    ema11_now, ema11_prev = calc_ema_series(closes, EMA_FAST)
    ema26_now, ema26_prev = calc_ema_series(closes, EMA_SLOW)

    # ✅ فحص صريح بـ is not None
    if any(v is None for v in [ema11_now, ema11_prev, ema26_now, ema26_prev]):
        return

    bullish = (
        (ema11_prev <= ema26_prev) and   # كروس EMA11 فوق EMA26
        (ema11_now  >  ema26_now)  and   # تأكيد الكروس
        (ema26_now  >  ema26_prev)       # trend filter: EMA26 صاعد
    )
    bearish = (ema11_prev >= ema26_prev) and (ema11_now < ema26_now)

    # ── 5) كروس BUY ──
    with state_lock:
        already_pending = symbol in pending_buy
        already_active  = symbol in active_buy
        ok_cooldown     = can_signal(symbol)

    if bullish and not already_pending and not already_active and ok_cooldown:
        with state_lock:
            pending_buy[symbol] = {
                "cross_price" : price,
                "cross_time"  : now,
                "checks"      : 0,
            }
        log.info(f"⏳ BUY كروس: {symbol} @ {fmt_price(price)} [{exchange}]")
        return

    # ── 6) كروس SELL ──
    with state_lock:
        in_active = symbol in active_buy
        in_sell2  = symbol in pending_sell

    if bearish and in_active and not in_sell2:
        with state_lock:
            pending_sell[symbol] = {"sell_time": now}
            pending_buy.pop(symbol, None)
        log.info(f"⏳ SELL كروس: {symbol} — انتظار 10 دقائق")
        return

    # ── 7) تأكيد BUY المعلق ──
    with state_lock:
        in_pending = symbol in pending_buy
        entry      = dict(pending_buy[symbol]) if in_pending else None

    if not in_pending:
        return

    elapsed = int((now - entry["cross_time"]).total_seconds() / 300)
    if elapsed < WAIT_CANDLES:
        return

    with state_lock:
        if symbol in pending_buy:
            pending_buy[symbol]["checks"] += 1
            checks = pending_buy[symbol]["checks"]
        else:
            return

    if checks > MAX_CHECKS:
        log.info(f"⏰ انتهت المهلة: {symbol}")
        with state_lock:
            pending_buy.pop(symbol, None)
        return

    # ✅ فحص صريح
    if ema11_now is not None and ema26_now is not None and ema11_now < ema26_now:
        log.info(f"↩️  كروس عكسي: {symbol}")
        with state_lock:
            pending_buy.pop(symbol, None)
        return

    pct = (price - entry["cross_price"]) / entry["cross_price"]
    if pct >= CONFIRM_MIN_PCT:
        pair = fmt_symbol(symbol)
        msg  = (
            f"👇💱👾🔥💥🚀🌕💯💯\n\n"
            f"<b>{pair}</b>\n"
            f"BUY NOW ✅\n"
            f"Price: {fmt_price(price)}\n\n"
            f"⚠️ Be Careful and Don't be greedy — take your profits.\n"
            f"⚠️ گەلەک تەماع نەبە _ و فایدێ خو وەربگرە.\n\n"
            f"💸💵💴💰💹💲💱👾"
        )
        msg_id = send_message(msg)
        log.info(f"✅ BUY: {symbol} @ {fmt_price(price)} (+{pct*100:.2f}%) [{exchange}]")

        with state_lock:
            active_buy[symbol] = {
                "buy_price"  : price,
                "buy_time"   : now,
                "message_id" : msg_id,
            }
            cooldown[symbol] = now
            pending_buy.pop(symbol, None)

# ═══════════════════════════════════════════════════
# تشغيل بالتوازي — thread مستقل + session لكل منصة
# ═══════════════════════════════════════════════════

def run_exchange(symbols, exchange, fetch_func):
    delay = REQUEST_DELAY.get(exchange, 0.25)
    for sym in symbols:
        try:
            check_symbol(sym, exchange, fetch_func)
        except Exception as e:
            log.warning(f"⚠️  [{exchange}] {sym}: {e}")
        time.sleep(delay)

def scan_all():
    exchanges = [
        (BINANCE_SYMBOLS, "Binance", get_closes_binance),
        (MEXC_SYMBOLS,    "MEXC",    get_closes_mexc),
        (BYBIT_SYMBOLS,   "Bybit",   get_closes_bybit),
        (GATE_SYMBOLS,    "Gate",    get_closes_gate),
        (KUCOIN_SYMBOLS,  "KuCoin",  get_closes_kucoin),
    ]
    threads = [
        Thread(target=run_exchange, args=(syms, exch, fn))
        for syms, exch, fn in exchanges
    ]
    for t in threads: t.start()
    for t in threads: t.join()

# ═══════════════════════════════════════════════════
# البرنامج الرئيسي
# ═══════════════════════════════════════════════════

# ═══════════════════════════════════════════════════
# Candle Alignment — ينتظر إغلاق الشمعة الفعلي
# ═══════════════════════════════════════════════════

def wait_for_candle_close():
    """
    ينتظر حتى إغلاق الشمعة الحقيقي (5m) + 5 ثوانٍ buffer
    يحل مشكلة اختلاف توقيت الشموع بين المنصات.
    """
    now  = datetime.utcnow()
    secs = (now.minute % 5) * 60 + now.second
    wait = (300 - secs) + 5   # 5 ثوانٍ buffer لـ Gate/KuCoin
    log.info(f"⏳ انتظار {wait:.0f} ثانية حتى إغلاق الشمعة...")
    time.sleep(wait)


def main():
    total = len(ALL_SYMBOLS)
    send_message(
        f"🚀 <b>البوت بدأ العمل</b>\n\n"
        f"📊 إجمالي العملات: <b>{total}</b>\n"
        f"🔷 Binance: {len(BINANCE_SYMBOLS)}\n"
        f"🔶 MEXC:    {len(MEXC_SYMBOLS)}\n"
        f"🟣 Bybit:   {len(BYBIT_SYMBOLS)}\n"
        f"🔵 Gate:    {len(GATE_SYMBOLS)}\n"
        f"🟢 KuCoin:  {len(KUCOIN_SYMBOLS)}\n\n"
        f"📈 EMA{EMA_FAST} / EMA{EMA_SLOW} | 5m Timeframe\n"
        f"✅ Confirmation: 0.2% | Cooldown: {COOLDOWN_MIN}min"
    )
    log.info(f"✅ البوت يعمل — {total} عملة على 5 منصات")

    cycle = 0
    while True:
        cycle += 1
        ts = datetime.now().strftime("%H:%M:%S")
        log.info(f"{'═'*55}")
        log.info(f"🔍 دورة #{cycle} | {ts} | {total} عملة")
        with state_lock:
            pb = len(pending_buy)
            ab = len(active_buy)
            ps = len(pending_sell)
        log.info(f"⏳pending={pb} | ✅active={ab} | 🔴sell={ps}")

        # ✅ انتظار الشمعة أولاً، ثم التحليل
        wait_for_candle_close()
        scan_all()
        cleanup_cooldown()


if __name__ == "__main__":
    main()
  
