#!/usr/bin/env python3
"""BTC Market Regime Analyzer - определение рыночного режима каждые 4 часа"""
import json, os, sys, time, hmac, hashlib, urllib.request
from datetime import datetime, timezone

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes/profiles/trader"))
STATE_FILE = os.path.join(HERMES_HOME, "workspace", "trading_state.json")
REGIME_FILE = os.path.join(HERMES_HOME, "workspace", "market_regime.json")

# Bybit API
env_file = os.path.join(HERMES_HOME, ".env")
if os.path.exists(env_file):
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
DEMO_BASE = "https://api-demo.bybit.com"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL_NAME = os.environ.get("REGIME_MODEL", "qwen3.5:9b")

WEIGHTS = {
    "macro": 0.35,
    "etf": 0.25,
    "onchain": 0.20,
    "sentiment": 0.10,
    "liquidity": 0.10,
}

def bybit_request(endpoint, query=""):
    ts = str(int(time.time() * 1000))
    sign = hmac.new(BYBIT_API_SECRET.encode(), f"{ts}{BYBIT_API_KEY}5000{query}".encode(), hashlib.sha256).hexdigest()
    url = f"{DEMO_BASE}{endpoint}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [Bybit API error] {endpoint}: {e}")
        return {}

def get_price():
    r = bybit_request("/v5/market/tickers", "category=linear&symbol=BTCUSDT")
    if r.get("result") and r["result"].get("list"):
        t = r["result"]["list"][0]
        return {
            "price": float(t.get("lastPrice", 0)),
            "change_24h": float(t.get("price24hPcnt", 0)) * 100,
            "high_24h": float(t.get("highPrice24h", 0)),
            "low_24h": float(t.get("lowPrice24h", 0)),
            "volume_24h": float(t.get("turnover24h", 0)),
        }
    return None

def get_klines(interval="60", limit=100):
    r = bybit_request("/v5/market/kline", f"category=linear&symbol=BTCUSDT&interval={interval}&limit={limit}")
    if r.get("result") and r["result"].get("list"):
        return [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                 "close": float(k[4]), "volume": float(k[5])}
                for k in r["result"]["list"]]
    return []

def get_funding():
    r = bybit_request("/v5/market/funding/history", "category=linear&symbol=BTCUSDT&limit=1")
    if r.get("result") and r["result"].get("list"):
        return float(r["result"]["list"][0].get("fundingRate", 0))
    return 0

def get_open_interest():
    r = bybit_request("/v5/market/open-interest", "category=linear&symbol=BTCUSDT&intervalTime=1h&limit=1")
    if r.get("result") and r["result"].get("list"):
        return float(r["result"]["list"][0].get("openInterest", 0))
    return 0

def get_dxy():
    """Получить индекс доллара (DXY) через прокси — полная формула"""
    try:
        req = urllib.request.Request(
            "https://api.exchangerate.host/latest?base=USD&symbols=EUR,GBP,JPY",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            if data.get("rates"):
                eur = data["rates"].get("EUR", 1)
                gbp = data["rates"].get("GBP", 1)
                jpy = data["rates"].get("JPY", 1)
                # Полная формула DXY: 50.14 x EUR^(-0.576) x JPY^(0.136) x GBP^(-0.119)
                dxy = 50.14348112 * (eur ** -0.576) * (jpy ** 0.136) * (gbp ** -0.119)
                return round(dxy, 2)
    except Exception as e:
        print(f"  [DXY error] {e}")
    return None

def get_fear_greed_index():
    """Получить Fear & Greed Index"""
    try:
        req = urllib.request.Request(
            "https://api.alternative.me/fng/?limit=7",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            if data.get("data"):
                return {
                    "current": int(data["data"][0]["value"]),
                    "classification": data["data"][0]["value_classification"],
                    "history": [{"value": int(d["value"]), "date": d["timestamp"]} for d in data["data"][:7]]
                }
    except Exception as e:
        print(f"  [F&G error] {e}")
    return None

def get_btc_dominance():
    """Получить доминацию BTC через CoinGecko"""
    try:
        req = urllib.request.Request(
            "https://api.coingecko.com/api/v3/global",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            if data.get("data"):
                return {
                    "btc_dominance": data["data"].get("market_cap_percentage", {}).get("btc", 0),
                    "total_market_cap": data["data"].get("total_market_cap", {}).get("usd", 0),
                    "total_volume": data["data"].get("total_volume", {}).get("usd", 0),
                }
    except Exception as e:
        print(f"  [Dominance error] {e}")
    return None

def get_etf_flows():
    """Получить данные по рыночному сентименту и потокам капитала"""
    result = {}
    
    # Fear & Greed Index
    fng = get_fear_greed_index()
    if fng:
        result["fear_greed"] = fng
    
    # BTC Dominance
    dominance = get_btc_dominance()
    if dominance:
        result["dominance"] = dominance
    
    # Оценка потоков на основе FNG и dominance
    if fng and dominance:
        fng_value = fng["current"]
        btc_dom = dominance["btc_dominance"]
        
        # Простая эвристика для оценки потоков
        if fng_value < 25 and btc_dom > 50:
            result["flow_signal"] = "strong_inflow"  # Страх + высокая доминация = приток в BTC
        elif fng_value < 40:
            result["flow_signal"] = "mild_inflow"    # Страх = приток
        elif fng_value > 70:
            result["flow_signal"] = "mild_outflow"   # Жадность = отток
        elif fng_value > 85:
            result["flow_signal"] = "strong_outflow"  # Экстремальная жадность
        else:
            result["flow_signal"] = "neutral"
    
    return result if result else {"source": "unavailable"}

def ema(prices, period):
    if len(prices) < period:
        return None
    m = 2 / (period + 1)
    e = sum(prices[:period]) / period
    for p in prices[period:]:
        e = (p - e) * m + e
    return e

def rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    d = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    g = [x if x > 0 else 0 for x in d]
    l = [-x if x < 0 else 0 for x in d]
    ag = sum(g[-period:]) / period
    al = sum(l[-period:]) / period
    return 100 - (100 / (1 + ag / al)) if al > 0 else 100

def atr(klines, period=14):
    if len(klines) < period:
        return None
    trs = []
    for i in range(1, len(klines)):
        tr = max(klines[i]["high"] - klines[i]["low"],
                 abs(klines[i]["high"] - klines[i - 1]["close"]),
                 abs(klines[i]["low"] - klines[i - 1]["close"]))
        trs.append(tr)
    return sum(trs[-period:]) / period if trs else None

def volume_ratio(klines, period=20):
    if len(klines) < period:
        return 1.0
    vols = [k["volume"] for k in klines[-period:]]
    avg = sum(vols) / len(vols)
    return klines[-1]["volume"] / avg if avg > 0 else 1.0

def fetch_news():
    """Fetch latest BTC news from RSS feeds"""
    sys.path.insert(0, os.path.join(HERMES_HOME, "scripts"))
    try:
        from btc_news_rss import check_btc_news
        news = check_btc_news()
        return [f"{n['title']} ({n['source']})" for n in news]
    except Exception as e:
        print(f"  [RSS error] {e}")
        return []

def load_regime():
    if os.path.exists(REGIME_FILE):
        try:
            with open(REGIME_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "regime": "NEUTRAL",
        "confidence": 50,
        "allowed_direction": "WAIT",
        "risk_pct": 0.25,
        "scores": {},
        "reason": [],
        "updated_at": None,
    }

def save_regime(data):
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(REGIME_FILE), exist_ok=True)
    with open(REGIME_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def calc_weighted_score(scores):
    total = 0
    total_weight = 0
    for factor, score in scores.items():
        weight = WEIGHTS.get(factor, 0)
        total += score * weight
        total_weight += weight
    return total / total_weight if total_weight > 0 else 0

def score_to_regime(weighted_score, confidence):
    if confidence < 40:
        return "NEUTRAL", "WAIT", 0.25
    if weighted_score >= 60:
        return "STRONG_BULL", "LONG", 0.75
    elif weighted_score >= 30:
        return "BULL", "LONG", 0.50
    elif weighted_score >= -30:
        return "NEUTRAL", "WAIT", 0.25
    elif weighted_score >= -60:
        return "BEAR", "SHORT", 0.50
    else:
        return "STRONG_BEAR", "SHORT", 0.75

def get_high_impact_events():
    """Определить высоковлиятельные события (приблизительный календарь)"""
    now = datetime.now(timezone.utc)
    events = []
    day = now.day
    weekday = now.weekday()  # 0=Пн, 4=Пт
    
    # NFP (Non-Farm Payrolls) — первый пятничный день месяца
    if weekday == 4 and day <= 7:
        events.append("NFP (Non-Farm Payrolls)")
    
    # CPI (Consumer Price Index) — примерно 10-13 число
    if 10 <= day <= 13:
        events.append("CPI (Consumer Price Index)")
    
    # FOMC — 8 раз в год, обычно среда, 19:00 UTC
    # Упрощённо: проверяем среду + вторую/четвёртую неделю месяца
    if weekday == 2 and (8 <= day <= 14 or 22 <= day <= 28):
        events.append("FOMC potential")
    
    # GTC / Другие события — можно добавить позже
    
    return events

def build_prompt(market_data, news, regime):
    events = get_high_impact_events()
    ema21_1h = market_data.get("ema21_1h") or 0
    ema55_1h = market_data.get("ema55_1h") or 0
    ema21_4h = market_data.get("ema21_4h") or 0
    ema55_4h = market_data.get("ema55_4h") or 0
    rsi_1h = market_data.get("rsi_1h") or 50
    atr_1h = market_data.get("atr_1h") or 0

    trend_1h = "bullish" if ema21_1h > ema55_1h and ema21_1h > 0 else \
               "bearish" if ema21_1h < ema55_1h and ema55_1h > 0 else "neutral"
    trend_4h = "bullish" if ema21_4h > ema55_4h and ema21_4h > 0 else \
               "bearish" if ema21_4h < ema55_4h and ema55_4h > 0 else "neutral"

    news_str = "\n".join(f"- {n}" for n in news) if news else "- Нет свежих новостей"

    dxy_val = market_data.get("dxy")
    dxy_str = f"{dxy_val:.2f}" if dxy_val else "N/A"
    
    etf_data = market_data.get("etf_flows", {})
    fng = etf_data.get("fear_greed", {})
    dominance = etf_data.get("dominance", {})
    flow_signal = etf_data.get("flow_signal", "N/A")
    fng_value = fng.get("current", "N/A") if fng else "N/A"
    btc_dom = f"{dominance.get('btc_dominance', 0):.1f}%" if dominance else "N/A"
    
    return f"""Определи Market Regime для BTC/USDT на основе реальных данных рынка.

## ТЕКУЩИЕ ДАННЫЕ РЫНКА:
- Цена BTC: ${market_data['price']:,.2f}
- 24h изменение: {market_data['change_24h']:+.2f}%
- 24h High/Low: ${market_data['high_24h']:,.2f} / ${market_data['low_24h']:,.2f}
- Тренд 1H: {trend_1h} (EMA21={ema21_1h:,.2f} vs EMA55={ema55_1h:,.2f})
- Тренд 4H: {trend_4h} (EMA21={ema21_4h:,.2f} vs EMA55={ema55_4h:,.2f})
- RSI(14) 1H: {rsi_1h:.1f}
- ATR(14) 1H: ${atr_1h:,.1f}
- Volume ratio: {market_data.get('volume_ratio', 1.0):.2f}x average
- Funding rate: {market_data.get('funding', 0):.6f}%
- Open Interest: {market_data.get('oi', 0):,.0f} контрактов
- DXY (индекс доллара): {dxy_str}
- Fear & Greed Index: {fng_value}
- BTC Dominance: {btc_dom}
- Flow Signal: {flow_signal}

## СВЕЖИЕ НОВОСТИ:
{news_str}

## ВАЖНЫЕ СОБЫТИЯ:
{', '.join(events) if events else 'Нет событий'}

## ПРЕДЫДУЩИЙ РЕЖИМ:
{regime['regime']} (confidence: {regime['confidence']}%)
Обновлён: {regime.get('updated_at', 'никогда')}

## ЗАДАЧА:
Проанализируй данные и определи рыночный режим. Используй 4-вопросный фреймворк:
1. Что произошло? (событие на рынке)
2. Что ожидал рынок? (консенсус)
3. Как отреагировал? (цена + объём + OI)
4. Что это значит? (баланс сил быков и медведей)

ОТВЕТЬ СТРОГО В JSON БЕЗ ЛИШНЕГО ТЕКСТА:
{{
  "macro_score": число от -100 до +100,
  "etf_score": число от -100 до +100,
  "onchain_score": число от -100 до +100,
  "sentiment_score": число от -100 до +100,
  "liquidity_score": число от -100 до +100,
  "confidence": число от 0 до 100,
  "reason": ["причина 1", "причина 2"]
}}

Правила оценок:
- macro_score: ФРС ставка, CPI инфляция, NFP занятость, геополитика, DXY доллар, US10Y облигации
- etf_score: притоки/оттоки BTC ETF (BlackRock IBIT, Fidelity, Grayscale GBTC)
- onchain_score: движение китов, exchange reserves, MVRV Z-score, NVT ratio
- sentiment_score: Fear&Greed Index, social volume, funding rate, доминация BTC
- liquidity_score: объём торгов, open interest, глубина стакана, спред bid/ask"""

def call_ollama(prompt):
    """Call Ollama API with the prompt (think: false to get direct JSON output)"""
    data = json.dumps({
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 1024},
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read().decode())
            return result.get("response", "")
    except Exception as e:
        print(f"  [Ollama error] {e}")
        return None

def parse_llm_response(response):
    """Parse JSON from LLM response (handles thinking mode from qwen3.5)"""
    if not response:
        return None
    import re

    # 1. Try to find JSON in code blocks
    code_block = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', response, re.DOTALL)
    if code_block:
        try:
            parsed = json.loads(code_block.group(1))
            if "macro_score" in parsed or "confidence" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    # 2. Try to find complete JSON object with all required fields
    # Look for the pattern with macro_score, etf_score, etc.
    json_pattern = r'\{[^{}]*"macro_score"[^{}]*"etf_score"[^{}]*"confidence"[^{}]*\}'
    matches = re.findall(json_pattern, response, re.DOTALL)
    for match in matches:
        try:
            parsed = json.loads(match)
            if "macro_score" in parsed and "confidence" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    # 3. Fallback: find any JSON object
    start = response.find("{")
    while start >= 0:
        end = response.rfind("}", start)
        if end < 0:
            break
        candidate = response[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if "macro_score" in parsed or "confidence" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass
        start = response.find("{", start + 1)

    print(f"  [JSON parse error] No valid JSON found")
    return None

def collect_market_data():
    """Collect all market data from Bybit"""
    print("Сбор данных с Bybit...")

    price_data = get_price()
    if not price_data:
        print("  Ошибка получения цены")
        return None

    klines_1h = get_klines("60", 100)
    klines_4h = get_klines("240", 50)

    closes_1h = [k["close"] for k in klines_1h] if klines_1h else []
    closes_4h = [k["close"] for k in klines_4h] if klines_4h else []

    funding = get_funding()
    oi = get_open_interest()

    # Получить DXY и ETF данные
    dxy = get_dxy()
    etf = get_etf_flows()
    
    data = {
        "price": price_data["price"],
        "change_24h": price_data["change_24h"],
        "high_24h": price_data["high_24h"],
        "low_24h": price_data["low_24h"],
        "ema21_1h": ema(closes_1h, 21),
        "ema55_1h": ema(closes_1h, 55),
        "ema21_4h": ema(closes_4h, 21),
        "ema55_4h": ema(closes_4h, 55),
        "rsi_1h": rsi(closes_1h),
        "atr_1h": atr(klines_1h) if klines_1h else None,
        "volume_ratio": volume_ratio(klines_1h) if klines_1h else 1.0,
        "funding": funding,
        "oi": oi,
        "dxy": dxy,
        "etf_flows": etf,
    }

    print(f"  Цена: ${data['price']:,.2f} ({data['change_24h']:+.2f}%)")
    print(f"  Trend 1H: {'BULL' if data['ema21_1h'] and data['ema55_1h'] and data['ema21_1h'] > data['ema55_1h'] else 'BEAR'}")
    print(f"  RSI: {data['rsi_1h']:.1f}" if data['rsi_1h'] else "  RSI: N/A")
    print(f"  Funding: {data['funding']:.6f}%")
    print(f"  OI: {data['oi']:,.0f}")

    return data

def main():
    print(f"=== Market Regime Analyzer ===")
    print(f"Время: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        print("❌ BYBIT_API_KEY/SECRET не настроены!")
        return

    # Load previous regime
    old_regime = load_regime()
    print(f"\nПредыдущий режим: {old_regime['regime']} ({old_regime['confidence']}%)")

    # Collect market data
    market_data = collect_market_data()
    if not market_data:
        print("❌ Не удалось собрать данные рынка")
        return

    # Fetch news
    print("\nЗагрузка новостей...")
    news = fetch_news()
    print(f"  Получено {len(news)} новостей")

    # Build prompt
    prompt = build_prompt(market_data, news, old_regime)

    # Call Ollama
    print(f"\nЗапрос к {MODEL_NAME}...")
    response = call_ollama(prompt)
    if not response:
        print("❌ Ollama не ответил")
        return

    print(f"  Получен ответ ({len(response)} символов)")

    # Parse response - look for JSON in thinking mode output
    scores = parse_llm_response(response)
    if not scores:
        # Try to extract from thinking text
        import re
        json_pattern = r'\{[^{}]*"macro_score"[^{}]*\}'
        match = re.search(json_pattern, response)
        if match:
            try:
                scores = json.loads(match.group())
            except:
                pass
    if not scores:
        print("❌ Не удалось распарсить ответ LLM")
        print(f"  Ответ (первые 500 символов): {response[:500]}")
        return

    print(f"\nОценки LLM:")
    for k in ["macro_score", "etf_score", "onchain_score", "sentiment_score", "liquidity_score"]:
        print(f"  {k}: {scores.get(k, 0)}")
    print(f"  confidence: {scores.get('confidence', 0)}")

    # Calculate weighted score
    score_map = {
        "macro": scores.get("macro_score", 0),
        "etf": scores.get("etf_score", 0),
        "onchain": scores.get("onchain_score", 0),
        "sentiment": scores.get("sentiment_score", 0),
        "liquidity": scores.get("liquidity_score", 0),
    }
    weighted = calc_weighted_score(score_map)
    confidence = scores.get("confidence", 50)

    # Determine regime
    regime, allowed, risk_pct = score_to_regime(weighted, confidence)

    print(f"\n=== РЕЗУЛЬТАТ ===")
    print(f"Weighted Score: {weighted:+.1f}")
    print(f"Regime: {regime}")
    print(f"Allowed: {allowed}")
    print(f"Risk: {risk_pct}")
    print(f"Confidence: {confidence}%")
    print(f"Reasons: {scores.get('reason', [])}")

    # Save regime
    regime_data = {
        "regime": regime,
        "confidence": confidence,
        "allowed_direction": allowed,
        "risk_pct": risk_pct,
        "scores": score_map,
        "weighted_score": round(weighted, 1),
        "reason": scores.get("reason", []),
        "market_snapshot": {
            "price": market_data["price"],
            "change_24h": market_data["change_24h"],
            "rsi_1h": market_data.get("rsi_1h"),
            "funding": market_data["funding"],
            "oi": market_data["oi"],
        },
    }
    save_regime(regime_data)
    print(f"\n✅ Сохранено в {REGIME_FILE}")

if __name__ == "__main__":
    main()
