#!/usr/bin/env python3
"""BTC Monitor Service - Фоновый мониторинг с алертами и health-check"""
import urllib.request, json, hmac, hashlib, time, os, sys
HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes/profiles/trader"))

# Конфигурация
env = {}
env_file = os.path.join(HERMES_HOME, '.env')
with open(env_file) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()

API_KEY = env['BYBIT_API_KEY']
API_SECRET = env['BYBIT_API_SECRET']
BOT_TOKEN = env['TELEGRAM_BOT_TOKEN']
CHAT_ID = env['TELEGRAM_CHAT_ID']
BASE_URL = 'https://api-demo.bybit.com'

# Состояние
last_price = None
last_alert_time = 0
last_health_check = 0
api_healthy = True
ALERT_COOLDOWN = 300
HEALTH_CHECK_INTERVAL = 300  # 5 минут
POSITION_CHECK_INTERVAL = 30
LOSS_ALERT_THRESHOLD = -1.0  # Алерт при убытке > 1%

def send_telegram(msg):
    """Отправить сообщение в Telegram"""
    try:
        data = json.dumps({'chat_id': CHAT_ID, 'text': msg}).encode()
        req = urllib.request.Request(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage', data=data, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

def bybit_request(endpoint, query=''):
    """Запрос к Bybit API"""
    ts = str(int(time.time() * 1000))
    sign = hmac.new(API_SECRET.encode(), f'{ts}{API_KEY}5000{query}'.encode(), hashlib.sha256).hexdigest()
    url = f'{BASE_URL}{endpoint}' + (f'?{query}' if query else '')
    req = urllib.request.Request(url, headers={
        'X-BAPI-API-KEY': API_KEY,
        'X-BAPI-SIGN': sign,
        'X-BAPI-TIMESTAMP': ts,
        'X-BAPI-RECV-WINDOW': '5000'
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def health_check():
    """Проверка работоспособности Bybit API"""
    global api_healthy, last_health_check
    
    current_time = time.time()
    if current_time - last_health_check < HEALTH_CHECK_INTERVAL:
        return api_healthy
    
    last_health_check = current_time
    
    try:
        # Простой ping - проверка времени сервера
        result = bybit_request('/v5/market/time')
        if result.get('retCode') == 0:
            if not api_healthy:
                print(f"[{time.strftime('%H:%M:%S')}] ✅ Bybit API восстановлен")
                send_telegram("✅ Bybit API восстановлен!\nМониторинг продолжается.")
            api_healthy = True
            return True
        else:
            raise Exception("API returned error")
    except Exception as e:
        if api_healthy:
            print(f"[{time.strftime('%H:%M:%S')}] ❌ Bybit API недоступен: {e}")
            send_telegram(f"❌ Bybit API недоступен!\nОшибка: {str(e)[:100]}\nТорговля приостановлена.")
        api_healthy = False
        return False

def check_position():
    """Проверить позицию"""
    try:
        positions = bybit_request('/v5/position/list', 'category=linear&symbol=BTCUSDT')['result']['list']
        active = [p for p in positions if float(p.get('size', 0)) > 0]
        
        if not active:
            return None
        
        p = active[0]
        entry = float(p['avgPrice'])
        mark = float(p['markPrice'])
        side = p['side']
        size = float(p['size'])
        sl = float(p.get('stopLoss', 0) or 0)
        tp = float(p.get('takeProfit', 0) or 0)
        
        if side == 'Buy':
            pnl_pct = (mark - entry) / entry * 100
        else:
            pnl_pct = (entry - mark) / entry * 100
        
        return {
            'side': side, 'size': size, 'entry': entry,
            'mark': mark, 'pnl_pct': pnl_pct, 'sl': sl, 'tp': tp
        }
    except Exception as e:
        print(f"Position check error: {e}")
        return None

def check_loss_alert(pos):
    """Проверить необходимость алерта об убытке"""
    global last_alert_time
    
    if pos['pnl_pct'] > LOSS_ALERT_THRESHOLD:
        return
    
    current_time = time.time()
    if current_time - last_alert_time < ALERT_COOLDOWN:
        return
    
    pnl_usd = (pos['mark'] - pos['entry']) * pos['size'] if pos['side'] == 'Buy' else \
              (pos['entry'] - pos['mark']) * pos['size']
    
    msg = f"🔴 УБЫТОК: {pos['pnl_pct']:+.2f}%\n"
    msg += f"Позиция: {pos['side']} {pos['size']} BTC\n"
    msg += f"Entry: ${pos['entry']:,.2f}\n"
    msg += f"Текущая: ${pos['mark']:,.2f}\n"
    msg += f"PnL: ${pnl_usd:,.2f}\n"
    if pos['sl'] > 0:
        msg += f"SL: ${pos['sl']:,.2f}"
    
    send_telegram(msg)
    last_alert_time = current_time

def format_status(pos, trailing):
    """Форматировать статус для лога"""
    if not pos:
        return "Нет позиций"
    
    emoji = "🟢" if pos['pnl_pct'] >= 0 else "🔴"
    return f"{emoji} {pos['side']}: PnL {pos['pnl_pct']:+.2f}% | {trailing['action']}"

def main():
    """Основной цикл мониторинга"""
    global last_price, last_alert_time
    
    print(f"[{time.strftime('%H:%M:%S')}] 🚀 BTC Monitor запущен")
    send_telegram("🚀 BTC Monitor запущен!\nМониторинг активен.")
    
    # Начальная проверка API
    if not health_check():
        print(f"[{time.strftime('%H:%M:%S')}] ⚠️ Bybit API недоступен, ожидание...")
    
    while True:
        try:
            # Health-check API
            if not health_check():
                time.sleep(60)
                continue
            
            # Проверить позицию
            pos = check_position()
            
            if pos:
                # Проверка убытка
                check_loss_alert(pos)
                
                # Лог
                status = format_status(pos, {'action': 'HOLD'})
                print(f"[{time.strftime('%H:%M:%S')}] {status}")
                
                # Обновить последнюю цену
                last_price = pos['mark']
            else:
                # Нет позиций - проверяем раз в 5 минут
                print(f"[{time.strftime('%H:%M:%S')}] Нет открытых позиций")
            
            time.sleep(POSITION_CHECK_INTERVAL)
            
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Ошибка: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
