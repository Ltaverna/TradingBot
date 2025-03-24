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
import ccxt
import os
from dotenv import load_dotenv

class TradingBot:
    def __init__(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('trading_bot.log'),
                logging.StreamHandler()
            ]
        )
        load_dotenv('.env')
        
        # ====== Telegram ======
        self.telegram_key = os.getenv("TELEGRAM_KEY", "")
        self.bot = telebot.TeleBot(self.telegram_key)
        
        # Parseamos TELEGRAM_CHAT_ID como lista de enteros
        chat_env = os.getenv("TELEGRAM_CHAT_ID", "")
        if "," in chat_env:
            self.AUTHORIZED_CHAT_IDS = [
                int(x.strip()) for x in chat_env.split(",") if x.strip().isdigit()
            ]
        else:
            if chat_env.strip().isdigit():
                self.AUTHORIZED_CHAT_IDS = [int(chat_env.strip())]
            else:
                self.AUTHORIZED_CHAT_IDS = []

        # ====== Binance ======
        self.API_KEY = os.getenv("BINANCE_API_KEY", "")
        self.API_SECRET = os.getenv("BINANCE_API_SECRET", "")
        self.client = Client(self.API_KEY, self.API_SECRET)
        self.ccxt_client = ccxt.binance({'apiKey': self.API_KEY, 'secret': self.API_SECRET})
        
        # ====== Parámetros de trading ======
        self.SYMBOL = 'XRPUSDT'
        self.INTERVAL = Client.KLINE_INTERVAL_5MINUTE
        self.LIMIT = 300
        
        # Porcentaje de Stop Loss y Take Profit
        self.STOP_LOSS_PCT = 0.05   # 3%
        self.TAKE_PROFIT_PCT = 0.20 # 10%

        self.lock = Lock()
        
        # ====== Estado de la posición ======
        self.stop_loss_price = None
        self.take_profit_price = None
        self.entry_price = None

        # ====== Para logs y control de tiempo ======
        self.initial_value = 0
        self.last_trade_time = None
        self.cooldown_period = 300  # 5 minutos (en segundos)

    # =============== Notificaciones & DB ===============
    def notify_telegram(self, message: str):
        with self.lock:
            for chat_id in self.AUTHORIZED_CHAT_IDS:
                try:
                    self.bot.send_message(chat_id, message)
                except Exception as e:
                    logging.error(f"Error enviando mensaje a Telegram: {e}")

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
            c.execute("PRAGMA table_info(trades)")
            columns = [row[1] for row in c.fetchall()]
            if 'trade_date' not in columns:
                c.execute("ALTER TABLE trades ADD COLUMN trade_date TEXT")
                conn.commit()

    def save_trade(self, order, signal, profit_loss=0):
        try:
            timestamp = datetime.utcnow().isoformat()
            trade_date = datetime.now().date().isoformat()
            symbol = order.get("symbol", self.SYMBOL)
            side = order.get("side", signal)
            fills = order.get("fills", [{}])
            if fills:
                price = float(fills[0].get("price", 0))
            else:
                price = 0
            quantity = float(order.get("executedQty", 0))
            detalles = json.dumps(order)
            with sqlite3.connect("trades.db") as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO trades (timestamp, trade_date, symbol, side, quantity, price, order_details, profit_loss)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, trade_date, symbol, side, quantity, price, detalles, profit_loss))
                conn.commit()
        except Exception as e:
            logging.error(f"Error saving trade: {e}")

    # =============== Lectura de datos & indicadores ===============
    def get_data(self):
        try:
            klines = self.client.get_klines(symbol=self.SYMBOL, interval=self.INTERVAL, limit=self.LIMIT)
            df = pd.DataFrame(klines, columns=[
                'open_time','open','high','low','close','volume',
                'close_time','quote_asset_volume','number_of_trades',
                'taker_buy_base_asset_volume','taker_buy_quote_asset_volume','ignore'
            ])
            df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
            df['close'] = df['close'].astype(float)
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            return self.calculate_indicators(df)
        except Exception as e:
            logging.error(f"Error obteniendo datos: {e}")
            return None

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calcular RSI, CCI, EMA9, EMA21, SMA10, SMA50."""
        # RSI(14)
        rsi_period = 14
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
        loss = -delta.where(delta < 0, 0).rolling(rsi_period).mean()
        rs = gain / (loss + 1e-9)
        df['RSI'] = 100 - (100 / (1 + rs))

        # CCI(20)
        cci_period = 20
        tp = (df['high'] + df['low'] + df['close']) / 3
        ma = tp.rolling(cci_period).mean()
        md = (tp - ma).abs().rolling(cci_period).mean()
        df['CCI'] = (tp - ma) / (0.015 * md + 1e-9)

        # EMA(9) & EMA(21)
        df['EMA9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['EMA21'] = df['close'].ewm(span=21, adjust=False).mean()

        # SMA(10) & SMA(50)
        df['SMA10'] = df['close'].rolling(10).mean()
        df['SMA50'] = df['close'].rolling(50).mean()

        return df

    # =============== Detección de señal ===============
    def detect_signal(self, df: pd.DataFrame):
        """Devuelve 'buy', 'sell' o None."""
        if df is None or len(df) < 51:
            return None  # Se requiere historial suficiente
        if len(df) < 2:
            return None  # Necesitamos al menos 2 velas para comparación

        last = df.iloc[-1]
        prev = df.iloc[-2]
        votes = []

        # RSI
        if last['RSI'] < 45:
            votes.append('Buy')
        elif last['RSI'] > 57:
            votes.append('Sell')
        else:
            votes.append('Neutral')

        # CCI
        if last['CCI'] < -20:
            votes.append('Buy')
        elif last['CCI'] > 20:
            votes.append('Sell')
        else:
            votes.append('Neutral')

        # Cruce EMA9 & EMA21
        if prev['EMA9'] <= prev['EMA21'] and last['EMA9'] > last['EMA21']:
            votes.append('Buy')
        elif prev['EMA9'] >= prev['EMA21'] and last['EMA9'] < last['EMA21']:
            votes.append('Sell')
        else:
            votes.append('Neutral')

        # Cruce SMA10 & SMA50
        if (pd.notna(prev['SMA10']) and pd.notna(prev['SMA50']) and 
            pd.notna(last['SMA10']) and pd.notna(last['SMA50'])):
            if prev['SMA10'] <= prev['SMA50'] and last['SMA10'] > last['SMA50']:
                votes.append('Buy')
            elif prev['SMA10'] >= prev['SMA50'] and last['SMA10'] < last['SMA50']:
                votes.append('Sell')
            else:
                votes.append('Neutral')
        else:
            votes.append('Neutral')

        buy_count = sum(v == 'Buy' for v in votes)
        sell_count = sum(v == 'Sell' for v in votes)

        if buy_count > sell_count:
            return 'buy'
        elif sell_count > buy_count:
            return 'sell'
        else:
            return None

    # =============== Órdenes y balances ===============
    def get_balance(self, asset: str) -> float:
        try:
            bal = self.client.get_asset_balance(asset=asset)
            return float(bal['free']) if bal else 0.0
        except Exception as e:
            logging.error(f"Error obteniendo balance de {asset}: {e}")
            return 0.0

    def get_portfolio_value(self) -> float:
        """Retorna el valor total en USDT (XRP + USDT)."""
        try:
            usdt = self.get_balance('USDT')
            xrp = self.get_balance('XRP')
            price = float(self.client.get_symbol_ticker(symbol=self.SYMBOL)['price'])
            return usdt + xrp * price
        except Exception as e:
            logging.error(f"Error calculando valor del portfolio: {e}")
            return 0.0

    def adjust_quantity(self, qty: float) -> float:
        """Ajusta la cantidad 'qty' según los filtros de Binance (LOT_SIZE y MIN_NOTIONAL)."""
        try:
            info = self.client.get_symbol_info(self.SYMBOL)
            price = float(self.client.get_symbol_ticker(symbol=self.SYMBOL)['price'])
            min_notional = None
            step_size = None

            for f in info['filters']:
                if f['filterType'] == 'MIN_NOTIONAL':
                    min_notional = float(f['minNotional'])
                elif f['filterType'] == 'LOT_SIZE':
                    step_size = float(f['stepSize'])

            # Ajuste LOT_SIZE
            if step_size is not None and step_size > 0:
                qty = math.floor(qty / step_size) * step_size

            # Verificación MIN_NOTIONAL
            if min_notional is not None and min_notional > 0:
                if qty * price < min_notional:
                    qty = 0

            return qty
        except Exception as e:
            logging.error(f"Error ajustando cantidad: {e}")
            return 0

    def get_quantity_to_buy(self, usdt_amount: float) -> float:
        """
        Calcula la cantidad de XRP a comprar usando todo el balance de USDT (o lo que desees).
        Aquí lo dejamos al 100%.
        """
        try:
            price = float(self.client.get_symbol_ticker(symbol=self.SYMBOL)['price'])
            qty = usdt_amount / price
            return self.adjust_quantity(qty)
        except Exception as e:
            logging.error(f"Error calculando cantidad: {e}")
            return 0

    def execute_order(self, side: str, quantity: float):
        """
        side: 'buy' o 'sell'.
        """
        with self.lock:
            try:
                binance_side = SIDE_BUY if side == 'buy' else SIDE_SELL
                order = self.client.create_order(
                    symbol=self.SYMBOL,
                    side=binance_side,
                    type=ORDER_TYPE_MARKET,
                    quantity=quantity
                )
                price = float(order['fills'][0]['price'])
                msg = f"Orden {side.upper()} ejecutada.\nCantidad: {quantity}\nPrecio: {price}"
                self.notify_telegram(msg)
                self.save_trade(order, side)
                return order
            except Exception as e:
                logging.error(f"Error ejecutando orden {side}: {e}")
                return None

    # =============== LÓGICA PRINCIPAL ===============
    def run(self):
        self.init_db()
        self.initial_value = self.get_portfolio_value()
        logging.info(f"Valor inicial: {self.initial_value:.2f} USDT")
        self.notify_telegram(f"Bot iniciado. Valor inicial: {self.initial_value:.2f} USDT")

        while True:
            try:
                current_time = time.time()
                # Respetamos el cooldown si hicimos un trade reciente
                if self.last_trade_time and (current_time - self.last_trade_time) < self.cooldown_period:
                    time.sleep(self.cooldown_period - (current_time - self.last_trade_time))
                    continue

                # 1) Obtener datos + Señal
                df = self.get_data()
                if df is None:
                    time.sleep(300)
                    continue

                signal = self.detect_signal(df)
                current_price = float(self.client.get_symbol_ticker(symbol=self.SYMBOL)['price'])

                # 2) Ver si estamos en posición (balance XRP > 0)
                xrp_balance = self.get_balance('XRP')
                usdt_balance = self.get_balance('USDT')
                in_position = xrp_balance > 0.0001  # Umbral mínimo para considerar que hay XRP

                # Actualizar trailing stop (si estamos en posición)
                if in_position and self.entry_price:
                    # Ajustar STOP LOSS dinámico
                    if current_price > self.entry_price:
                        new_stop_loss = current_price * (1 - self.STOP_LOSS_PCT)
                        if self.stop_loss_price is not None:
                            self.stop_loss_price = max(self.stop_loss_price, new_stop_loss)
                        else:
                            self.stop_loss_price = new_stop_loss

                    # Comprobar STOP LOSS
                    if self.stop_loss_price and current_price <= self.stop_loss_price:
                        adj_xrp = self.adjust_quantity(xrp_balance)
                        if adj_xrp > 0:
                            pl = (current_price - self.entry_price) * adj_xrp
                            self.execute_order('sell', adj_xrp)
                            # Guardar trade con P/L manual
                            self.save_trade({'symbol': self.SYMBOL, 'executedQty': adj_xrp}, 'sell', pl)
                            self.notify_telegram(f"Stop Loss ejecutado @ {current_price}")
                            self._reset_position()
                            self.last_trade_time = time.time()

                    # Comprobar TAKE PROFIT
                    elif self.take_profit_price and current_price >= self.take_profit_price:
                        adj_xrp = self.adjust_quantity(xrp_balance)
                        if adj_xrp > 0:
                            pl = (current_price - self.entry_price) * adj_xrp
                            self.execute_order('sell', adj_xrp)
                            self.save_trade({'symbol': self.SYMBOL, 'executedQty': adj_xrp}, 'sell', pl)
                            self.notify_telegram(f"Take Profit ejecutado @ {current_price}")
                            self._reset_position()
                            self.last_trade_time = time.time()

                # 3) Ejecutar trades según la señal
                if signal == 'buy':
                    # Si no estoy en posición y tengo USDT, compro
                    if not in_position and usdt_balance > 1:
                        qty = self.get_quantity_to_buy(usdt_balance)
                        if qty > 0:
                            order = self.execute_order('buy', qty)
                            if order:
                                self.entry_price = current_price
                                self.stop_loss_price = current_price * (1 - self.STOP_LOSS_PCT)
                                self.take_profit_price = current_price * (1 + self.TAKE_PROFIT_PCT)
                                self.last_trade_time = time.time()
                                self.notify_telegram(
                                    f"Compra ejecutada @ {current_price}\n"
                                    f"SL={self.stop_loss_price}, TP={self.take_profit_price}"
                                )

                elif signal == 'sell':
                    # Si estoy en posición (tengo XRP), vendo
                    if in_position:
                        adj_xrp = self.adjust_quantity(xrp_balance)
                        if adj_xrp > 0:
                            pl = (current_price - self.entry_price) * adj_xrp if self.entry_price else 0
                            self.execute_order('sell', adj_xrp)
                            self.save_trade({'symbol': self.SYMBOL, 'executedQty': adj_xrp}, 'sell', pl)
                            self.notify_telegram(f"Venta ejecutada @ {current_price}")
                            self._reset_position()
                            self.last_trade_time = time.time()

                # 4) Log final
                current_value = self.get_portfolio_value()
                logging.info(
                    f"Portfolio: {current_value:.2f} USDT | "
                    f"P/L: {current_value - self.initial_value:.2f}"
                )
                last_row = df.iloc[-1]
                logging.info(
                    f"Indicadores: RSI={last_row['RSI']:.2f}, CCI={last_row['CCI']:.2f}, "
                    f"EMA9={last_row['EMA9']:.4f}, EMA21={last_row['EMA21']:.4f}, "
                    f"SMA10={last_row['SMA10']:.4f}, SMA50={last_row['SMA50']:.4f}"
                )

                time.sleep(300)  # Esperar 5 minutos por la siguiente iteración

            except Exception as e:
                logging.error(f"Error en loop principal: {e}")
                self.notify_telegram(f"Error detectado: {str(e)}")
                time.sleep(300)

    def _reset_position(self):
        """Resetea variables de la posición para arrancar de cero."""
        self.entry_price = None
        self.stop_loss_price = None
        self.take_profit_price = None

if __name__ == "__main__":
    bot = TradingBot()
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.notify_telegram("Bot detenido manualmente")
        logging.info("Bot detenido por el usuario")
    except Exception as e:
        print("Error en el bot:", e)