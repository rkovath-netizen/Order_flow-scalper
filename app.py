import streamlit as st
import pandas as pd
import requests
import urllib.parse
import urllib.request
from datetime import datetime, time
import pytz
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import smtplib
from email.mime.text import MIMEText
from github import Github
from streamlit_autorefresh import st_autorefresh
import gzip
import csv

# --- 1. Page Config & Timezone Setup ---
st.set_page_config(page_title="Order Flow Scalper", layout="wide")
st.title("⚡ Institutional Setup: VWAP & Volume Scalper")

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
    Reads the massive Upstox master file line-by-line to prevent Streamlit RAM crashes.
    """
    url = 'https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz'
    search_name = symbol_name.upper().strip()
    active_contracts = []
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            with gzip.GzipFile(fileobj=response) as uncompressed:
                # Stream the file line-by-line (Zero memory footprint)
                lines = (line.decode('utf-8') for line in uncompressed)
                reader = csv.DictReader(lines)
                
                for row in reader:
                    # Only keep FUTURES matching our symbol name
                    if row.get('instrument_type') == 'FUT' and row.get('tradingsymbol', '').startswith(search_name):
                        active_contracts.append(row)
        
        if not active_contracts:
            return None
            
        # Convert only our tiny handful of matches to Pandas for date sorting
        df = pd.DataFrame(active_contracts)
        df['expiry'] = pd.to_datetime(df['expiry'])
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
        st.sidebar.success("🔄 Auto-scanner active.")
        st_autorefresh(interval=300000, key="dataframerefresh") 

# --- 5. Main Execution Block ---
if st.sidebar.button("Run Manual Scan") or (auto_run and is_market_open):
    with st.spinner(f"Scanning {symbol_input} setup..."):
        instrument_key = get_instrument_key(symbol_input)
        if instrument_key:
            df = fetch_data(instrument_key, interval=5, token=TOKEN)
            if df is not None and not df.empty:
                df = apply_setup_logic(df)
                
                # Charting
                fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2, 0.7])
                fig.add_trace(go.Candlestick(x=df.index, open=df['open'], high=df['high'], low=df['low'], close=df['close'], name="Price"), row=1, col=1)
                fig.add_trace(go.Scatter(x=df.index, y=df['VWAP'], line=dict(color='blue', width=2), name='VWAP'), row=1, col=1)
                
                buy_signals = df[df['Buy_Trigger']]
                fig.add_trace(go.Scatter(x=buy_signals.index, y=buy_signals['Entry_Price'], mode='markers', marker=dict(symbol='triangle-up', color='green', size=14, line=dict(width=2, color='white')), name='Buy Exec'), row=1, col=1)
                
                sell_signals = df[df['Sell_Trigger']]
                fig.add_trace(go.Scatter(x=sell_signals.index, y=sell_signals['Entry_Price'], mode='markers', marker=dict(symbol='triangle-down', color='red', size=14, line=dict(width=2, color='white')), name='Sell Exec'), row=1, col=1)
                
                colors = ['green' if row['close'] >= row['open'] else 'red' for idx, row in df.iterrows()]
                fig.add_trace(go.Bar(x=df.index, y=df['volume'], marker_color=colors, name='Volume'), row=2, col=1)
                fig.add_trace(go.Scatter(x=df.index, y=df['vol_sma'], line=dict(color='orange', width=1), name='Avg Vol'), row=2, col=1)
                
                fig.update_layout(xaxis_rangeslider_visible=False, height=700, template="plotly_dark", margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig, use_container_width=True)

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
            st.error("Failed to map instrument key from master list.")
