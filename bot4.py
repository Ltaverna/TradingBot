import time
import math
import logging
import json
import sqlite3
import pandas as pd
from datetime import datetime
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
import telebot
from threading import Lock
import ccxt  # Añadimos ccxt como respaldo
import os
from dotenv import load_dotenv

# ============= CONFIGURACIÓN INICIAL =============
class TradingBot:
    def __init__(self):
        # Configuración de logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('trading_bot.log'),
                logging.StreamHandler()
            ]
        )
        load_dotenv('.env')        
        # Configuración Telegram
        self.telegram_key = os.getenv("TELEGRAM_KEY")
        self.bot = telebot.TeleBot(self.telegram_key)
        self.AUTHORIZED_CHAT_IDS = os.getenv("TELEGRAM_CHAT_ID")
        
        # Configuración Binance
        self.API_KEY = os.getenv("BINANCE_API_KEY")
        self.API_SECRET = os.getenv("BINANCE_API_SECRET")
        self.client = Client(self.API_KEY, self.API_SECRET)
        self.ccxt_client = ccxt.binance({'apiKey': self.API_KEY, 'secret': self.API_SECRET})
        
        # Parámetros de trading
        self.SYMBOL = 'XRPUSDT'
        self.INTERVAL = Client.KLINE_INTERVAL_5MINUTE
        self.LIMIT = 100
        self.SHORT_WINDOW = 3
        self.MEDIUM_WINDOW = 20
        self.LONG_WINDOW = 30
        self.RSI_LOWER = 40
        self.RSI_UPPER = 60
        self.STOP_LOSS_PCT = 0.03
        self.TAKE_PROFIT_PCT = 0.1
        self.MIN_USDT_BALANCE = 10
        self.RSI_PERIOD = 5

        # Control de concurrencia
        self.lock = Lock()
        
        # Estado del bot
        self.position = None
        self.stop_loss_price = None
        self.take_profit_price = None
        self.initial_value = 0

    # ============= MÉTODOS DE NOTIFICACIÓN =============
    def notify_telegram(self, message):
        with self.lock:
            #for chat_id in self.AUTHORIZED_CHAT_IDS:
            try:
                self.bot.send_message(self.AUTHORIZED_CHAT_IDS, message)
            except Exception as e:
                logging.error(f"Error enviando mensaje a Telegram: {e}")

    # ============= BASE DE DATOS =============
    def init_db(self):
        with sqlite3.connect("trades.db") as conn:
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    trade_date TEXT,
                    symbol TEXT,
                    side TEXT,
                    quantity REAL,
                    price REAL,
                    order_details TEXT,
                    profit_loss REAL DEFAULT 0
                )
            ''')
            # Asegurar que la columna trade_date existe
            c.execute("PRAGMA table_info(trades)")
            columns = [row[1] for row in c.fetchall()]
            if 'trade_date' not in columns:
                c.execute("ALTER TABLE trades ADD COLUMN trade_date TEXT")
                conn.commit()
                logging.info("Added trade_date column to trades table.")

    def save_trade(self, order, signal, profit_loss=0):
        try:
            timestamp = datetime.utcnow().isoformat()  # Usar UTC para timestamp
            trade_date = datetime.now().date().isoformat()  # Fecha local para trade_date
            symbol = order.get("symbol", self.SYMBOL)
            side = order.get("side", signal)
            price = float(order.get("fills", [{}])[0].get("price", 0))
            quantity = float(order.get("executedQty", 0))
            detalles = json.dumps(order)
            with sqlite3.connect("trades.db") as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO trades (timestamp, trade_date, symbol, side, quantity, price, order_details, profit_loss)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, trade_date, symbol, side, quantity, price, detalles, profit_loss))
                conn.commit()
                logging.info("Trade saved successfully.")
        except Exception as e:
            logging.error(f"Error saving trade: {e}")
    
    def get_today_trades(self):
        try:
            today = datetime.now().date().isoformat()  # Obtener la fecha local de hoy
            with sqlite3.connect("trades.db") as conn:
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM trades WHERE trade_date = ?", (today,))
                count = c.fetchone()[0]
                return count
        except Exception as e:
            logging.error(f"Error getting today's trades: {e}")
            return 0

    # ============= INDICADORES TÉCNICOS =============
    def get_data(self):
        try:
            klines = self.client.get_klines(symbol=self.SYMBOL, interval=self.INTERVAL, limit=self.LIMIT)
            df = pd.DataFrame(klines, columns=[
                'open_time', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'number_of_trades',
                'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
            ])
            df['close'] = df['close'].astype(float)
            df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
            return self.calculate_indicators(df)
        except Exception as e:
            logging.error(f"Error obteniendo datos: {e}")
            return None

    def calculate_indicators(self, df):
        df['MA_short'] = df['close'].ewm(span=self.SHORT_WINDOW, adjust=False).mean()  # EMA3
        df['MA_medium'] = df['close'].ewm(span=self.MEDIUM_WINDOW, adjust=False).mean()  # EMA5
        df['MA_long'] = df['close'].ewm(span=self.LONG_WINDOW, adjust=False).mean()  # EMA10

        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/self.RSI_PERIOD, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/self.RSI_PERIOD, adjust=False).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        return df

    def detect_signal(self, df):
        if df is None or len(df) < 2:
            return None
        current = df.iloc[-1]
        previous = df.iloc[-2]
        
        buy_signal = (
            previous['MA_short'] < previous['MA_medium'] and
            current['MA_short'] > current['MA_medium'] and
            current['MA_short'] > current['MA_long'] and
            current['RSI'] < self.RSI_LOWER
        )
        sell_signal = (
            previous['MA_short'] > previous['MA_medium'] and
            current['MA_short'] < current['MA_medium'] and
            current['MA_short'] < current['MA_long'] and
            current['RSI'] > self.RSI_UPPER
        )
        
        return 'buy' if buy_signal else 'sell' if sell_signal else None

    # ============= GESTIÓN DE ÓRDENES =============
    def get_quantity(self, usdt_amount):
        try:
            ticker = self.client.get_symbol_ticker(symbol=self.SYMBOL)
            price = float(ticker['price'])
            qty = usdt_amount / price
            return self.adjust_quantity(qty)
        except Exception as e:
            logging.error(f"Error calculando cantidad: {e}")
            return 0

    def adjust_quantity(self, qty):
        try:
            info = self.client.get_symbol_info(self.SYMBOL)
            for f in info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    step_size = float(f['stepSize'])
                    qty = math.floor(qty / step_size) * step_size
                elif f['filterType'] == 'MIN_NOTIONAL':
                    min_notional = float(f['minNotional'])
                    price = float(self.client.get_symbol_ticker(symbol=self.SYMBOL)['price'])
                    if qty * price < min_notional:
                        qty = math.ceil(min_notional / price / step_size) * step_size
            return qty
        except Exception as e:
            logging.error(f"Error ajustando cantidad: {e}")
            return qty

    def execute_order(self, signal, quantity):
        with self.lock:
            try:
                if signal == 'buy':
                    order = self.client.create_order(
                        symbol=self.SYMBOL, side=SIDE_BUY,
                        type=ORDER_TYPE_MARKET, quantity=quantity
                    )
                else:  # sell
                    order = self.client.create_order(
                        symbol=self.SYMBOL, side=SIDE_SELL,
                        type=ORDER_TYPE_MARKET, quantity=quantity
                    )
                
                price = float(order['fills'][0]['price'])
                msg = f"Orden {signal.upper()} ejecutada\nCantidad: {quantity}\nPrecio: {price}"
                self.notify_telegram(msg)
                self.save_trade(order, signal)
                return order
            except Exception as e:
                logging.error(f"Error ejecutando orden {signal}: {e}")
                return None

    def get_balance(self, asset):
        try:
            return float(self.client.get_asset_balance(asset=asset)['free'])
        except Exception as e:
            logging.error(f"Error obteniendo balance de {asset}: {e}")
            return 0

    def get_portfolio_value(self):
        try:
            usdt = self.get_balance('USDT')
            xrp = self.get_balance('XRP')
            price = float(self.client.get_symbol_ticker(symbol=self.SYMBOL)['price'])
            return usdt + (xrp * price)
        except Exception as e:
            logging.error(f"Error calculando valor del portfolio: {e}")
            return 0

    # ============= LÓGICA PRINCIPAL =============
    def run(self):
        self.init_db()
        self.initial_value = self.get_portfolio_value()
        logging.info(f"Valor inicial: {self.initial_value:.2f} USDT")
        self.notify_telegram(f"Bot iniciado - Valor inicial: {self.initial_value:.2f} USDT")
        

        while True:
            try:
                today_trades = self.get_today_trades()
                if today_trades >= 10:
                    logging.info("Daily trade limit reached. Skipping.")
                    time.sleep(300)
                    continue
                # Verificar balance mínimo
                usdt_balance = self.get_balance('USDT')
                if usdt_balance < self.MIN_USDT_BALANCE:
                    xrp_balance = self.get_balance('XRP')
                    if xrp_balance > 0:
                        self.execute_order('sell', xrp_balance)
                        time.sleep(5)

                # Obtener datos y señales
                df = self.get_data()
                if df is None:
                    time.sleep(300)
                    continue

                signal = self.detect_signal(df)
                current_price = float(self.client.get_symbol_ticker(symbol=self.SYMBOL)['price'])
                if signal:
                    latest = df.iloc[-1]
                    print(f"Señal detectada: {signal} | Indicadores: MA_short={latest['MA_short']:.4f}, MA_medium={latest['MA_medium']:.4f}, MA_long={latest['MA_long']:.4f}, RSI={latest['RSI']:.2f}")
                # Gestión de posición existente
                if self.position == 'long':
                    if current_price <= self.stop_loss_price:
                        xrp_balance = self.get_balance('XRP')
                        profit_loss = (current_price - self.entry_price) * xrp_balance
                        self.execute_order('sell', xrp_balance)
                        self.save_trade({'symbol': self.SYMBOL, 'executedQty': xrp_balance}, 'sell', profit_loss)
                        self.position = None
                        self.notify_telegram(f"Stop Loss ejecutado @ {current_price}")
                    
                    elif current_price >= self.take_profit_price:
                        xrp_balance = self.get_balance('XRP')
                        profit_loss = (current_price - self.entry_price) * xrp_balance
                        self.execute_order('sell', xrp_balance)
                        self.save_trade({'symbol': self.SYMBOL, 'executedQty': xrp_balance}, 'sell', profit_loss)
                        self.position = None
                        self.notify_telegram(f"Take Profit ejecutado @ {current_price}")

                # Nueva posición
                if signal == 'buy' and not self.position:
                    usdt_balance = self.get_balance('USDT')
                    qty = self.get_quantity(usdt_balance * 0.70)  # Usar 95% del balance
                    order = self.execute_order('buy', qty)
                    if order:
                        self.position = 'long'
                        self.entry_price = current_price
                        self.stop_loss_price = current_price * (1 - self.STOP_LOSS_PCT)
                        self.take_profit_price = current_price * (1 + self.TAKE_PROFIT_PCT)
                        self.notify_telegram(
                            f"Nueva posición LONG\nEntry: {current_price}\nSL: {self.stop_loss_price}\nTP: {self.take_profit_price}"
                        )

                elif signal == 'sell' and self.position == 'long':
                    xrp_balance = self.get_balance('XRP')
                    profit_loss = (current_price - self.entry_price) * xrp_balance
                    self.execute_order('sell', xrp_balance)
                    self.save_trade({'symbol': self.SYMBOL, 'executedQty': xrp_balance}, 'sell', profit_loss)
                    self.position = None

                # Reporte de estado
                current_value = self.get_portfolio_value()
                logging.info(f"Portfolio: {current_value:.2f} USDT | P/L: {current_value - self.initial_value:.2f}")
                latest = df.iloc[-1]
                logging.info(f"Indicadores: MA_short={latest['MA_short']:.4f}, MA_medium={latest['MA_medium']:.4f}, MA_long={latest['MA_long']:.4f}, RSI={latest['RSI']:.2f}")
                time.sleep(300)

            except Exception as e:
                logging.error(f"Error en loop principal: {e}")
                self.notify_telegram(f"Error detectado: {str(e)}")
                time.sleep(300)

if __name__ == "__main__":
    bot = TradingBot()
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.notify_telegram("Bot detenido manualmente")
        logging.info("Bot detenido por el usuario")