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

# --- 1. Page Config & Timezone Setup ---
st.set_page_config(page_title="Order Flow Scalper", layout="wide")
st.title("📈 Institutional Setup: VWAP & Volume Scalper")

# Set timezone strictly to IST
IST = pytz.timezone('Asia/Kolkata')
now_ist = datetime.now(IST)
current_time = now_ist.time()

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
def get_instrument_key(symbol_name):
    """
    Downloads the master file to disk to prevent zlib stream segmentation faults,
    then parses it in small chunks using Pandas to prevent Out-Of-Memory crashes.
    """
    url = 'https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz'
    search_name = symbol_name.upper().strip()
    local_filename = "/tmp/upstox_complete.csv.gz"
    
    try:
        # 1. Safely download the file to disk first
        response = requests.get(url, stream=True)
        response.raise_for_status()
        with open(local_filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                
        # 2. Process using Pandas chunks to keep memory ultra-low
        columns_to_load = ['instrument_key', 'instrument_type', 'tradingsymbol', 'expiry']
        chunk_iter = pd.read_csv(
            local_filename, 
            compression='gzip', 
            usecols=columns_to_load,
            chunksize=20000,
            low_memory=True
        )
        
        active_contracts = []
        for chunk in chunk_iter:
            # Filter chunk for FUTURES and matching tradingsymbol
            mask = chunk['instrument_type'].astype(str).str.startswith('FUT') & chunk['tradingsymbol'].astype(str).str.startswith(search_name)
            match = chunk[mask]
            if not match.empty:
                active_contracts.append(match)
        
        # Clean up the temp file
        if os.path.exists(local_filename):
            os.remove(local_filename)
            
        if not active_contracts:
            return None
            
        # 3. Concatenate and find the nearest expiry
        df = pd.concat(active_contracts, ignore_index=True)
        
        # Safely convert expiry, removing timezone info to compare with local current time
        df['expiry'] = pd.to_datetime(df['expiry'], errors='coerce').dt.tz_localize(None)
        df = df.dropna(subset=['expiry'])
        df = df[df['expiry'] >= pd.Timestamp.now().normalize()]
        df = df.sort_values('expiry')
        
        if df.empty:
            return None
        return df.iloc[0]['instrument_key']
        
    except Exception as e:
        st.error(f"Error parsing master file: {e}")
        return None

def fetch_data(instrument_key, interval, token):
    encoded_key = urllib.parse.quote(instrument_key)
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

def apply_setup_logic(df):
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

def send_email_alert(symbol, signal_type, entry_price, time_triggered):
    try:
        subject = f"🚨 {signal_type} ALERT: {symbol} at {entry_price}"
        body = f"Setup triggered for {symbol}.\nSignal: {signal_type}\nEntry Price: {entry_price}\nTime: {time_triggered}"
        msg = MIMEText(body)
        msg['Subject'], msg['From'], msg['To'] = subject, GMAIL_USER, TARGET_EMAIL
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, TARGET_EMAIL, msg.as_string())
        return True
    except Exception as e:
        print(f"Email failed: {e}")
        return False

def push_csv_to_github(df, filename):
    try:
        g = Github(GITHUB_PAT)
        repo = g.get_repo(GITHUB_REPO)
        csv_content = df.to_csv(index=True)
        repo.create_file(f"logs/{filename}", f"Auto-log: {filename}", csv_content, branch="main")
        return True
    except Exception as e:
        print(f"GitHub push failed: {e}")
        return False

# --- 4. Sidebar & Market Hours Logic ---
st.sidebar.header("Settings")
symbol_input = st.sidebar.selectbox("Select Instrument", ["CRUDEOILM", "NIFTY", "BANKNIFTY", "SENSEX"])
auto_run = st.sidebar.toggle("Auto-Scanner (Refresh every 5 mins)")

# Determine if market is open based on user rules
is_market_open = False
if symbol_input == "CRUDEOILM":
    if time(9, 0) <= current_time <= time(23, 30):
        is_market_open = True
elif symbol_input in ["NIFTY", "BANKNIFTY", "SENSEX"]:
    if time(9, 30) <= current_time <= time(15, 30):
        is_market_open = True

st.sidebar.write(f"**Current IST:** {now_ist.strftime('%I:%M %p')}")
st.sidebar.write(f"**Market Status:** {'🟢 OPEN' if is_market_open else '🔴 CLOSED'}")

# Non-Blocking Background Auto-Refresh
if auto_run:
    if not is_market_open:
        st.sidebar.warning(f"Market closed for {symbol_input}. Scanner paused.")
    else:
        st.sidebar.success("🚀 Auto-scanner active.")
        st_autorefresh(interval=300000, key="dataframerefresh") 

# --- 5. Main Execution Block ---
if st.sidebar.button("Run Manual Scan") or (auto_run and is_market_open):
    with st.spinner(f"Scanning {symbol_input} setup..."):
        instrument_key = get_instrument_key(symbol_input)
        if instrument_key:
            df = fetch_data(instrument_key, interval=5, token=TOKEN)
            if df is not None and not df.empty:
                df = apply_setup_logic(df)
                
                # Triggers & Alerts
                triggers = df[df['Buy_Trigger'] | df['Sell_Trigger']].copy()
                if not triggers.empty:
                    triggers['Signal'] = triggers.apply(lambda row: 'BUY' if row['Buy_Trigger'] else 'SELL', axis=1)
                    export_df = triggers[['open', 'high', 'low', 'close', 'Entry_Price', 'VWAP', 'Signal']]
                    st.subheader("Trade Logs (Today)")
                    st.dataframe(export_df)
                    
                    latest = export_df.iloc[-1]
                    trigger_time = export_df.index[-1].strftime("%Y-%m-%d %H:%M:%S")
                    last_alert = f"{symbol_input}_{trigger_time}"
                    
                    # Process Actions safely without double-triggering
                    if 'last_alert_sent' not in st.session_state or st.session_state.last_alert_sent != last_alert:
                        if send_email_alert(symbol_input, latest['Signal'], latest['Entry_Price'], trigger_time):
                            st.success(f"Email alert sent for {latest['Signal']} at {trigger_time}!")
                        
                        ts_str = now_ist.strftime("%Y%m%d_%H%M%S")
                        filename = f"{symbol_input}_triggers_{ts_str}.csv"
                        if push_csv_to_github(export_df, filename):
                            st.success(f"Log backup uploaded to GitHub: {filename}")
                            
                        st.session_state.last_alert_sent = last_alert
                else:
                    st.info(f"No execution triggers hit for {symbol_input} today yet.")
        else:
            st.error("Failed to map instrument key from master list. Check if the symbol exists as a Futures contract.")
