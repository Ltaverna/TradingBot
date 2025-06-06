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

def load_historical_data_from_csv(filepath: str) -> pd.DataFrame:
    df = pd.read_csv(filepath)
    # Assuming timestamp is in milliseconds in the CSV
    # If 'timestamp' is the first column and is the index, it might be read as string
    # Or if it's a regular column named 'timestamp'
    if 'timestamp' in df.columns:
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
    elif df.index.name == 'timestamp' or (df.shape[1] > 0 and 'time' in df.columns[0].lower()): # Crude check if first col is timestamp
        # Attempt to parse index if it looks like a millisecond timestamp
        try:
            df.index = pd.to_datetime(df.index, unit='ms')
            df.index.name = 'timestamp'
        except (ValueError, TypeError):
            # If direct conversion fails, try assuming it's already a datetime-like string
            try:
                df.index = pd.to_datetime(df.index)
                df.index.name = 'timestamp'
            except Exception as e:
                print(f"Could not parse index as datetime: {e}")
                # Or, if the first column is named 'timestamp' but not the index yet
                if 'timestamp' in df.columns: # Should have been caught above, but as fallback
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    df.set_index('timestamp', inplace=True)

    # Ensure correct dtypes for ohlcv columns
    ohlcv_cols = ['open', 'high', 'low', 'close', 'volume']
    for col in ohlcv_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce') # errors='coerce' will turn non-numeric to NaN
    return df

def calculate_and_display_performance_metrics(trades_log, final_portfolio_value, initial_usdt_balance, portfolio_over_time, commission_rate):
    print("\n--- Backtest Performance Metrics ---")

    if not portfolio_over_time:
        print("No portfolio data to analyze.")
        print("--- End of Report ---")
        return

    print(f"Period: {portfolio_over_time[0]['timestamp'].date()} to {portfolio_over_time[-1]['timestamp'].date()}")
    print(f"Initial Portfolio Value: {initial_usdt_balance:.2f} USDT")
    print(f"Final Portfolio Value:   {final_portfolio_value:.2f} USDT")

    total_net_pl = final_portfolio_value - initial_usdt_balance
    total_net_pl_pct = (total_net_pl / initial_usdt_balance) * 100 if initial_usdt_balance else 0
    print(f"Total Net Profit/Loss:   {total_net_pl:.2f} USDT ({total_net_pl_pct:.2f}%)")

    num_trades = len(trades_log)
    print(f"Total Trades:            {num_trades}")

    if num_trades > 0:
        winning_trades = [t for t in trades_log if t.get('pnl', 0) > 0 and ('sell' in t['type'] or 'sl' in t['type'] or 'tp' in t['type'])] # PNL is relevant for sells
        losing_trades = [t for t in trades_log if t.get('pnl', 0) <= 0 and ('sell' in t['type'] or 'sl' in t['type'] or 'tp' in t['type'])]

        num_winning_trades = len(winning_trades)
        num_losing_trades = len(losing_trades) # Trades with PNL <= 0 from sells

        win_rate = (num_winning_trades / (num_winning_trades + num_losing_trades)) * 100 if (num_winning_trades + num_losing_trades) > 0 else 0

        avg_pl_per_trade = total_net_pl / num_trades # Considers all trades for overall P/L average

        avg_win_pl = sum(t['pnl'] for t in winning_trades) / num_winning_trades if num_winning_trades else 0
        avg_loss_pl = sum(t['pnl'] for t in losing_trades) / num_losing_trades if num_losing_trades else 0

        gross_profit = sum(t['pnl'] for t in winning_trades)
        gross_loss = abs(sum(t['pnl'] for t in losing_trades))
        profit_factor = gross_profit / gross_loss if gross_loss else float('inf')

        print(f"Winning Trades:          {num_winning_trades}")
        print(f"Losing Trades (P&L <=0): {num_losing_trades}") # Clarify this counts PNL-affecting trades
        print(f"Win Rate (on P&L trades):{win_rate:.2f}%")
        print(f"Average P/L per Trade:   {avg_pl_per_trade:.2f} USDT")
        print(f"Average Winning P/L:     {avg_win_pl:.2f} USDT")
        print(f"Average Losing P/L:      {avg_loss_pl:.2f} USDT") # Will be negative or zero
        print(f"Profit Factor:           {profit_factor:.2f}")

    # Commission calculation based on the log structure from previous steps
    # Buy trades log: 'commission_asset', 'price', 'type':'buy'
    # Sell trades log: 'commission' (in USDT), 'type':'sell_sl'/'sell_tp'/'sell'
    total_commissions_usdt = 0
    for t in trades_log:
        if t['type'] == 'buy':
            # Buy commission was logged as 'commission' in USDT directly in the previous step's buy logic.
            # The buy logic was:
            # cost_before_commission = quantity_to_buy_asset * trade_price
            # commission_paid_usdt = cost_before_commission * self.commission_rate
            # ... log entry ... 'commission': commission_paid_usdt ...
            total_commissions_usdt += t.get('commission', 0)
        elif 'sell' in t['type']: # covers 'sell', 'sell_sl', 'sell_tp'
            total_commissions_usdt += t.get('commission', 0)

    print(f"Total Commissions Paid:  {total_commissions_usdt:.2f} USDT (Rate: {commission_rate*100:.3f}%)")

    # Max Drawdown Calculation
    peak_value = initial_usdt_balance
    max_drawdown_pct = 0.0
    current_drawdown_pct = 0.0
    for entry in portfolio_over_time:
        current_value = entry['value']
        peak_value = max(peak_value, current_value)
        if peak_value > 0 : # Avoid division by zero if peak is somehow zero
            drawdown = (peak_value - current_value) / peak_value
            current_drawdown_pct = drawdown # Current drawdown from the most recent peak
            max_drawdown_pct = max(max_drawdown_pct, current_drawdown_pct)
        else: # Should not happen with positive initial balance
            max_drawdown_pct = 0 # Or handle as an undefined case

    print(f"Max Drawdown:            {max_drawdown_pct*100:.2f}%")
    print("--- End of Report ---")

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

    def get_today_trades(self):
        try:
            today = datetime.now().date().isoformat()  # Obtener la fecha local de hoy
            with sqlite3.connect("trades.db") as conn:
                c = conn.cursor()
                # Count both 'buy' and 'sell' trades for the day
                c.execute("SELECT COUNT(*) FROM trades WHERE trade_date = ? AND (side = 'buy' OR side = 'sell')", (today,))
                count = c.fetchone()[0]
                return count
        except Exception as e:
            logging.error(f"Error getting today's trades: {e}")
            return 0 # Return 0 in case of error to avoid blocking trading indefinitely

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
                # Check daily trade limit
                daily_trades_count = self.get_today_trades()
                if daily_trades_count >= 2:
                    logging.info(f"Daily trade limit of 2 reached ({daily_trades_count} trades). Pausing activity.")
                    time.sleep(300)  # Pause for 5 minutes before checking again
                    continue

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

    def run_backtest(self, historical_data_filepath: str, initial_usdt_balance: float, commission_rate: float, start_date_str: str = None, end_date_str: str = None):
        # Initialization for backtesting state
        self.usdt_balance = initial_usdt_balance
        self.asset_balance = 0.0  # Assuming SYMBOL is the asset, e.g., XRP
        self.commission_rate = commission_rate
        self.portfolio_over_time = [] # For tracking portfolio value over time for drawdown, etc.

        # Reset live trading position state if any, ensure clean slate for backtest-specific tracking
        self.entry_price = None
        # self.stop_loss_price and self.take_profit_price are already part of the class,
        # they will be managed by the backtest logic

        self.in_position = False # Tracks if a position is currently open in the backtest
        self.trades_log = []     # List to store dictionaries of trade details
        self.daily_trade_counts = {} # Dictionary to store trades per day: {'YYYY-MM-DD': count}

        # Load historical data
        historical_df = load_historical_data_from_csv(historical_data_filepath)

        # Apply date filtering
        if start_date_str:
            try:
                start_dt = pd.to_datetime(start_date_str)
                historical_df = historical_df[historical_df.index >= start_dt]
                logging.info(f"Filtered historical data from start date: {start_date_str}")
            except ValueError:
                logging.error(f"Invalid start_date format: {start_date_str}. Please use YYYY-MM-DD. Proceeding without start date filter.")

        if end_date_str:
            try:
                # end_dt is exclusive, so data up to end_date_str 23:59:59... is included
                end_dt_exclusive = pd.to_datetime(end_date_str) + pd.Timedelta(days=1)
                historical_df = historical_df[historical_df.index < end_dt_exclusive]
                logging.info(f"Filtered historical data up to end date: {end_date_str} (data until beginning of next day)")
            except ValueError:
                logging.error(f"Invalid end_date format: {end_date_str}. Please use YYYY-MM-DD. Proceeding without end date filter.")

        if historical_df.empty:
            logging.error("Historical data is empty after filtering (or originally), cannot run backtest.")
            # Return empty portfolio_over_time as well
            return [], initial_usdt_balance, initial_usdt_balance, []

        logging.info(f"Starting backtest with initial balance: {initial_usdt_balance:.2f} USDT, commission: {commission_rate*100:.2f}%")
        # Update log message to reflect filtering
        logging.info(f"Using {len(historical_df)} historical klines for {self.SYMBOL} (after date filtering if any)")

        indicator_warmup_period = 50 # Should match the longest period in calculate_indicators (e.g. SMA50)

        for i, current_kline_series in historical_df.iterrows():
            current_kline = current_kline_series.to_dict() # Convert row to dict for easier access
            current_timestamp = i # Index is the timestamp

            # Use 'close' price of the current kline for most operations, including portfolio valuation at kline end.
            current_market_price_for_kline = current_kline['close']

            current_date_str = current_timestamp.date().isoformat()
            trades_today = self.daily_trade_counts.get(current_date_str, 0)

            # --- Stop-Loss/Take-Profit Check ---
            if self.in_position:
                triggered_sl_tp = False
                sell_price_sl_tp = 0
                sl_tp_type = ""

                # Check Stop Loss: if current kline's low hits or goes below SL price
                if self.stop_loss_price and current_kline['low'] <= self.stop_loss_price:
                    sell_price_sl_tp = self.stop_loss_price # Execute at SL price
                    sl_tp_type = 'sell_sl'
                    triggered_sl_tp = True
                    logging.info(f"{current_timestamp}: Stop-loss triggered at {sell_price_sl_tp:.2f}")
                # Check Take Profit: if current kline's high hits or goes above TP price
                elif self.take_profit_price and current_kline['high'] >= self.take_profit_price:
                    sell_price_sl_tp = self.take_profit_price # Execute at TP price
                    sl_tp_type = 'sell_tp'
                    triggered_sl_tp = True
                    logging.info(f"{current_timestamp}: Take-profit triggered at {sell_price_sl_tp:.2f}")

                if triggered_sl_tp:
                    # Closing a position counts towards daily trade limit
                    if trades_today >= 2 and sl_tp_type:
                        logging.warning(f"{current_timestamp}: SL/TP for {sl_tp_type} would execute, but daily limit of 2 trades already met for {current_date_str}. Position remains open.")
                        pass # Allow SL/TP to proceed, it will increment trades_today.

                    quantity_to_sell = self.asset_balance
                    gross_proceeds = quantity_to_sell * sell_price_sl_tp
                    commission = gross_proceeds * self.commission_rate
                    net_proceeds = gross_proceeds - commission

                    entry_cost_of_assets_sold = self.entry_price * quantity_to_sell if self.entry_price else 0
                    pnl = net_proceeds - entry_cost_of_assets_sold

                    self.usdt_balance += net_proceeds
                    self.trades_log.append({
                        'timestamp': current_timestamp, 'type': sl_tp_type,
                        'price': sell_price_sl_tp, 'quantity': quantity_to_sell,
                        'commission': commission, 'pnl': pnl,
                        'usdt_balance': self.usdt_balance
                    })
                    logging.info(f"SL/TP Trade: {sl_tp_type.upper()} {quantity_to_sell} {self.SYMBOL} at {sell_price_sl_tp:.2f}, P&L: {pnl:.2f}, Commission: {commission:.2f}")

                    # Update portfolio value after SL/TP trade
                    current_total_value_after_sl_tp = self.usdt_balance + (self.asset_balance * sell_price_sl_tp) # asset_balance is now 0
                    self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value_after_sl_tp})

                    self.asset_balance = 0
                    self.in_position = False
                    self.entry_price = None
                    self.stop_loss_price = None
                    self.take_profit_price = None
                    self.daily_trade_counts[current_date_str] = trades_today + 1
                    continue # Skip further signal detection for this kline as position is closed

            # --- Indicator Calculation & Signal Detection ---
            # current_market_price_for_kline is already defined as current_kline['close']
            current_loc = historical_df.index.get_loc(current_timestamp)
            if current_loc < indicator_warmup_period: # Not enough data for reliable indicators yet
                # Record portfolio value even if skipping indicators/trades
                current_total_value = self.usdt_balance + (self.asset_balance * current_market_price_for_kline)
                self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value})
                continue

            df_for_indicators = historical_df.iloc[:current_loc + 1].copy()
            indicators_df = self.calculate_indicators(df_for_indicators)
            if indicators_df is None or indicators_df.empty:
                current_total_value = self.usdt_balance + (self.asset_balance * current_market_price_for_kline)
                self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value})
                continue

            signal = self.detect_signal(indicators_df)
            trade_price = current_market_price_for_kline # Use close price of current kline for trade execution

            # --- Buy Logic ---
            if signal == 'buy' and not self.in_position and trades_today < 2:
                usdt_to_spend_for_buy = self.usdt_balance * 0.95 # Use 95% of available USDT balance

                if usdt_to_spend_for_buy > 0: # Ensure there's USDT to spend
                    # Calculate quantity considering commission is paid from USDT
                    # Total USDT spent = (quantity * price) + (quantity * price * commission_rate)
                    # Total USDT spent = (quantity * price) * (1 + commission_rate)
                    # So, quantity = Total USDT spent / (price * (1 + commission_rate))

                    quantity_to_buy_asset = usdt_to_spend_for_buy / (trade_price * (1 + self.commission_rate))

                    # If using fixed precision for asset (e.g. XRP often 1 decimal place, or from symbol info)
                    # quantity_to_buy_asset = math.floor(quantity_to_buy_asset * 10) / 10 # Example for 1 decimal place

                    if quantity_to_buy_asset > 0:
                        cost_before_commission = quantity_to_buy_asset * trade_price
                        commission_paid_usdt = cost_before_commission * self.commission_rate
                        net_cost_usdt_for_trade = cost_before_commission + commission_paid_usdt

                        # Update balances
                        self.asset_balance += quantity_to_buy_asset
                        self.usdt_balance -= net_cost_usdt_for_trade

                        # Set position details
                        self.in_position = True
                        self.entry_price = trade_price # Store actual execution price before commission for P&L
                                                       # Or, effective price: net_cost_usdt_for_trade / quantity_to_buy_asset
                        self.stop_loss_price = trade_price * (1 - self.STOP_LOSS_PCT)
                        self.take_profit_price = trade_price * (1 + self.TAKE_PROFIT_PCT)

                        # Log trade
                        self.trades_log.append({
                            'timestamp': current_timestamp, 'type': 'buy',
                            'price': trade_price, 'quantity': quantity_to_buy_asset,
                            'commission': commission_paid_usdt, 'cost_usdt': net_cost_usdt_for_trade,
                            'usdt_balance': self.usdt_balance, 'asset_balance': self.asset_balance
                        })
                        self.daily_trade_counts[current_date_str] = trades_today + 1
                        logging.info(f"{current_timestamp}: BUY {quantity_to_buy_asset:.4f} {self.SYMBOL} at {trade_price:.2f}, Cost: {net_cost_usdt_for_trade:.2f}, Commission: {commission_paid_usdt:.2f}")

                        # Update portfolio value after buy trade
                        current_total_value_after_buy = self.usdt_balance + (self.asset_balance * trade_price)
                        self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value_after_buy})

            # --- Sell Logic ---
            elif signal == 'sell' and self.in_position and trades_today < 2:
                quantity_to_sell = self.asset_balance # Sell all current holdings

                if quantity_to_sell > 0:
                    gross_proceeds = quantity_to_sell * trade_price
                    commission_paid_usdt_sell = gross_proceeds * self.commission_rate
                    net_proceeds = gross_proceeds - commission_paid_usdt_sell

                    # Calculate P&L for this trade
                    # entry_price was stored as the price *before* buy commission
                    entry_cost_of_assets_sold = self.entry_price * quantity_to_sell if self.entry_price else 0
                    pnl_trade = net_proceeds - entry_cost_of_assets_sold # PNL for this specific trade cycle

                    # Update balances
                    self.usdt_balance += net_proceeds
                    self.asset_balance = 0

                    # Reset position details
                    self.in_position = False
                    # Store entry_price before resetting for the log
                    logged_entry_price = self.entry_price
                    self.entry_price = None
                    self.stop_loss_price = None
                    self.take_profit_price = None

                    # Log trade
                    self.trades_log.append({
                        'timestamp': current_timestamp, 'type': 'sell',
                        'price': trade_price, 'quantity': quantity_to_sell,
                        'commission': commission_paid_usdt_sell, 'pnl': pnl_trade,
                        'usdt_balance': self.usdt_balance, 'asset_balance': self.asset_balance,
                        'entry_price_for_this_lot': logged_entry_price
                    })
                    self.daily_trade_counts[current_date_str] = trades_today + 1
                    logging.info(f"{current_timestamp}: SELL {quantity_to_sell:.4f} {self.SYMBOL} at {trade_price:.2f}, Proceeds: {net_proceeds:.2f}, Commission: {commission_paid_usdt_sell:.2f}, P&L: {pnl_trade:.2f}")

                    # Update portfolio value after sell trade
                    current_total_value_after_sell = self.usdt_balance + (self.asset_balance * trade_price) # asset_balance is now 0
                    self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value_after_sell})

            else: # No trade occurred in this kline based on signal
                # Still record portfolio value at the end of this kline if no trade happened
                # Check if portfolio_over_time was already updated by SL/TP, Buy, or Sell logic for this timestamp
                # This 'else' branch might lead to duplicate entries if not careful.
                # A better way is to ensure one update per kline at the end, unless a trade has just updated it.
                # Let's ensure it's only appended if no trade happened in this iteration for this timestamp.
                # A simple check: if the last timestamp in portfolio_over_time is not current_timestamp
                if not self.portfolio_over_time or self.portfolio_over_time[-1]['timestamp'] != current_timestamp:
                    current_total_value = self.usdt_balance + (self.asset_balance * current_market_price_for_kline)
                    self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value})


        # At the end of the loop, calculate final portfolio value
        last_price = historical_df.iloc[-1]['close'] if not historical_df.empty else 0
        final_portfolio_value = self.usdt_balance + (self.asset_balance * last_price)

        # Ensure one final portfolio value entry if historical_df was not empty
        if not historical_df.empty and (not self.portfolio_over_time or self.portfolio_over_time[-1]['timestamp'] != historical_df.index[-1]):
             self.portfolio_over_time.append({'timestamp': historical_df.index[-1], 'value': final_portfolio_value})
        elif not historical_df.empty and self.portfolio_over_time and self.portfolio_over_time[-1]['timestamp'] == historical_df.index[-1]:
            # If last entry is for the same timestamp, update its value to be the final calculated one
            self.portfolio_over_time[-1]['value'] = final_portfolio_value


        logging.info(f"Backtest completed. Final portfolio value: {final_portfolio_value:.2f} USDT")
        return self.trades_log, final_portfolio_value, initial_usdt_balance, self.portfolio_over_time

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trading Bot for Binance or Backtesting")
    parser.add_argument("--backtest", action="store_true", help="Run in backtesting mode.")
    parser.add_argument("--data-file", type=str, default="historical_data.csv", help="Path to historical data CSV file for backtesting.")
    parser.add_argument("--initial-balance", type=float, default=1000.0, help="Initial USDT balance for backtesting.")
    parser.add_argument("--commission-rate", type=float, default=0.001, help="Commission rate for trades (e.g., 0.001 for 0.1%).")
    parser.add_argument("--start-date", type=str, default=None, help="Start date for backtesting (YYYY-MM-DD).")
    parser.add_argument("--end-date", type=str, default=None, help="End date for backtesting (YYYY-MM-DD).")

    args = parser.parse_args()

    bot = TradingBot()

    if args.backtest:
        if not os.path.exists(args.data_file):
            print(f"Error: Historical data file not found: {args.data_file}")
            logging.error(f"Historical data file not found: {args.data_file}")
            # Consider exiting or handling this more gracefully
        else:
            print(f"Running in backtest mode with data from: {args.data_file}")
            logging.info(f"Starting backtest mode. Data: {args.data_file}, Initial Balance: {args.initial_balance}, Commission: {args.commission_rate}, StartDate: {args.start_date}, EndDate: {args.end_date}")

            trades_log, final_value, initial_value, portfolio_over_time = bot.run_backtest(
                historical_data_filepath=args.data_file,
                initial_usdt_balance=args.initial_balance,
                commission_rate=args.commission_rate,
                start_date_str=args.start_date,
                end_date_str=args.end_date
            )

            # Call the new function to display detailed metrics
            calculate_and_display_performance_metrics(
                trades_log,
                final_value,
                initial_value,
                portfolio_over_time,
                args.commission_rate # Pass the commission rate from args
            )

            # The old summary print statements are now covered by calculate_and_display_performance_metrics
            # So, they can be removed if they were separate. The prompt implies replacing them.
            logging.info("Backtest finished.") # Keep general log message

    else:
        # Live trading mode (original main execution)
        print("Starting live trading bot...")
        logging.info("Starting live trading bot...")
        try:
            bot.run()
        except KeyboardInterrupt:
            bot.notify_telegram("Bot detenido manualmente")
            logging.info("Bot detenido por el usuario")
        except Exception as e:
            logging.error(f"Error en el bot (live mode): {e}", exc_info=True)
            print(f"Error en el bot (live mode): {e}")


    def run_backtest(self, historical_data_filepath: str, initial_usdt_balance: float, commission_rate: float):
        # Initialization for backtesting state
        self.usdt_balance = initial_usdt_balance
        self.asset_balance = 0.0  # Assuming SYMBOL is the asset, e.g., XRP
        self.commission_rate = commission_rate
        self.portfolio_over_time = [] # For tracking portfolio value over time for drawdown, etc.

        # Reset live trading position state if any, ensure clean slate for backtest-specific tracking
        self.entry_price = None
        # self.stop_loss_price and self.take_profit_price are already part of the class,
        # they will be managed by the backtest logic

        self.in_position = False # Tracks if a position is currently open in the backtest
        self.trades_log = []     # List to store dictionaries of trade details
        self.daily_trade_counts = {} # Dictionary to store trades per day: {'YYYY-MM-DD': count}

        # Load historical data
        # Ensure the global function load_historical_data_from_csv is defined
        historical_df = load_historical_data_from_csv(historical_data_filepath)
        if historical_df.empty:
            logging.error("Historical data is empty, cannot run backtest.")
            return [], initial_usdt_balance, initial_usdt_balance

        logging.info(f"Starting backtest with initial balance: {initial_usdt_balance:.2f} USDT, commission: {commission_rate*100:.2f}%")
        logging.info(f"Loaded {len(historical_df)} historical klines for {self.SYMBOL} from {historical_data_filepath}")

        indicator_warmup_period = 50 # Should match the longest period in calculate_indicators (e.g. SMA50)

        for i, current_kline_series in historical_df.iterrows():
            current_kline = current_kline_series.to_dict() # Convert row to dict for easier access
            current_timestamp = i # Index is the timestamp

            # Use 'close' price of the current kline for most operations, including portfolio valuation at kline end.
            current_market_price_for_kline = current_kline['close']

            current_date_str = current_timestamp.date().isoformat()
            trades_today = self.daily_trade_counts.get(current_date_str, 0)

            # --- Stop-Loss/Take-Profit Check ---
            if self.in_position:
                triggered_sl_tp = False
                sell_price_sl_tp = 0
                sl_tp_type = ""

                # Check Stop Loss: if current kline's low hits or goes below SL price
                if self.stop_loss_price and current_kline['low'] <= self.stop_loss_price:
                    sell_price_sl_tp = self.stop_loss_price # Execute at SL price
                    sl_tp_type = 'sell_sl'
                    triggered_sl_tp = True
                    logging.info(f"{current_timestamp}: Stop-loss triggered at {sell_price_sl_tp:.2f}")
                # Check Take Profit: if current kline's high hits or goes above TP price
                elif self.take_profit_price and current_kline['high'] >= self.take_profit_price:
                    sell_price_sl_tp = self.take_profit_price # Execute at TP price
                    sl_tp_type = 'sell_tp'
                    triggered_sl_tp = True
                    logging.info(f"{current_timestamp}: Take-profit triggered at {sell_price_sl_tp:.2f}")

                if triggered_sl_tp:
                    # Closing a position counts towards daily trade limit
                    if trades_today >= 2 and sl_tp_type: # Check if limit already reached by prior trades today
                        logging.warning(f"{current_timestamp}: SL/TP for {sl_tp_type} would execute, but daily limit of 2 trades already met for {current_date_str}. Position remains open.")
                        # This is a choice: strictly enforce "no more than 2 trades of any kind" vs "SL/TP must execute".
                        # For now, let's assume SL/TP *must* execute to protect capital / lock profit,
                        # but it will be logged as exceeding the desired "new trade" limit if that's the case.
                        # The problem statement says "skip trading logic... but still check for SL/TP"
                        # "Closing a position counts as a trade" in the SL/TP section.
                        # This implies SL/TP can proceed but increments the count.
                        pass # Allow SL/TP to proceed, it will increment trades_today.

                    quantity_to_sell = self.asset_balance
                    gross_proceeds = quantity_to_sell * sell_price_sl_tp
                    commission = gross_proceeds * self.commission_rate
                    net_proceeds = gross_proceeds - commission

                    entry_cost_of_assets_sold = self.entry_price * quantity_to_sell if self.entry_price else 0
                    pnl = net_proceeds - entry_cost_of_assets_sold

                    self.usdt_balance += net_proceeds
                    self.trades_log.append({
                        'timestamp': current_timestamp, 'type': sl_tp_type,
                        'price': sell_price_sl_tp, 'quantity': quantity_to_sell,
                        'commission': commission, 'pnl': pnl,
                        'usdt_balance': self.usdt_balance
                    })
                    logging.info(f"SL/TP Trade: {sl_tp_type.upper()} {quantity_to_sell} {self.SYMBOL} at {sell_price_sl_tp:.2f}, P&L: {pnl:.2f}, Commission: {commission:.2f}")

                    # Update portfolio value after SL/TP trade
                    current_total_value_after_sl_tp = self.usdt_balance + (self.asset_balance * sell_price_sl_tp) # asset_balance is now 0
                    self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value_after_sl_tp})

                    self.asset_balance = 0
                    self.in_position = False
                    self.entry_price = None
                    self.stop_loss_price = None
                    self.take_profit_price = None
                    self.daily_trade_counts[current_date_str] = trades_today + 1
                    continue # Skip further signal detection for this kline as position is closed

            # --- Indicator Calculation & Signal Detection ---
            # current_market_price_for_kline is already defined as current_kline['close']
            current_loc = historical_df.index.get_loc(current_timestamp)
            if current_loc < indicator_warmup_period: # Not enough data for reliable indicators yet
                # Record portfolio value even if skipping indicators/trades
                current_total_value = self.usdt_balance + (self.asset_balance * current_market_price_for_kline)
                self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value})
                continue

            df_for_indicators = historical_df.iloc[:current_loc + 1].copy()
            indicators_df = self.calculate_indicators(df_for_indicators)
            if indicators_df is None or indicators_df.empty:
                current_total_value = self.usdt_balance + (self.asset_balance * current_market_price_for_kline)
                self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value})
                continue

            signal = self.detect_signal(indicators_df)
            trade_price = current_market_price_for_kline # Use close price of current kline for trade execution

            # --- Buy Logic ---
            if signal == 'buy' and not self.in_position and trades_today < 2:
                usdt_to_spend_for_buy = self.usdt_balance * 0.95 # Use 95% of available USDT balance

                if usdt_to_spend_for_buy > 0: # Ensure there's USDT to spend
                    # Calculate quantity considering commission is paid from USDT
                    # Total USDT spent = (quantity * price) + (quantity * price * commission_rate)
                    # Total USDT spent = (quantity * price) * (1 + commission_rate)
                    # So, quantity = Total USDT spent / (price * (1 + commission_rate))

                    quantity_to_buy_asset = usdt_to_spend_for_buy / (trade_price * (1 + self.commission_rate))

                    # If using fixed precision for asset (e.g. XRP often 1 decimal place, or from symbol info)
                    # quantity_to_buy_asset = math.floor(quantity_to_buy_asset * 10) / 10 # Example for 1 decimal place

                    if quantity_to_buy_asset > 0:
                        cost_before_commission = quantity_to_buy_asset * trade_price
                        commission_paid_usdt = cost_before_commission * self.commission_rate
                        net_cost_usdt_for_trade = cost_before_commission + commission_paid_usdt

                        # Update balances
                        self.asset_balance += quantity_to_buy_asset
                        self.usdt_balance -= net_cost_usdt_for_trade

                        # Set position details
                        self.in_position = True
                        self.entry_price = trade_price # Store actual execution price before commission for P&L
                                                       # Or, effective price: net_cost_usdt_for_trade / quantity_to_buy_asset
                        self.stop_loss_price = trade_price * (1 - self.STOP_LOSS_PCT)
                        self.take_profit_price = trade_price * (1 + self.TAKE_PROFIT_PCT)

                        # Log trade
                        self.trades_log.append({
                            'timestamp': current_timestamp, 'type': 'buy',
                            'price': trade_price, 'quantity': quantity_to_buy_asset,
                            'commission': commission_paid_usdt, 'cost_usdt': net_cost_usdt_for_trade,
                            'usdt_balance': self.usdt_balance, 'asset_balance': self.asset_balance
                        })
                        self.daily_trade_counts[current_date_str] = trades_today + 1
                        logging.info(f"{current_timestamp}: BUY {quantity_to_buy_asset:.4f} {self.SYMBOL} at {trade_price:.2f}, Cost: {net_cost_usdt_for_trade:.2f}, Commission: {commission_paid_usdt:.2f}")

                        # Update portfolio value after buy trade
                        current_total_value_after_buy = self.usdt_balance + (self.asset_balance * trade_price)
                        self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value_after_buy})

            # --- Sell Logic ---
            elif signal == 'sell' and self.in_position and trades_today < 2:
                quantity_to_sell = self.asset_balance # Sell all current holdings

                if quantity_to_sell > 0:
                    gross_proceeds = quantity_to_sell * trade_price
                    commission_paid_usdt_sell = gross_proceeds * self.commission_rate
                    net_proceeds = gross_proceeds - commission_paid_usdt_sell

                    # Calculate P&L for this trade
                    # entry_price was stored as the price *before* buy commission
                    entry_cost_of_assets_sold = self.entry_price * quantity_to_sell if self.entry_price else 0
                    pnl_trade = net_proceeds - entry_cost_of_assets_sold # PNL for this specific trade cycle

                    # Update balances
                    self.usdt_balance += net_proceeds
                    self.asset_balance = 0

                    # Reset position details
                    self.in_position = False
                    # Store entry_price before resetting for the log
                    logged_entry_price = self.entry_price
                    self.entry_price = None
                    self.stop_loss_price = None
                    self.take_profit_price = None

                    # Log trade
                    self.trades_log.append({
                        'timestamp': current_timestamp, 'type': 'sell',
                        'price': trade_price, 'quantity': quantity_to_sell,
                        'commission': commission_paid_usdt_sell, 'pnl': pnl_trade,
                        'usdt_balance': self.usdt_balance, 'asset_balance': self.asset_balance,
                        'entry_price_for_this_lot': logged_entry_price
                    })
                    self.daily_trade_counts[current_date_str] = trades_today + 1
                    logging.info(f"{current_timestamp}: SELL {quantity_to_sell:.4f} {self.SYMBOL} at {trade_price:.2f}, Proceeds: {net_proceeds:.2f}, Commission: {commission_paid_usdt_sell:.2f}, P&L: {pnl_trade:.2f}")

                    # Update portfolio value after sell trade
                    current_total_value_after_sell = self.usdt_balance + (self.asset_balance * trade_price) # asset_balance is now 0
                    self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value_after_sell})

            else: # No trade occurred in this kline based on signal
                # Still record portfolio value at the end of this kline if no trade happened
                # Check if portfolio_over_time was already updated by SL/TP, Buy, or Sell logic for this timestamp
                # This 'else' branch might lead to duplicate entries if not careful.
                # A better way is to ensure one update per kline at the end, unless a trade has just updated it.
                # Let's ensure it's only appended if no trade happened in this iteration for this timestamp.
                # A simple check: if the last timestamp in portfolio_over_time is not current_timestamp
                if not self.portfolio_over_time or self.portfolio_over_time[-1]['timestamp'] != current_timestamp:
                    current_total_value = self.usdt_balance + (self.asset_balance * current_market_price_for_kline)
                    self.portfolio_over_time.append({'timestamp': current_timestamp, 'value': current_total_value})


        # At the end of the loop, calculate final portfolio value
        last_price = historical_df.iloc[-1]['close'] if not historical_df.empty else 0
        final_portfolio_value = self.usdt_balance + (self.asset_balance * last_price)

        # Ensure one final portfolio value entry if historical_df was not empty
        if not historical_df.empty and (not self.portfolio_over_time or self.portfolio_over_time[-1]['timestamp'] != historical_df.index[-1]):
             self.portfolio_over_time.append({'timestamp': historical_df.index[-1], 'value': final_portfolio_value})
        elif not historical_df.empty and self.portfolio_over_time and self.portfolio_over_time[-1]['timestamp'] == historical_df.index[-1]:
            # If last entry is for the same timestamp, update its value to be the final calculated one
            self.portfolio_over_time[-1]['value'] = final_portfolio_value


        logging.info(f"Backtest completed. Final portfolio value: {final_portfolio_value:.2f} USDT")
        return self.trades_log, final_portfolio_value, initial_usdt_balance, self.portfolio_over_time

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trading Bot for Binance or Backtesting")
    parser.add_argument("--backtest", action="store_true", help="Run in backtesting mode.")
    parser.add_argument("--data-file", type=str, default="historical_data.csv", help="Path to historical data CSV file for backtesting.")
    parser.add_argument("--initial-balance", type=float, default=1000.0, help="Initial USDT balance for backtesting.")
    parser.add_argument("--commission-rate", type=float, default=0.001, help="Commission rate for trades (e.g., 0.001 for 0.1%).")

    args = parser.parse_args()

    bot = TradingBot()

    if args.backtest:
        if not os.path.exists(args.data_file):
            print(f"Error: Historical data file not found: {args.data_file}")
            logging.error(f"Historical data file not found: {args.data_file}")
            # Consider exiting or handling this more gracefully
        else:
            print(f"Running in backtest mode with data from: {args.data_file}")
            logging.info(f"Starting backtest mode. Data: {args.data_file}, Initial Balance: {args.initial_balance}, Commission: {args.commission_rate}")

            # Ensure bot's internal logging is set up if not done in __init__ already for backtesting
            # For example, if __init__ has handlers that only make sense for live mode.
            # However, the current __init__ seems fine for both.

            trades_log, final_value, initial_value = bot.run_backtest(
                historical_data_filepath=args.data_file,
                initial_usdt_balance=args.initial_balance,
                commission_rate=args.commission_rate
            )

            print(f"\n--- Backtest Report ---")
            print(f"Initial Portfolio Value: {initial_value:.2f} USDT")
            print(f"Final Portfolio Value:   {final_value:.2f} USDT")
            pnl_percentage = ((final_value - initial_value) / initial_value) * 100 if initial_value > 0 else 0
            print(f"Net Profit/Loss:         {final_value - initial_value:.2f} USDT ({pnl_percentage:.2f}%)")
            print(f"Total Trades Executed:   {len(trades_log)}")

            # Optional: Print detailed trades log
            # print("\n--- Trades Log ---")
            # for trade in trades_log:
            #     print(f"{trade['timestamp']} - {trade['type'].upper()} {trade['quantity']:.4f} at {trade['price']:.2f}, P&L: {trade.get('pnl', 0):.2f}, Comm: {trade['commission']:.2f}")

            # Further analysis could be done here, e.g., saving log to CSV, calculating more metrics.
            print("Backtest finished.")
            logging.info("Backtest finished.")

    else:
        # Live trading mode (original main execution)
        print("Starting live trading bot...")
        logging.info("Starting live trading bot...")
        try:
            bot.run()
        except KeyboardInterrupt:
            bot.notify_telegram("Bot detenido manualmente")
            logging.info("Bot detenido por el usuario")
        except Exception as e:
            logging.error(f"Error en el bot (live mode): {e}", exc_info=True)
            print(f"Error en el bot (live mode): {e}")