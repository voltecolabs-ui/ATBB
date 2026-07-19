#!/usr/bin/env python3
"""Bybit BTC Auto Trader v10 - EMA, RSI, MACD, Bollinger, ATR, StochRSI, OI, Funding"""
import os, time, json, hashlib, hmac, urllib.request
from datetime import datetime, timedelta, timezone

HERMES_HOME = os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes/profiles/trader'))
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
}
LOSS_COOLDOWN_HOURS = 4  # Пауза после серии убытков (часы)

# Trailing Stop настройки
TRAILING = {
    'breakeven_trigger_r': 1.0,      # SL → entry при +1R
    'breakeven_buffer_pct': 0.15,    # Buffer для комиссий (0.15%)
    'partial_close_r': 1.5,          # Частичная фиксация при +1.5R
    'partial_close_pct': 30,         # Закрыть 30% позиции
    'tight_trail_r': 2.0,            # Плотный trailing при +2R
    'tight_trail_distance_atr': 1.0, # Расстояние trailing = 1x ATR
}

def bybit_request(endpoint, params=None, data=None):
    if not BYBIT_API_KEY or not BYBIT_API_SECRET: return {'error': 'No API keys'}
    ts = str(int(time.time() * 1000))
    q = '&'.join([f'{k}={v}' for k, v in sorted(params.items())]) if params else ''
    body = json.dumps(data) if data else ''
    sign = hmac.new(BYBIT_API_SECRET.encode(), f"{ts}{BYBIT_API_KEY}5000{q}{body}".encode(), hashlib.sha256).hexdigest()
    url = f'{DEMO_BASE}{endpoint}' + (f'?{q}' if q else '')
    h = {'X-BAPI-API-KEY': BYBIT_API_KEY, 'X-BAPI-SIGN': sign, 'X-BAPI-TIMESTAMP': ts, 'X-BAPI-RECV-WINDOW': '5000', 'Content-Type': 'application/json'}
    req = urllib.request.Request(url, headers=h, method='POST' if data else 'GET')
    if data: req.data = body.encode()
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read().decode())
    except Exception as e: return {'error': str(e)}

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
                    'liq': float(p.get('liqPrice', 0) or 0)})
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
    rsi_vals = []
    for i in range(rsi_period + 1, len(prices) + 1):
        r = rsi(prices[:i], rsi_period)
        if r is not None: rsi_vals.append(r)
    if len(rsi_vals) < stoch_period: return None, None
    rsi_min = min(rsi_vals[-stoch_period:]); rsi_max = max(rsi_vals[-stoch_period:])
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
    """Расчёт ADX (Average Directional Index) для определения силы тренда"""
    if len(klines) < period + 1:
        return None, None, None
    
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
    
    # ATR
    atr_val = sum(trs[-period:]) / period if trs else 1
    
    # +DI / -DI
    plus_di = 100 * (sum(plus_dm[-period:]) / period) / atr_val if atr_val > 0 else 0
    minus_di = 100 * (sum(minus_dm[-period:]) / period) / atr_val if atr_val > 0 else 0
    
    # DX
    if plus_di + minus_di == 0:
        dx = 0
    else:
        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
    
    return dx, plus_di, minus_di
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
        except:
            return losses
    
    return losses

def load_regime():
    """Загрузить рыночный режим из market_regime.json"""
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

def analyze():
    price = get_btc_price(); klines = get_klines('15', 200)
    funding = get_funding_rate(); oi = get_open_interest()
    if not price or not klines: return None
    closes = [k['close'] for k in klines]
    e21 = ema(closes, 21); e55 = ema(closes, 55); e200 = ema(closes, 200) if len(closes) >= 200 else None
    r = rsi(closes); ml, sl, hist = macd(closes)
    bu, bm, bl = bollinger(closes); vr = volume_ratio(klines)
    a = atr(klines); sk, sd = stochastic_rsi(closes); adx_val, pdi, mdi = adx(klines)
    trend = 'bullish' if e21 and e55 and e21 > e55 else 'bearish' if e21 and e55 and e21 < e55 else 'neutral'
    mom = 'bullish' if ml and sl and ml > sl else 'bearish' if ml and sl and ml < sl else 'neutral'
    support = min(k['low'] for k in klines[-20:]); resistance = max(k['high'] for k in klines[-20:])
    swing_low = recent_swing_low(klines, 20)
    swing_high = recent_swing_high(klines, 20)
    return {'price': price['price'], 'change': price['change'], 'trend': trend, 'momentum': mom,
            'ema21': e21, 'ema55': e55, 'ema200': e200, 'rsi': r, 'macd': ml, 'macd_signal': sl, 'hist': hist,
            'bb_upper': bu, 'bb_mid': bm, 'bb_lower': bl, 'atr': a, 'adx': adx_val, 'plus_di': pdi, 'minus_di': mdi, 'stoch_rsi_k': sk, 'stoch_rsi_d': sd,
            'volume': vr, 'funding': funding, 'oi': oi, 'support': support, 'resistance': resistance,
            'swing_low': swing_low, 'swing_high': swing_high}

def signal(analysis, positions, state):
    if not analysis: return None
    if len(positions) >= RISK_RULES['max_positions']: return {'action': 'WAIT', 'reason': 'Макс позиций'}
    for p in positions:
        if p['pnl'] < 0: return {'action': 'WAIT', 'reason': 'Убыточная позиция'}
    if count_recent_losses(state.get('trades', [])) >= RISK_RULES['max_daily_losses']:
        return {'action': 'WAIT', 'reason': f'Серия убытков, пауза {LOSS_COOLDOWN_HOURS}ч'}

    # === РЕЖИМНЫЙ ФИЛЬТР ===
    regime = load_regime()
    if regime["allowed_direction"] not in ["LONG", "SHORT", "BOTH"]:
        return {"action": "WAIT", "reason": f"Regime: {regime["regime"]} ({regime["allowed_direction"]})"}

    # === ADX ФИЛЬТР ===
    adx_val = analysis.get("adx")
    if adx_val is not None and adx_val < 23:
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

def calc_size(balance, entry, sl, risk_pct=None):
    risk = balance * (risk_pct if risk_pct is not None else RISK_RULES["max_risk_percent"]) / 100
    risk_per = abs(entry - sl)
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
    # Управление trailing stop\n    manage_trailing_stop(positions, analysis)
    balance = get_balance()
    if not balance: print('❌ Ошибка баланса'); return
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
                state['trades'].append({'time': datetime.now().isoformat(), 'action': 'CLOSE', 'closed_pnl': p['pnl'], 'reason': 'force_close_loss_limit', 'side': p['side'], 'qty': p['size'], 'price': p['mark']})
                closed_any = True
            else:
                print(f"❌ Ошибка закрытия: {close_result.get('retMsg')}")
        elif pnl_pct >= RISK_RULES['take_profit_alert_pct']:
            close_result = close_position(p['side'], p['size'])
            if close_result.get('retCode') == 0:
                print(f"🟢 Закрыл: +${p['pnl']:,.2f} прибыль")
                state['trades'].append({'time': datetime.now().isoformat(), 'action': 'CLOSE', 'closed_pnl': p['pnl'], 'reason': 'take_profit_lock', 'side': p['side'], 'qty': p['size'], 'price': p['mark']})
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
        state['last_analysis'] = analysis; save_state(state); return
    conf = sig.get('confidence', 50)
    print(f"\\n🎯 Сигнал: {sig['action']} ({conf:.1f}%)")
    if sig['action'] in ['LONG', 'SHORT'] and sig.get('confidence', 0) >= 70:
        regime = load_regime()
        qty = calc_size(balance["equity"], sig["entry"], sig["sl"], regime.get("risk_pct", 0.5))
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

if __name__ == '__main__': main()

def manage_trailing_stop(positions, analysis):
    """Управление trailing stop для открытых позиций"""
    if not positions or not analysis:
        return
    
    atr_val = analysis.get('atr') or 200
    
    for p in positions:
        entry = p['entry']
        mark = p['mark']
        side = p['side']
        
        # Рассчитать PnL в R (множителях риска)
        if side == 'Buy':
            pnl_pct = (mark - entry) / entry * 100
        else:
            pnl_pct = (entry - mark) / entry * 100
        
        # Рассчитать R на основе ATR
        risk_per_unit = atr_val * 1.2  # SL = entry ± 1.2 ATR
        if risk_per_unit <= 0:
            continue
        
        if side == 'Buy':
            r_multiple = (mark - entry) / risk_per_unit
        else:
            r_multiple = (entry - mark) / risk_per_unit
        
        # 1. При +1R → SL на entry + buffer (безубыток с учётом комиссий)
        if r_multiple >= TRAILING['breakeven_trigger_r']:
            if side == 'Buy':
                new_sl = entry * (1 + TRAILING['breakeven_buffer_pct'] / 100)
            else:
                new_sl = entry * (1 - TRAILING['breakeven_buffer_pct'] / 100)
            
            result = set_trading_stop(sl=new_sl)
            if result.get('retCode') == 0:
                print(f"   🔒 Trailing: +{r_multiple:.1f}R → SL на безубыток ${new_sl:,.2f}")
        
        # 2. При +1.5R → частичная фиксация (30% позиции)
        elif r_multiple >= TRAILING['partial_close_r']:
            # Проверить не было ли уже частичного закрытия
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
            except:
                pass
    return False, None, 0

def volatility_regime(klines, period=14):
    """Определить режим волатильности (ATR% vs средняя)"""
    if len(klines) < 50:
        return 'normal', 1.0
    
    # Текущий ATR%
    trs = []
    for i in range(1, len(klines)):
        tr = max(klines[i]['high'] - klines[i]['low'],
                 abs(klines[i]['high'] - klines[i-1]['close']),
                 abs(klines[i]['low'] - klines[i-1]['close']))
        trs.append(tr)
    
    if len(trs) < period:
        return 'normal', 1.0
    
    current_atr = sum(trs[-period:]) / period
    avg_atr = sum(trs[-50:]) / min(50, len(trs)) if len(trs) >= 50 else current_atr
    
    # ATR% = ATR / цена * 100
    current_price = klines[-1]['close']
    atr_pct = (current_atr / current_price * 100) if current_price > 0 else 0
    avg_atr_pct = (avg_atr / current_price * 100) if current_price > 0 else 0
    
    # Определение режима
    if atr_pct > avg_atr_pct * 1.5:
        return 'high', atr_pct / avg_atr_pct if avg_atr_pct > 0 else 1.0
    elif atr_pct < avg_atr_pct * 0.7:
        return 'low', atr_pct / avg_atr_pct if avg_atr_pct > 0 else 1.0
    else:
        return 'normal', atr_pct / avg_atr_pct if avg_atr_pct > 0 else 1.0
