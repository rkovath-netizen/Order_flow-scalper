import streamlit as st
import pandas as pd
import requests
from datetime import datetime, time
import pytz
import smtplib
from email.mime.text import MIMEText
from github import Github
from streamlit_autorefresh import st_autorefresh
import urllib.parse
import os
import numpy as np

# --- 1. Page Config & Timezone Setup ---
st.set_page_config(page_title="Order Flow Scalper Dashboard", layout="wide")
st.title("📈 Institutional Setup: Dual-Timeframe Dashboard")

# Set timezone strictly to IST
IST = pytz.timezone('Asia/Kolkata')
now_ist = datetime.now(IST)
current_time = now_ist.time()

# Session State for Trade Management
if 'sent_alerts' not in st.session_state:
    st.session_state.sent_alerts = set()
if 'active_trades' not in st.session_state:
    st.session_state.active_trades = {} # Tracks symbols currently in a trade
if 'trade_history' not in st.session_state:
    st.session_state.trade_history = [] # Logs closed trades for dashboard

# --- 2. Security: Get Token & Secrets ---
try:
    TOKEN = st.secrets["UPSTOX_TOKEN"]
    GMAIL_USER = st.secrets["GMAIL_USER"]
    GMAIL_PASS = st.secrets["GMAIL_PASS"]
    TARGET_EMAIL = st.secrets["TARGET_EMAIL"]
    GITHUB_PAT = st.secrets["GITHUB_PAT"]
    GITHUB_REPO = st.secrets["GITHUB_REPO"]
except Exception as e:
    st.error("Missing Secrets! Please configure them in Streamlit Advanced Settings.")
    st.stop()

# --- 3. Helper Functions ---
@st.cache_data(ttl=3600)
def get_all_instrument_keys(symbols_list):
    if not symbols_list:
        return {}
        
    url = 'https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz'
    local_filename = "/tmp/upstox_complete.csv.gz"
    
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        with open(local_filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                
        columns_to_load = ['instrument_key', 'instrument_type', 'tradingsymbol', 'expiry']
        chunk_iter = pd.read_csv(
            local_filename, 
            compression='gzip', 
            usecols=columns_to_load,
            chunksize=20000,
            low_memory=True
        )
        
        active_contracts = []
        pattern = '^(' + '|'.join(symbols_list) + ')'
        
        for chunk in chunk_iter:
            mask_fut = chunk['instrument_type'].astype(str).str.startswith('FUT')
            chunk_fut = chunk[mask_fut]
            if not chunk_fut.empty:
                mask_sym = chunk_fut['tradingsymbol'].astype(str).str.contains(pattern, regex=True)
                match = chunk_fut[mask_sym]
                if not match.empty:
                    active_contracts.append(match)
        
        if os.path.exists(local_filename):
            os.remove(local_filename)
            
        if not active_contracts:
            return {}
            
        df = pd.concat(active_contracts, ignore_index=True)
        df['expiry'] = pd.to_datetime(df['expiry'], errors='coerce').dt.tz_localize(None)
        df = df.dropna(subset=['expiry'])
        df = df[df['expiry'] >= pd.Timestamp.now().normalize()]
        df = df.sort_values('expiry')
        
        keys_dict = {}
        for sym in symbols_list:
            sym_df = df[df['tradingsymbol'].str.startswith(sym)]
            if not sym_df.empty:
                keys_dict[sym] = sym_df.iloc[0]['instrument_key']
                
        return keys_dict
        
    except Exception as e:
        st.error(f"Error parsing master file: {e}")
        return {}

def fetch_data(instrument_key, interval, token):
    encoded_key = urllib.parse.quote(instrument_key)
    # Using '1' for 1-minute and '5' for 5-minute
    url = f'https://api.upstox.com/v3/historical-candle/intraday/{encoded_key}/minutes/{interval}'
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and 'data' in res.json():
        candles = res.json()['data']['candles']
        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
        df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_convert('Asia/Kolkata')
        df.set_index('timestamp', inplace=True)
        df.sort_index(ascending=True, inplace=True)
        return df.astype(float)
    return None

def calculate_atr(df, period=14):
    df['prev_close'] = df['close'].shift(1)
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['prev_close'])
    df['tr3'] = abs(df['low'] - df['prev_close'])
    df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    df.drop(['prev_close', 'tr1', 'tr2', 'tr3', 'tr'], axis=1, inplace=True)
    return df

def apply_setup_logic(df):
    df = calculate_atr(df)
    
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['cum_vol'] = df['volume'].cumsum()
    df['cum_vol_price'] = (df['typical_price'] * df['volume']).cumsum()
    df['VWAP'] = df['cum_vol_price'] / df['cum_vol']
    df['vol_sma'] = df['volume'].rolling(window=10).mean()
    
    dec_close = df['close'].shift(1)
    dec_prev_close = df['close'].shift(2)
    dec_vwap = df['VWAP'].shift(1)
    dec_prev_vwap = df['VWAP'].shift(2)
    dec_vol = df['volume'].shift(1)
    dec_vol_sma = df['vol_sma'].shift(1)
    
    df['Buy_Trigger'] = (dec_close > dec_vwap) & (dec_prev_close < dec_prev_vwap) & (dec_vol > (dec_vol_sma * 2))
    df['Sell_Trigger'] = (dec_close < dec_vwap) & (dec_prev_close > dec_prev_vwap) & (dec_vol > (dec_vol_sma * 2))
    df['Entry_Price'] = df['open']
    return df

def send_email_alert(symbol, signal_type, price, time_triggered, info=""):
    try:
        if "EXIT" in signal_type:
            subject = f"🛑 {signal_type} ALERT: {symbol} at {price}"
        else:
            subject = f"🚨 {signal_type} ALERT: {symbol} at {price}"
            
        body = f"Event for {symbol}.\nAction: {signal_type}\nPrice: {price}\nTime: {time_triggered}\n{info}"
        msg = MIMEText(body)
        msg['Subject'], msg['From'], msg['To'] = subject, GMAIL_USER, TARGET_EMAIL
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, TARGET_EMAIL, msg.as_string())
        return True
    except Exception as e:
        print(f"Email failed: {e}")
        return False

# --- 4. Sidebar & Dashboard Settings ---
st.sidebar.header("Dashboard Settings")
target_symbols = st.sidebar.multiselect(
    "Select Instruments to Monitor", 
    ["CRUDEOILM", "NIFTY", "BANKNIFTY", "SENSEX"],
    default=["CRUDEOILM", "NIFTY", "BANKNIFTY", "SENSEX"]
)

auto_run = st.sidebar.toggle("Auto-Scanner (Refresh every 1 min)")

# Determine which selected markets are actually open right now
active_symbols = []
for sym in target_symbols:
    if sym == "CRUDEOILM" and time(9, 0) <= current_time <= time(23, 30):
        active_symbols.append(sym)
    elif sym in ["NIFTY", "BANKNIFTY", "SENSEX"] and time(9, 15) <= current_time <= time(15, 30):
        active_symbols.append(sym)

st.sidebar.write(f"**Current IST:** {now_ist.strftime('%I:%M %p')}")
st.sidebar.write(f"**Actively Scanning:** {', '.join(active_symbols) if active_symbols else 'None (Market Closed)'}")

# Refresh Interval set to 1 Minute (60000 ms) for exit tracking
if auto_run:
    if not active_symbols:
        st.sidebar.warning("Selected markets are closed. Scanner idle.")
    else:
        st.sidebar.success("🚀 Auto-scanner running (1m cycle).")
        st_autorefresh(interval=60000, key="dashrefresh") 

# --- 5. Main Execution Block ---
if st.sidebar.button("Run Manual Scan") or (auto_run and active_symbols):
    with st.spinner("Scanning active instruments..."):
        instrument_keys = get_all_instrument_keys(active_symbols)
        
        # Flag to check if we need to push an updated log to GitHub this cycle
        tracker_updated = False 
        
        for symbol in active_symbols:
            key = instrument_keys.get(symbol)
            if not key:
                continue
                
            # ---------------------------------------------------------
            # PHASE 1: EXIT MANAGEMENT (1-Minute Scan)
            # ---------------------------------------------------------
            if symbol in st.session_state.active_trades:
                trade = st.session_state.active_trades[symbol]
                df_1m = fetch_data(key, interval=1, token=TOKEN)
                
                if df_1m is not None and not df_1m.empty:
                    # Look at data from the entry time onwards
                    df_monitor = df_1m[df_1m.index > trade['Entry_Time']]
                    
                    exit_triggered = False
                    exit_price = 0
                    exit_time = None
                    
                    for idx, row in df_monitor.iterrows():
                        if trade['Signal'] == 'BUY':
                            # Update trailing peak
                            trade['Extremum'] = max(trade['Extremum'], row['high'])
                            tsl = trade['Extremum'] - trade['ATR_3X']
                            
                            if row['close'] < tsl:
                                exit_triggered = True
                                exit_price = row['close']
                                exit_time = idx.strftime("%Y-%m-%d %H:%M:%S")
                                break
                                
                        elif trade['Signal'] == 'SELL':
                            # Update trailing trough
                            trade['Extremum'] = min(trade['Extremum'], row['low'])
                            tsl = trade['Extremum'] + trade['ATR_3X']
                            
                            if row['close'] > tsl:
                                exit_triggered = True
                                exit_price = row['close']
                                exit_time = idx.strftime("%Y-%m-%d %H:%M:%S")
                                break
                    
                    if exit_triggered:
                        pnl = (exit_price - trade['Entry_Price']) if trade['Signal'] == 'BUY' else (trade['Entry_Price'] - exit_price)
                        info_text = f"Trailing Stop Hit.\nTSL Value: {tsl:.2f}\nEst. Gross PnL Points: {pnl:.2f}"
                        
                        send_email_alert(symbol, f"EXIT {trade['Signal']}", exit_price, exit_time, info=info_text)
                        
                        st.success(f"🚨 EXIT Alert Sent for {symbol} at {exit_time}")
                        
                        # Log to history
                        st.session_state.trade_history.append({
                            'Status': 'CLOSED',
                            'Symbol': symbol,
                            'Signal': trade['Signal'],
                            'Entry_Time': trade['Entry_Time'].strftime("%Y-%m-%d %H:%M:%S"),
                            'Entry_Price': trade['Entry_Price'],
                            'Exit_Time': exit_time,
                            'Exit_Price': exit_price,
                            'PnL_Points': round(pnl, 2)
                        })
                        
                        # Remove from active trades and flag for GitHub update
                        del st.session_state.active_trades[symbol]
                        tracker_updated = True 
                        continue # Skip entry scanning for this symbol until next cycle

            # ---------------------------------------------------------
            # PHASE 2: ENTRY MANAGEMENT (5-Minute Scan)
            # ---------------------------------------------------------
            if symbol not in st.session_state.active_trades:
                df_5m = fetch_data(key, interval=5, token=TOKEN)
                if df_5m is not None and not df_5m.empty:
                    df_5m = apply_setup_logic(df_5m)
                    
                    # Check the most recently fully formed setups
                    triggers = df_5m[df_5m['Buy_Trigger'] | df_5m['Sell_Trigger']].copy()
                    
                    if not triggers.empty:
                        latest = triggers.iloc[-1]
                        trigger_time = triggers.index[-1]
                        signal = 'BUY' if latest['Buy_Trigger'] else 'SELL'
                        alert_id = f"{symbol}_ENTRY_{trigger_time.strftime('%Y-%m-%d %H:%M:%S')}_{signal}"
                        
                        if alert_id not in st.session_state.sent_alerts:
                            atr_value = latest['atr']
                            atr_3x = atr_value * 3
                            
                            # Add to Active Trades memory
                            st.session_state.active_trades[symbol] = {
                                'Status': 'OPEN',
                                'Signal': signal,
                                'Entry_Time': trigger_time,
                                'Entry_Price': latest['Entry_Price'],
                                'ATR_3X': atr_3x,
                                'Extremum': latest['Entry_Price'] 
                            }
                            
                            st.session_state.sent_alerts.add(alert_id)
                            tracker_updated = True # Flag for GitHub update
                            
                            info_text = f"5m ATR: {atr_value:.2f}\nInitial 3x Trailing SL buffer: {atr_3x:.2f}"
                            if send_email_alert(symbol, f"ENTRY {signal}", latest['Entry_Price'], trigger_time.strftime('%Y-%m-%d %H:%M:%S'), info_text):
                                st.success(f"🚀 ENTRY Alert sent: {signal} on {symbol}")

        # ---------------------------------------------------------
        # CSV LOGGING TO GITHUB (Triggered only on state change)
        # ---------------------------------------------------------
        if tracker_updated:
            # Build a unified tracker DataFrame combining History and Active Trades
            tracker_data = list(st.session_state.trade_history)
            
            for sym, details in st.session_state.active_trades.items():
                tracker_data.append({
                    'Status': details['Status'],
                    'Symbol': sym,
                    'Signal': details['Signal'],
                    'Entry_Time': details['Entry_Time'].strftime("%Y-%m-%d %H:%M:%S"),
                    'Entry_Price': details['Entry_Price'],
                    'Exit_Time': 'ACTIVE',
                    'Exit_Price': 'ACTIVE',
                    'PnL_Points': 'ACTIVE'
                })
                
            if tracker_data:
                tracker_df = pd.DataFrame(tracker_data)
                ts_str = now_ist.strftime("%Y%m%d_%H%M%S")
                filename = f"forward_tracker_{ts_str}.csv"
                
                if push_csv_to_github(tracker_df, filename):
                    st.toast(f"💾 Log backup saved to GitHub: {filename}")

        # ---------------------------------------------------------
        # DISPLAY DASHBOARD PANELS
        # ---------------------------------------------------------
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("🔥 Active Open Trades (1m Trailing)")
            if st.session_state.active_trades:
                active_list = []
                for sym, details in st.session_state.active_trades.items():
                    # Calculate current dynamic TSL for display
                    if details['Signal'] == 'BUY':
                        curr_tsl = details['Extremum'] - details['ATR_3X']
                    else:
                        curr_tsl = details['Extremum'] + details['ATR_3X']
                        
                    active_list.append({
                        "Symbol": sym,
                        "Signal": details['Signal'],
                        "Entry Price": details['Entry_Price'],
                        "Current TSL": round(curr_tsl, 2),
                        "High/Low Reached": details['Extremum'],
                        "Entry Time": details['Entry_Time'].strftime("%H:%M:%S")
                    })
                st.dataframe(pd.DataFrame(active_list), use_container_width=True)
            else:
                st.info("No active trades currently open.")
                
        with col2:
            st.subheader("📋 Closed Trades History (Today)")
            if st.session_state.trade_history:
                hist_df = pd.DataFrame(st.session_state.trade_history)
                # Sort newest exits to the top
                hist_df = hist_df.sort_values(by="Exit_Time", ascending=False).reset_index(drop=True)
                st.dataframe(hist_df, use_container_width=True)
            else:
                st.info("No trades closed yet today.")
