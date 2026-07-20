#!/usr/bin/env python3
"""Bybit BTC Auto Trader v10 - EMA, RSI, MACD, Bollinger, ATR, StochRSI, OI, Funding"""
import os, time, json, hashlib, hmac, urllib.request
import yaml
from datetime import datetime, timedelta, timezone

HERMES_HOME = os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes/profiles/trader'))
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.yaml")

def load_config():
    """Загрузить конфигурацию из config.yaml"""
    try:
        with open(CONFIG_FILE, "r") as f:
            return yaml.safe_load(f)
    except (IOError, OSError):
        return {}
STATE_FILE = os.path.join(HERMES_HOME, 'workspace', 'trading_state.json')
REGIME_FILE = os.path.join(HERMES_HOME, 'workspace', 'market_regime.json')

env_file = os.path.join(HERMES_HOME, '.env')
if os.path.exists(env_file):
    with open(env_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()

BYBIT_API_KEY = os.environ.get('BYBIT_API_KEY', '')
BYBIT_API_SECRET = os.environ.get('BYBIT_API_SECRET', '')
DEMO_BASE = 'https://api-demo.bybit.com'
RISK_RULES = {
    'max_risk_percent': 0.5,
    'min_rr_ratio': 2.0,
    'max_positions': 1,
    'max_daily_losses': 2,
    'force_close_loss_pct': -1.0,
    'take_profit_alert_pct': 5.0,
    'daily_loss_limit_pct': 3.0,      # Макс. дневной убыток % от баланса
    'weekly_loss_limit_pct': 7.0,     # Макс. недельный убыток % от баланса
    'max_drawdown_pct': 15.0,         # Макс. просадка от пика % - Kill Switch
    'position_max_hours': 48,         # Макс. время позиции (часы) без прибыли
    'min_volume_ratio': 1.25,           # Мин. объём для входа
    'max_daily_trades': 2,              # Макс. сделок в день
}
TRAILING = {
    # Основные настройки
    'enabled': True,
    'activation_r': 1.0,               # Начинать трейлинг после +1.0R
    
    # SL/TP расстояния
    'sl_distance_pct': 1.5,            # SL = цена - 1.5%
    'tp_distance_pct': 2.0,            # TP = цена + 2.0%
    
    # Режим трейлинга
    'trail_only_up': True,             # Двигать только вверх
    
    # Partial Close
    'partial_close_r': 1.7,            # Частичная фиксация при +1.7R
    'partial_close_pct': 35,           # Закрыть 35% позиции
    'partial_close_2_r': 3.0,          # Вторая фиксация при +3R
    'partial_close_2_pct': 30,         # Закрыть ещё 30%
    
    # Volatility
    'vol_high_factor': 0.7,            # High vol: -30% размера
    'vol_low_factor': 1.2,             # Low vol: +20% размера
    
    # Regime Multiplier
    'regime_multiplier': {
        'STRONG_BULL': 1.2,            # Даём тренду больше пространства
        'BULL': 1.0,                   # Стандарт
        'NEUTRAL': 0.6,                # Tighter в боковике
        'BEAR': 1.0,                   # Стандарт
        'STRONG_BEAR': 1.2,            # Больше пространства для шортов
    },
}

def manage_trailing_stop(positions, analysis):
    """Гибридный Trailing Stop + TP с regime multiplier"""
    if not positions or not analysis or not TRAILING["enabled"]:
        return
    
    # Получить regime
    regime = load_regime()
    regime_name = regime.get("regime", "NEUTRAL")
    
    # === REGIME FILTER ===
    if regime_name in ["NEUTRAL", "RANGE", "STRONG_BEAR"]:
        return  # Не трейлим в этих режимах
    
    multiplier = TRAILING["regime_multiplier"].get(regime_name, 1.0)
    
    atr_val = analysis.get('atr') or 200
    state = load_state()
    
    for p in positions:
        entry = p['entry']
        mark = p['mark']
        side = p['side']
        
        # Рассчитать R
        risk_per_unit = atr_val * 1.9
        if risk_per_unit <= 0:
            continue
        
        if side == 'Buy':
            r_multiple = (mark - entry) / risk_per_unit
        else:
            r_multiple = (entry - mark) / risk_per_unit
        
        # Проверка: трейлинг активируется при +1.0R
        if r_multiple < TRAILING['activation_r']:
            continue
        
        # Рассчитать расстояния с regime multiplier
        sl_dist = TRAILING['sl_distance_pct'] * multiplier
        tp_dist = TRAILING['tp_distance_pct'] * multiplier
        
        # Rate limit: проверяем интервал между обновлениями
        last_trail_key = f"last_trail_{p['entry']}"
        last_trail_time = state.get(last_trail_key, 0)
        if time.time() - last_trail_time < TRAIL_COOLDOWN:
            continue  # Пропустить, если прошло менее TRAIL_COOLDOWN секунд

        # === TRAILING SL ===
        if side == "Buy":
            new_sl = mark * (1 - sl_dist / 100)
        else:
            new_sl = mark * (1 + sl_dist / 100)
        
        current_sl = p.get("stopLoss", 0)
        sl_valid = (side == "Buy" and new_sl > entry and (current_sl == 0 or new_sl > current_sl * 1.001)) or \
                   (side == "Sell" and new_sl < entry and (current_sl == 0 or new_sl < current_sl * 0.999))
        
        # === TRAILING TP ===
        if side == "Buy":
            new_tp = mark * (1 + tp_dist / 100)
        else:
            new_tp = mark * (1 - tp_dist / 100)
        
        # Один запрос API для SL + TP
        if sl_valid:
            try:
                result = set_trading_stop(sl=new_sl, tp=new_tp)
                if result.get("retCode") == 0:
                    dist_to_tp = abs(new_tp - mark) / mark * 100
                    log_msg = f"[{regime_name} x{multiplier:.1f}] +{r_multiple:.1f}R | SL ${new_sl:,.2f} ({sl_dist:.1f}%) | TP ${new_tp:,.2f} ({dist_to_tp:.1f}%)"
                    print(f"   🔒 Trailing {log_msg}")
                    # Запись в лог-файл
                    os.makedirs(os.path.expanduser("~/.hermes/profiles/trader/logs"), exist_ok=True)
                    with open(os.path.expanduser("~/.hermes/profiles/trader/logs/trailing.log"), "a") as lf:
                        lf.write(f"{datetime.now().isoformat()} | {side} {p['size']} BTC @ ${entry:,.2f} | {log_msg}\n")
                    state[last_trail_key] = time.time()
                else:
                    print(f"   ⚠️ Trailing failed: {result.get('retMsg', 'unknown error')}")
            except Exception as e:
                print(f"   ❌ Trailing error: {e}")
        
        # === PARTIAL CLOSE ===
        # 1. При +1.7R → 35%
        if r_multiple >= TRAILING['partial_close_r']:
            partial_key = f"partial_closed_{p['entry']}"
            state = load_state()
            if not state.get(partial_key):
                partial_qty = round(p['size'] * TRAILING['partial_close_pct'] / 100, 3)
                if partial_qty >= 0.001:
                    close_result = close_position(side, partial_qty)
                    if close_result.get('retCode') == 0:
                        print(f"   📊 Частичная фиксация: {TRAILING['partial_close_pct']}% ({partial_qty} BTC) при +{r_multiple:.1f}R")
                        state[partial_key] = True
                        state['trades'].append({
                            'time': datetime.now().isoformat(),
                            'action': 'PARTIAL_CLOSE',
                            'closed_pnl': p['pnl'] * TRAILING['partial_close_pct'] / 100,
                            'reason': f'partial_close_at_{r_multiple:.1f}R',
                            'side': side,
                            'qty': partial_qty,
                            'price': mark
                        })
                        save_state(state)
        
        # 2. При +3R → 30%
        partial_key_2 = f"partial_closed_2_{p['entry']}"
        if r_multiple >= TRAILING['partial_close_2_r'] and not state.get(partial_key_2):
            state = load_state()
            if not state.get(partial_key_2):
                partial_qty = round(p['size'] * TRAILING['partial_close_2_pct'] / 100, 3)
                if partial_qty >= 0.001:
                    close_result = close_position(side, partial_qty)
                    if close_result.get('retCode') == 0:
                        print(f"   📊 2-я фиксация: {TRAILING['partial_close_2_pct']}% ({partial_qty} BTC) при +{r_multiple:.1f}R")
                        state[partial_key_2] = True
                        state['trades'].append({
                            'time': datetime.now().isoformat(),
                            'action': 'PARTIAL_CLOSE_2',
                            'closed_pnl': p['pnl'] * TRAILING['partial_close_2_pct'] / 100,
                            'reason': f'partial_close_2_at_{r_multiple:.1f}R',
                            'side': side,
                            'qty': partial_qty,
                            'price': mark
                        })
                        save_state(state)
    
    # Сохранить состояние после обработки всех позиций
    save_state(state)


LOSS_COOLDOWN_HOURS = 4  # Пауза после серии убытков (часы)
_regime_cache = None  # Кэш для fallback regime
_regime_cache_time = 0  # Время последнего обновления кэша
TRAIL_COOLDOWN = 60  # Минимальный интервал между обновлениями trailing (секунды)

# Trailing Stop настройки

def bybit_request(endpoint, params=None, data=None, max_retries=3):
    """Запрос к Bybit API с retry и exponential backoff"""
    if not BYBIT_API_KEY or not BYBIT_API_SECRET: return {'error': 'No API keys'}
    
    for attempt in range(max_retries):
        try:
            ts = str(int(time.time() * 1000))
            q = '&'.join([f'{k}={v}' for k, v in sorted(params.items())]) if params else ''
            body = json.dumps(data) if data else ''
            sign = hmac.new(BYBIT_API_SECRET.encode(), f"{ts}{BYBIT_API_KEY}5000{q}{body}".encode(), hashlib.sha256).hexdigest()
            url = f'{DEMO_BASE}{endpoint}' + (f'?{q}' if q else '')
            h = {'X-BAPI-API-KEY': BYBIT_API_KEY, 'X-BAPI-SIGN': sign, 'X-BAPI-TIMESTAMP': ts, 'X-BAPI-RECV-WINDOW': '5000', 'Content-Type': 'application/json'}
            req = urllib.request.Request(url, headers=h, method='POST' if data else 'GET')
            if data: req.data = body.encode()
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                print(f"   ⚠️ API retry {attempt + 1}/{max_retries}: {e} (wait {wait_time}s)")
                time.sleep(wait_time)
            else:
                return {'error': str(e)}
    return {'error': 'Max retries exceeded'}

def get_balance():
    r = bybit_request('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
    if r.get('result'):
        for c in r['result'].get('list', [{}])[0].get('coin', []):
            if c['coin'] == 'USDT':
                return {'equity': float(c.get('equity', 0) or 0), 'pnl': float(c.get('unrealisedPnl', 0) or 0)}
    return None

def get_positions():
    r = bybit_request('/v5/position/list', {'category': 'linear', 'symbol': 'BTCUSDT'})
    positions = []
    if r.get('result') and r['result'].get('list'):
        for p in r['result']['list']:
            sz = float(p.get('size', 0) or 0)
            if sz > 0:
                positions.append({'side': p['side'], 'size': sz, 'entry': float(p.get('avgPrice', 0) or 0),
                    'mark': float(p.get('markPrice', 0) or 0), 'pnl': float(p.get('unrealisedPnl', 0) or 0),
                    'liq': float(p.get('liqPrice', 0) or 0), 'open_time': p.get('createdTime', ''), 'stopLoss': float(p.get('stopLoss', 0) or 0)})
    return positions

def get_btc_price():
    r = bybit_request('/v5/market/tickers', {'category': 'linear', 'symbol': 'BTCUSDT'})
    if r.get('result') and r['result'].get('list'):
        t = r['result']['list'][0]
        return {'price': float(t.get('lastPrice', 0)), 'change': float(t.get('price24hPcnt', 0)) * 100}
    return None

def get_klines(interval='15', limit=100):
    r = bybit_request('/v5/market/kline', {'category': 'linear', 'symbol': 'BTCUSDT', 'interval': interval, 'limit': str(limit)})
    if r.get('result') and r['result'].get('list'):
        return [{'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])} for k in r['result']['list']]
    return []

def get_funding_rate():
    r = bybit_request('/v5/market/funding/history', {'category': 'linear', 'symbol': 'BTCUSDT', 'limit': '1'})
    if r.get('result') and r['result'].get('list'): return float(r['result']['list'][0].get('fundingRate', 0))
    return 0

def get_open_interest():
    r = bybit_request('/v5/market/open-interest', {'category': 'linear', 'symbol': 'BTCUSDT', 'intervalTime': '1h', 'limit': '1'})
    if r.get('result') and r['result'].get('list'): return float(r['result']['list'][0].get('openInterest', 0))
    return 0

def ema(prices, period):
    if len(prices) < period: return None
    m = 2 / (period + 1); e = sum(prices[:period]) / period
    for p in prices[period:]: e = (p - e) * m + e
    return e

def rsi(prices, period=14):
    if len(prices) < period + 1: return None
    d = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    g = [x if x > 0 else 0 for x in d]; l = [-x if x < 0 else 0 for x in d]
    ag = sum(g[-period:]) / period; al = sum(l[-period:]) / period
    return 100 - (100 / (1 + ag / al)) if al > 0 else 100

def stochastic_rsi(prices, rsi_period=14, stoch_period=14):
    """Улучшенный Stochastic RSI с кэшированием RSI"""
    if len(prices) < rsi_period + stoch_period:
        return None, None
    rsi_vals = []
    for i in range(rsi_period + 1, len(prices) + 1):
        r = rsi(prices[:i], rsi_period)
        if r is not None: rsi_vals.append(r)
    if len(rsi_vals) < stoch_period: return None, None
    recent_rsi = rsi_vals[-stoch_period:]
    rsi_min = min(recent_rsi); rsi_max = max(recent_rsi)
    if rsi_max - rsi_min == 0: return 50, 50
    k = (rsi_vals[-1] - rsi_min) / (rsi_max - rsi_min) * 100
    d = sum(rsi_vals[-3:]) / 3 if len(rsi_vals) >= 3 else k
    return k, d
def macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow: return None, None, None
    ef = ema(prices, fast); es = ema(prices, slow)
    if ef is None or es is None: return None, None, None
    ml = ef - es
    mvs = []
    for i in range(slow, len(prices) + 1):
        fast_ema = ema(prices[:i], fast)
        slow_ema = ema(prices[:i], slow)
        if fast_ema is not None and slow_ema is not None:
            mvs.append(fast_ema - slow_ema)
    if len(mvs) >= signal:
        sl = sum(mvs[-signal:]) / signal
        return ml, sl, ml - sl
    return ml, None, None

def bollinger(prices, period=20, std_dev=2):
    if len(prices) < period: return None, None, None
    s = sum(prices[-period:]) / period
    std = (sum((p - s) ** 2 for p in prices[-period:]) / period) ** 0.5
    return s + std_dev * std, s, s - std_dev * std

def atr(klines, period=14):
    if len(klines) < period: return None
    trs = []
    for i in range(1, len(klines)):
        tr = max(klines[i]['high'] - klines[i]['low'], abs(klines[i]['high'] - klines[i-1]['close']), abs(klines[i]['low'] - klines[i-1]['close']))
        trs.append(tr)
    return sum(trs[-period:]) / period if trs else None


def adx(klines, period=14):
    """Расчёт ADX с Wilder smoothing (стандарт для ADX)"""
    if len(klines) < period * 2:
        return None, None, None
    
    # True Range, +DM, -DM
    trs = []
    plus_dm = []
    minus_dm = []
    
    for i in range(1, len(klines)):
        tr = max(
            klines[i]["high"] - klines[i]["low"],
            abs(klines[i]["high"] - klines[i-1]["close"]),
            abs(klines[i]["low"] - klines[i-1]["close"])
        )
        trs.append(tr)
        
        up_move = klines[i]["high"] - klines[i-1]["high"]
        down_move = klines[i-1]["low"] - klines[i]["low"]
        
        plus_dm.append(max(up_move, 0) if up_move > down_move else 0)
        minus_dm.append(max(down_move, 0) if down_move > up_move else 0)
    
    if len(trs) < period:
        return None, None, None
    
    # Wilder smoothing для ATR, +DM, -DM
    atr_val = sum(trs[:period]) / period
    smooth_plus_dm = sum(plus_dm[:period]) / period
    smooth_minus_dm = sum(minus_dm[:period]) / period
    
    dx_values = []
    
    for i in range(period, len(trs)):
        # Wilder smoothing
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        smooth_plus_dm = (smooth_plus_dm * (period - 1) + plus_dm[i]) / period
        smooth_minus_dm = (smooth_minus_dm * (period - 1) + minus_dm[i]) / period
        
        # +DI / -DI
        plus_di = 100 * smooth_plus_dm / atr_val if atr_val > 0 else 0
        minus_di = 100 * smooth_minus_dm / atr_val if atr_val > 0 else 0
        
        # DX
        if plus_di + minus_di == 0:
            dx = 0
        else:
            dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
        dx_values.append(dx)
    
    # ADX = Wilder smoothed DX
    if len(dx_values) >= period:
        adx_val = sum(dx_values[:period]) / period
        for dx in dx_values[period:]:
            adx_val = (adx_val * (period - 1) + dx) / period
        return adx_val, plus_di, minus_di
    
    return None, None, None
def volume_ratio(klines, period=20):
    if len(klines) < period: return 1.0
    vols = [k['volume'] for k in klines[-period:]]
    avg = sum(vols) / len(vols)
    return vols[-1] / avg if avg > 0 else 1.0

def recent_swing_low(klines, lookback=20):
    if not klines: return None
    window = klines[-lookback:] if len(klines) >= lookback else klines
    return min(k['low'] for k in window)

def recent_swing_high(klines, lookback=20):
    if not klines: return None
    window = klines[-lookback:] if len(klines) >= lookback else klines
    return max(k['high'] for k in window)

def same_direction_position_exists(positions, action):
    side = 'Buy' if action == 'LONG' else 'Sell'
    return any(p['side'] == side for p in positions)

def count_recent_losses(trades, limit=10):
    """Посчитать последние убытки и проверить cooldown"""
    losses = 0
    last_loss_time = None
    for trade in reversed(trades[-limit:]):
        pnl = trade.get('closed_pnl')
        if pnl is None:
            continue
        if pnl < 0:
            losses += 1
            if last_loss_time is None:
                last_loss_time = trade.get('time')
        else:
            break
    
    # Если серия убытков - проверяем cooldown
    if losses >= RISK_RULES['max_daily_losses'] and last_loss_time:
        try:
            loss_time = datetime.fromisoformat(last_loss_time.replace('Z', '+00:00'))
            # Убедиться что timezone-aware
            if loss_time.tzinfo is None:
                loss_time = loss_time.replace(tzinfo=timezone.utc)
            cooldown_end = loss_time + timedelta(hours=LOSS_COOLDOWN_HOURS)
            now = datetime.now(timezone.utc)
            if now < cooldown_end:
                return losses  # Еще на cooldown
            else:
                return 0  # Cooldown прошел, можно торговать
        except (IOError, OSError):
            return losses
    
    return losses

def fallback_regime(klines):
    """Fallback regime без Ollama (чисто математический: EMA + RSI + volume)"""
    if not klines or len(klines) < 50:
        return {"regime": "NEUTRAL", "confidence": 50, "allowed_direction": "WAIT", "risk_pct": 0.25}
    
    closes = [k['close'] for k in klines]
    e21 = ema(closes, 21)
    e55 = ema(closes, 55)
    e200 = ema(closes, 200) if len(closes) >= 200 else None
    r = rsi(closes)
    vr = volume_ratio(klines)
    adx_val, _, _ = adx(klines)
    
    score = 0
    reasons = []
    
    # EMA тренд
    if e21 and e55:
        if e21 > e55:
            score += 20
            reasons.append("EMA21 > EMA55 (bullish)")
        else:
            score -= 20
            reasons.append("EMA21 < EMA55 (bearish)")
    
    # EMA vs EMA200
    if e55 and e200:
        if e55 > e200:
            score += 15
            reasons.append("EMA55 > EMA200 (strong bullish)")
        else:
            score -= 15
            reasons.append("EMA55 < EMA200 (strong bearish)")
    
    # RSI
    if r:
        if r > 70:
            score -= 15
            reasons.append(f"RSI {r:.0f} (overbought)")
        elif r < 30:
            score += 15
            reasons.append(f"RSI {r:.0f} (oversold)")
    
    # Volume
    if vr > 1.2:
        score += 10
        reasons.append(f"Volume {vr:.1f}x (high)")
    elif vr < 0.8:
        score -= 10
        reasons.append(f"Volume {vr:.1f}x (low)")
    
    # ADX
    if adx_val:
        if adx_val > 25:
            reasons.append(f"ADX {adx_val:.0f} (trend)")
        else:
            reasons.append(f"ADX {adx_val:.0f} (range)")
    
    # Определить режим
    if score >= 30:
        regime = "BULL"
        allowed = "LONG"
        risk = 0.50
    elif score >= 10:
        regime = "BULL"
        allowed = "LONG"
        risk = 0.25
    elif score <= -30:
        regime = "BEAR"
        allowed = "SHORT"
        risk = 0.50
    elif score <= -10:
        regime = "BEAR"
        allowed = "SHORT"
        risk = 0.25
    else:
        regime = "NEUTRAL"
        allowed = "WAIT"
        risk = 0.25
    
    confidence = min(90, 50 + abs(score))
    
    return {
        "regime": regime,
        "confidence": confidence,
        "allowed_direction": allowed,
        "risk_pct": risk,
        "scores": {"fallback": score},
        "reason": reasons,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fallback": True
    }

def load_regime():
    """Загрузить рыночный режим из market_regime.json или fallback"""
    global _regime_cache, _regime_cache_time
    
    # Попробовать загрузить из файла
    if os.path.exists(REGIME_FILE):
        try:
            with open(REGIME_FILE, "r") as f:
                data = json.load(f)
            # Проверить актуальность (не старше 6 часов)
            if data.get("updated_at"):
                try:
                    updated = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    age_hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
                    if age_hours > 6:
                        print("⚠️ Regime устарел (>6ч), используем fallback")
                        return None  # Сигнал для fallback
                    return data
                except (ValueError, TypeError):
                    pass
            return data
        except (IOError, OSError, json.JSONDecodeError):
            pass
    
    # Fallback: математический расчёт (с кэшированием на 1 час)
    now = time.time()
    if _regime_cache and (now - _regime_cache_time) < 3600:
        print("⚠️ Regime: используем кэшированный fallback")
        return _regime_cache
    
    print("⚠️ Regime не найден, рассчитываем fallback")
    klines = get_klines('15', 200)
    _regime_cache = fallback_regime(klines)
    _regime_cache_time = now
    return _regime_cache

def volatility_regime(klines, period=14):
    """Определить режим волатильности (ATR% vs средняя)"""
    if len(klines) < 50:
        return 'normal', 1.0
    trs = []
    for i in range(1, len(klines)):
        tr = max(klines[i]["high"] - klines[i]["low"],
                 abs(klines[i]["high"] - klines[i-1]["close"]),
                 abs(klines[i]["low"] - klines[i-1]["close"]))
        trs.append(tr)
    if len(trs) < period:
        return 'normal', 1.0
    current_atr = sum(trs[-period:]) / period
    avg_atr = sum(trs[-50:]) / min(50, len(trs)) if len(trs) >= 50 else current_atr
    current_price = klines[-1]["close"]
    atr_pct = (current_atr / current_price * 100) if current_price > 0 else 0
    avg_atr_pct = (avg_atr / current_price * 100) if current_price > 0 else 0
    if atr_pct > avg_atr_pct * 1.5:
        return 'high', atr_pct / avg_atr_pct if avg_atr_pct > 0 else 1.0
    elif atr_pct < avg_atr_pct * 0.7:
        return 'low', atr_pct / avg_atr_pct if avg_atr_pct > 0 else 1.0
    else:
        return 'normal', atr_pct / avg_atr_pct if avg_atr_pct > 0 else 1.0

def is_trading_hours():
    """Проверить что сейчас торговые часы (08:00-20:00 UTC)"""
    hour = datetime.now(timezone.utc).hour
    return 8 <= hour <= 20

def analyze():
    price = get_btc_price(); klines = get_klines('15', 200)
    funding = get_funding_rate(); oi = get_open_interest()
    if not price or not klines: return None
    closes = [k['close'] for k in klines]
    e21 = ema(closes, 21); e55 = ema(closes, 55); e200 = ema(closes, 200) if len(closes) >= 200 else None
    r = rsi(closes); ml, sl, hist = macd(closes)
    bu, bm, bl = bollinger(closes); vr = volume_ratio(klines)
    a = atr(klines); sk, sd = stochastic_rsi(closes); adx_val, pdi, mdi = adx(klines); vol_regime, vol_ratio = volatility_regime(klines)
    trend = 'bullish' if e21 and e55 and e21 > e55 else 'bearish' if e21 and e55 and e21 < e55 else 'neutral'
    mom = 'bullish' if ml and sl and ml > sl else 'bearish' if ml and sl and ml < sl else 'neutral'
    support = min(k['low'] for k in klines[-20:]); resistance = max(k['high'] for k in klines[-20:])
    swing_low = recent_swing_low(klines, 20)
    swing_high = recent_swing_high(klines, 20)
    return {'price': price['price'], 'change': price['change'], 'trend': trend, 'momentum': mom,
            'ema21': e21, 'ema55': e55, 'ema200': e200, 'rsi': r, 'macd': ml, 'macd_signal': sl, 'hist': hist,
            'bb_upper': bu, 'bb_mid': bm, 'bb_lower': bl, 'atr': a, 'adx': adx_val, 'plus_di': pdi, 'minus_di': mdi, 'stoch_rsi_k': sk, 'stoch_rsi_d': sd,
            'volume': vr, 'funding': funding, 'oi': oi, 'support': support, 'resistance': resistance,
            'swing_low': swing_low, 'swing_high': swing_high, 'vol_regime': vol_regime, 'vol_ratio': vol_ratio}

def signal(analysis, positions, state):
    if not analysis: return None
    if len(positions) >= RISK_RULES['max_positions']: return {'action': 'WAIT', 'reason': 'Макс позиций'}
    for p in positions:
        if p['pnl'] < 0: return {'action': 'WAIT', 'reason': 'Убыточная позиция'}
    if count_recent_losses(state.get('trades', [])) >= RISK_RULES['max_daily_losses']:
        return {'action': 'WAIT', 'reason': f'Серия убытков, пауза {LOSS_COOLDOWN_HOURS}ч'}

    # === ФИЛЬТР ВРЕМЕНИ ===

    # === ФИЛЬТР ОБЪЁМА ===
    if analysis.get("volume", 1.0) < RISK_RULES["min_volume_ratio"]:
        return {"action": "WAIT", "reason": f"Низкий объём: {analysis.get("volume", 1.0):.2f}x < {RISK_RULES["min_volume_ratio"]}x"}

    # === МАКС. СДЕЛОК В ДЕНЬ ===
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_trades = sum(1 for t in state.get("trades", []) if t.get("time", "").startswith(today))
    if daily_trades >= RISK_RULES["max_daily_trades"]:
        return {"action": "WAIT", "reason": f"Макс. сделок в день: {daily_trades}/{RISK_RULES["max_daily_trades"]}"}
    if not is_trading_hours():
        return {"action": "WAIT", "reason": "Вне торговых часов (08:00-20:00 UTC)"}

    # === РЕЖИМНЫЙ ФИЛЬТР ===
    regime = load_regime()
    if regime["allowed_direction"] not in ["LONG", "SHORT", "BOTH"] or regime["regime"] in ["NEUTRAL", "RANGE", "STRONG_BEAR"]:
        return {"action": "WAIT", "reason": f"Regime: {regime["regime"]} ({regime["allowed_direction"]})"}

    # === ADX ФИЛЬТР ===
    adx_val = analysis.get("adx")
    if adx_val is not None and adx_val < 24:
        # Боковик - проверяем Mean Reversion
        rsi_val = analysis.get("rsi", 50)
        bb_lower = analysis.get("bb_lower")
        bb_upper = analysis.get("bb_upper")
        bb_mid = analysis.get("bb_mid")
        if rsi_val and bb_lower and bb_upper and bb_mid:
            if analysis["price"] < bb_lower * 1.002 and rsi_val < 32:
                # Mean Reversion LONG
                risk = analysis.get("atr", 250) * 1.1
                return {"action": "LONG", "confidence": 65, "reason": f"Mean Reversion (ADX={adx_val:.1f})",
                        "entry": analysis["price"], "sl": analysis["price"] - risk, "tp": bb_mid}
            elif analysis["price"] > bb_upper * 0.998 and rsi_val > 68:
                # Mean Reversion SHORT
                risk = analysis.get("atr", 250) * 1.1
                return {"action": "SHORT", "confidence": 65, "reason": f"Mean Reversion (ADX={adx_val:.1f})",
                        "entry": analysis["price"], "sl": analysis["price"] + risk, "tp": bb_mid}
        return {"action": "WAIT", "reason": f"Боковик ADX={adx_val:.1f}"}
    s = {'action': 'WAIT', 'confidence': 0, 'reason': '', 'entry': analysis['price'], 'sl': None, 'tp': None}
    lv = sv = 0
    price_above_trend = bool(analysis['ema55'] and analysis['ema200'] and analysis['price'] > analysis['ema55'] > analysis['ema200'])
    price_below_trend = bool(analysis['ema55'] and analysis['ema200'] and analysis['price'] < analysis['ema55'] < analysis['ema200'])
    if analysis['trend'] == 'bullish' and price_above_trend: lv += 3
    elif analysis['trend'] == 'bearish' and price_below_trend: sv += 3
    if analysis['rsi'] and 45 <= analysis['rsi'] <= 60:
        if price_above_trend: lv += 1
        elif price_below_trend: sv += 1
    elif analysis['rsi'] and analysis['rsi'] < 35 and price_above_trend:
        lv += 1
    elif analysis['rsi'] and analysis['rsi'] > 65 and price_below_trend:
        sv += 1
    if analysis['hist'] and analysis['hist'] > 0: lv += 1
    elif analysis['hist'] and analysis['hist'] < 0: sv += 1
    if analysis['bb_lower'] and analysis['price'] < analysis['bb_lower'] and price_above_trend: lv += 1
    elif analysis['bb_upper'] and analysis['price'] > analysis['bb_upper'] and price_below_trend: sv += 1
    if analysis['funding'] < 0: lv += 1
    elif analysis['funding'] > 0.0001: sv += 1
    if analysis['volume'] > 1.2:
        if lv > sv: lv += 1
        elif sv > lv: sv += 1
    if analysis['stoch_rsi_k'] and analysis['stoch_rsi_k'] < 20 and price_above_trend: lv += 1
    elif analysis['stoch_rsi_k'] and analysis['stoch_rsi_k'] > 80 and price_below_trend: sv += 1
    if lv >= 5 and lv > sv and price_above_trend:
        s['action'] = 'LONG'; s['confidence'] = min(90, 50 + lv * 5)
        atr_buffer = analysis['atr'] * 0.75 if analysis['atr'] else 150
        swing_low = analysis.get('swing_low') or analysis['support']
        s['sl'] = min(analysis['price'] - (analysis['atr'] * 1.2 if analysis['atr'] else 250), swing_low - atr_buffer)
        risk = abs(analysis['price'] - s['sl'])
        s['tp'] = analysis['price'] + risk * 2.2
        s['reason'] = f'LONG={lv} SHORT={sv}'
    elif sv >= 5 and sv > lv and price_below_trend:
        s['action'] = 'SHORT'; s['confidence'] = min(90, 50 + sv * 5)
        atr_buffer = analysis['atr'] * 0.75 if analysis['atr'] else 150
        swing_high = analysis.get('swing_high') or analysis['resistance']
        s['sl'] = max(analysis['price'] + (analysis['atr'] * 1.2 if analysis['atr'] else 250), swing_high + atr_buffer)
        risk = abs(s['sl'] - analysis['price'])
        s['tp'] = analysis['price'] - risk * 2.2
        s['reason'] = f'LONG={lv} SHORT={sv}'
    if s['action'] in ['LONG', 'SHORT'] and same_direction_position_exists(positions, s['action']):
        return {'action': 'WAIT', 'reason': 'Повторный вход запрещён'}
    if s['action'] in ['LONG', 'SHORT'] and s['sl'] and s['tp']:
        risk = abs(s['entry'] - s['sl']); reward = abs(s['tp'] - s['entry'])
        if risk > 0 and reward / risk < RISK_RULES['min_rr_ratio']:
            s['action'] = 'WAIT'; s['reason'] = f'RR {reward/risk:.1f} < {RISK_RULES["min_rr_ratio"]}'
    return s

def place_order(side, qty, order_type='Market'):
    return bybit_request('/v5/order/create', data={'category': 'linear', 'symbol': 'BTCUSDT', 'side': side, 'orderType': order_type, 'qty': str(qty)})

def close_position(side, qty):
    close_side = 'Sell' if side == 'Buy' else 'Buy'
    return bybit_request('/v5/order/create', data={
        'category': 'linear',
        'symbol': 'BTCUSDT',
        'side': close_side,
        'orderType': 'Market',
        'qty': str(qty),
        'reduceOnly': True,
    })

def set_trading_stop(sl=None, tp=None):
    d = {'category': 'linear', 'symbol': 'BTCUSDT'}
    if sl: d['stopLoss'] = str(sl)
    if tp: d['takeProfit'] = str(tp)
    return bybit_request('/v5/position/trading-stop', data=d)

def calc_size(balance, entry, sl, risk_pct=None, vol_ratio=1.0):
    risk = balance * (risk_pct if risk_pct is not None else RISK_RULES["max_risk_percent"]) / 100
    risk_per = abs(entry - sl)
    # Адаптация к volatility
    if vol_ratio > 1.5:
        risk *= TRAILING["vol_high_factor"]  # Уменьшаем в шумном рынке
    elif vol_ratio < 0.7:
        risk *= TRAILING["vol_low_factor"]   # Увеличиваем в тихом рынке
    return round(risk / risk_per, 3) if risk_per > 0 and risk / risk_per >= 0.001 else 0

def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f: json.dump(s, f, indent=2)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f: return json.load(f)
    return {'trades': []}

def main():
    print("=== Bybit BTC Auto Trader v10 ===\n")
    if not BYBIT_API_KEY or not BYBIT_API_SECRET: print("❌ API keys не настроены!"); return
    state = load_state()
    analysis = analyze()
    if not analysis: print('❌ Ошибка анализа'); return
    positions = get_positions()
    manage_trailing_stop(positions, analysis)
    balance = get_balance()
    if not balance: print('❌ Ошибка баланса'); return

    # === ПРОВЕРКА ЛИМИТОВ ===
    daily_ok, daily_pnl, daily_limit = check_daily_loss_limit(state, balance["equity"])
    if daily_ok:
        print(f"🔴 Дневной лимит: ${daily_pnl:,.2f} / -${daily_limit:,.2f}")
        state["last_analysis"] = analysis; save_state(state); return
    
    weekly_ok, weekly_pnl, weekly_limit = check_weekly_loss_limit(state, balance["equity"])
    if weekly_ok:
        print(f"🔴 Недельный лимит: ${weekly_pnl:,.2f} / -${weekly_limit:,.2f}")
        state["last_analysis"] = analysis; save_state(state); return
    
    dd_ok, max_dd = check_max_drawdown(state, balance["equity"])
    if dd_ok:
        print(f"🔴 Max Drawdown: {max_dd:.1f}% / {RISK_RULES["max_drawdown_pct"]}% — KILL SWITCH")
        state["last_analysis"] = analysis; save_state(state); return
    sig = signal(analysis, positions, state)
    print(f"💰 Баланс: ${balance['equity']:,.2f} | PnL: ${balance['pnl']:,.2f}")
    print(f"📊 BTC: ${analysis['price']:,.2f} ({analysis['change']:+.2f}%)")
    print(f"📈 Тренд: {analysis['trend']} | RSI: {analysis['rsi']:.1f} | ATR: {analysis['atr']:.1f}" if analysis['rsi'] and analysis['atr'] else "")
    print(f"💰 Funding: {analysis['funding']:.6f} | OI: {analysis['oi']:,.0f}")
    print(f"📋 Позиции: {len(positions)}")
    closed_any = False
    for p in positions:
        pnl_pct = ((p['mark'] - p['entry']) / p['entry'] * 100) if p['side'] == 'Buy' else ((p['entry'] - p['mark']) / p['entry'] * 100)
        print(f"   {p['side']}: {p['size']} BTC @ ${p['entry']:,.2f} | PnL: ${p['pnl']:,.2f} ({pnl_pct:+.2f}%)")
        if pnl_pct <= RISK_RULES['force_close_loss_pct']:
            close_result = close_position(p['side'], p['size'])
            if close_result.get('retCode') == 0:
                print(f"🔴 Закрыл: ${p['pnl']:,.2f} убыток")
                log_trade_extended(state, {'action': 'CLOSE', 'closed_pnl': p['pnl'], 'reason': 'force_close_loss_limit', 'side': p['side'], 'qty': p['size'], 'price': p['mark']})
                closed_any = True
            else:
                print(f"❌ Ошибка закрытия: {close_result.get('retMsg')}")
        elif pnl_pct >= RISK_RULES['take_profit_alert_pct']:
            close_result = close_position(p['side'], p['size'])
            if close_result.get('retCode') == 0:
                print(f"🟢 Закрыл: +${p['pnl']:,.2f} прибыль")
                log_trade_extended(state, {'action': 'CLOSE', 'closed_pnl': p['pnl'], 'reason': 'take_profit_lock', 'side': p['side'], 'qty': p['size'], 'price': p['mark']})
                closed_any = True
            else:
                print(f"❌ Ошибка фиксации: {close_result.get('retMsg')}")
        # Проверка таймаута позиции
        if not closed_any and pnl_pct <= 0:
            timeout, pos, hours = check_position_timeout([p])
            if timeout:
                close_result = close_position(p["side"], p["size"])
                if close_result.get("retCode") == 0:
                    print(f"⏰ Таймаут: позиция {hours:.0f}ч без прибыли - закрыта")
                    state["trades"].append({"time": datetime.now().isoformat(), "action": "CLOSE", "closed_pnl": p["pnl"], "reason": "position_timeout", "side": p["side"], "qty": p["size"], "price": p["mark"]})
                    closed_any = True
    if closed_any:
        state['last_analysis'] = analysis; save_state(state)
    
    # Dashboard
    dashboard = performance_dashboard(state)
    if dashboard and dashboard['total_trades'] >= 5:
        print_dashboard(dashboard)
        return

    conf = sig.get('confidence', 50)
    print(f"\\n🎯 Сигнал: {sig['action']} ({conf:.1f}%)")
    if sig["action"] in ["LONG", "SHORT"] and sig.get("confidence", 0) >= 77:
        regime = load_regime()
        regime_risk = regime.get("risk_pct", 0.5)
        if regime["regime"] == "NEUTRAL":
            regime_risk = 0.15  # Снижаем риск в нейтральном режиме
        qty = calc_size(balance["equity"], sig["entry"], sig["sl"], regime_risk, analysis.get("vol_ratio", 1.0))
        if qty > 0:
            side = 'Buy' if sig['action'] == 'LONG' else 'Sell'
            print(f'\n{"🟢" if sig["action"] == "LONG" else "🔴"} ОТКРЫВАЮ {sig["action"]} {qty} BTC...')
            result = place_order(side, qty)
            if result.get('retCode') != 0 and 'not enough' in str(result.get('retMsg', '')).lower():
                qty = round(qty * 0.5, 3)
                if qty >= 0.001: result = place_order(side, qty)
            if result.get('retCode') == 0:
                print(f'✅ Ордер! ID: {result["result"].get("orderId")}')
                time.sleep(1)
                tp_sl = set_trading_stop(sl=sig['sl'], tp=sig['tp'])
                if tp_sl.get('retCode') == 0: print(f'✅ TP/SL установлен!')
                state['trades'].append({'time': datetime.now().isoformat(), 'action': sig['action'], 'qty': qty, 'price': sig['entry'], 'sl': sig['sl'], 'tp': sig['tp']})
            else: print(f'❌ {result.get("retMsg")}')
    state['last_analysis'] = analysis; save_state(state)
    
    # Dashboard
    dashboard = performance_dashboard(state)
    if dashboard and dashboard['total_trades'] >= 5:
        print_dashboard(dashboard)

def check_daily_loss_limit(state, balance):
    """Проверить дневной лимит убытков"""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    daily_pnl = 0
    for trade in state.get('trades', []):
        if trade.get('time', '').startswith(today) and trade.get('closed_pnl') is not None:
            daily_pnl += trade['closed_pnl']
    daily_limit = balance * RISK_RULES['daily_loss_limit_pct'] / 100
    if daily_pnl < -daily_limit:
        return True, daily_pnl, daily_limit
    return False, daily_pnl, daily_limit

def check_weekly_loss_limit(state, balance):
    """Проверить недельный лимит убытков"""
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday())
    week_start_str = week_start.strftime('%Y-%m-%d')
    weekly_pnl = 0
    for trade in state.get('trades', []):
        if trade.get('time', '') >= week_start_str and trade.get('closed_pnl') is not None:
            weekly_pnl += trade['closed_pnl']
    weekly_limit = balance * RISK_RULES['weekly_loss_limit_pct'] / 100
    if weekly_pnl < -weekly_limit:
        return True, weekly_pnl, weekly_limit
    return False, weekly_pnl, weekly_limit

def check_max_drawdown(state, balance):
    """Проверить максимальную просадку от пика"""
    peak = balance
    max_dd = 0
    for trade in state.get('trades', []):
        if trade.get('closed_pnl') is not None:
            peak += trade['closed_pnl']
            if peak > balance:
                peak = balance
            dd = (balance - peak) / balance * 100 if balance > 0 else 0
            if dd > max_dd:
                max_dd = dd
    if max_dd >= RISK_RULES['max_drawdown_pct']:
        return True, max_dd
    return False, max_dd

def check_position_timeout(positions):
    """Проверить таймаут позиций (без прибыли >48h)"""
    for p in positions:
        if p.get('open_time'):
            try:
                open_time = datetime.fromisoformat(p['open_time'].replace('Z', '+00:00'))
                if open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=timezone.utc)
                hours_open = (datetime.now(timezone.utc) - open_time).total_seconds() / 3600
                if hours_open > RISK_RULES['position_max_hours'] and p.get('pnl', 0) <= 0:
                    return True, p, hours_open
            except (IOError, OSError):
                pass
    return False, None, 0

def log_trade_extended(state, trade_data):
    """Расширенное логирование сделки"""
    regime = load_regime()
    trade_entry = {
        'time': datetime.now().isoformat(),
        'action': trade_data.get('action'),
        'side': trade_data.get('side'),
        'qty': trade_data.get('qty'),
        'price': trade_data.get('price'),
        'sl': trade_data.get('sl'),
        'tp': trade_data.get('tp'),
        'closed_pnl': trade_data.get('closed_pnl'),
        'reason': trade_data.get('reason'),
        'regime': regime.get('regime'),
        'regime_confidence': regime.get('confidence'),
        'regime_direction': regime.get('allowed_direction'),
    }
    state['trades'].append(trade_entry)
    return trade_entry

def performance_dashboard(state):
    """Dashboard производительности"""
    trades = state.get('trades', [])
    if not trades:
        return None
    
    # Только закрытые сделки
    closed = [t for t in trades if t.get('closed_pnl') is not None]
    if not closed:
        return None
    
    # Основные метрики
    total_trades = len(closed)
    wins = [t for t in closed if t['closed_pnl'] > 0]
    losses = [t for t in closed if t['closed_pnl'] <= 0]
    
    win_rate = len(wins) / total_trades * 100 if total_trades > 0 else 0
    total_pnl = sum(t['closed_pnl'] for t in closed)
    avg_win = sum(t['closed_pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['closed_pnl'] for t in losses) / len(losses) if losses else 0
    
    # Profit Factor
    gross_profit = sum(t['closed_pnl'] for t in wins)
    gross_loss = abs(sum(t['closed_pnl'] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Expectancy (EV)
    ev = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)
    
    # Max Drawdown
    peak = 0
    max_dd = 0
    equity = 10000  # Начальный баланс
    for t in closed:
        equity += t['closed_pnl']
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    
    # По регионам
    by_regime = {}
    for t in closed:
        r = t.get('regime', 'UNKNOWN')
        if r not in by_regime:
            by_regime[r] = {'trades': 0, 'pnl': 0, 'wins': 0}
        by_regime[r]['trades'] += 1
        by_regime[r]['pnl'] += t['closed_pnl']
        if t['closed_pnl'] > 0:
            by_regime[r]['wins'] += 1
    
    # По причинам закрытия
    by_reason = {}
    for t in closed:
        r = t.get('reason', 'UNKNOWN')
        if r not in by_reason:
            by_reason[r] = {'count': 0, 'pnl': 0}
        by_reason[r]['count'] += 1
        by_reason[r]['pnl'] += t['closed_pnl']
    
    return {
        'total_trades': total_trades,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(win_rate, 1),
        'total_pnl': round(total_pnl, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'expectancy': round(ev, 2),
        'max_drawdown': round(max_dd, 1),
        'by_regime': by_regime,
        'by_reason': by_reason,
    }

def print_dashboard(dashboard):
    """Вывести dashboard"""
    if not dashboard:
        print("Нет данных для dashboard")
        return
    
    print(f"\n=== PERFORMANCE DASHBOARD ===")
    print(f"Всего сделок: {dashboard['total_trades']}")
    print(f"Прибыльных: {dashboard['wins']} ({dashboard['win_rate']}%)")
    print(f"Убыточных: {dashboard['losses']}")
    print(f"Общий PnL: ${dashboard['total_pnl']:,.2f}")
    print(f"Ср. прибыль: ${dashboard['avg_win']:,.2f}")
    print(f"Ср. убыток: ${dashboard['avg_loss']:,.2f}")
    print(f"Profit Factor: {dashboard['profit_factor']}")
    print(f"Expectancy (EV): ${dashboard['expectancy']:,.2f}")
    print(f"Max Drawdown: {dashboard['max_drawdown']}%")
    
    print(f"\n--- По регионам ---")
    for regime, stats in dashboard['by_regime'].items():
        wr = stats['wins'] / stats['trades'] * 100 if stats['trades'] > 0 else 0
        print(f"  {regime}: {stats['trades']} сделок, PnL ${stats['pnl']:,.2f}, Winrate {wr:.0f}%")
    
    print(f"\n--- По причинам ---")
    for reason, stats in dashboard['by_reason'].items():
        print(f"  {reason}: {stats['count']} шт, PnL ${stats['pnl']:,.2f}")


if __name__ == '__main__':
    main()
