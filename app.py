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

# --- 1. Config & Setup ---
st.set_page_config(page_title="Order Flow Scalper", layout="wide")
st.title("⚡ Institutional Setup: VWAP & Volume Scalper")
IST = pytz.timezone('Asia/Kolkata')

# --- 2. Security ---
try:
    TOKEN = st.secrets["UPSTOX_TOKEN"]
    GMAIL_USER = st.secrets["GMAIL_USER"]
    GMAIL_PASS = st.secrets["GMAIL_PASS"]
    TARGET_EMAIL = st.secrets["TARGET_EMAIL"]
    GITHUB_PAT = st.secrets["GITHUB_PAT"]
    GITHUB_REPO = st.secrets["GITHUB_REPO"]
except:
    st.error("Secrets missing!")
    st.stop()

# --- 3. Unified Instrument Mapper ---
@st.cache_data(ttl=3600)
def get_instrument_key(symbol_name):
    url = 'https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz'
    search_name = symbol_name.upper().strip()
    
    # We use a broader search to ensure we catch the contract
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            with gzip.GzipFile(fileobj=response) as uncompressed:
                reader = csv.DictReader((line.decode('utf-8') for line in uncompressed))
                matches = []
                for row in reader:
                    # Look for Futures AND ensure the symbol is contained within the ticker
                    if row.get('instrument_type', '').startswith('FUT'):
                        if search_name in row.get('tradingsymbol', ''):
                            matches.append(row)
        
        if not matches: return None
        
        df = pd.DataFrame(matches)
        df['expiry'] = pd.to_datetime(df['expiry'])
        # Sort by expiry to get the nearest contract
        df = df[df['expiry'] >= pd.Timestamp.now().normalize()].sort_values('expiry')
        return df.iloc[0]['instrument_key']
    except:
        return None

# --- 4. Logic & Fetching ---
def fetch_data(instrument_key, interval, token):
    encoded_key = urllib.parse.quote(instrument_key)
    url = f'https://api.upstox.com/v3/historical-candle/intraday/{encoded_key}/minutes/{interval}'
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        data = res.json().get('data', {}).get('candles', [])
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
        df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_convert('Asia/Kolkata')
        df.set_index('timestamp', inplace=True)
        return df.sort_index().astype(float)
    return None

def apply_setup_logic(df):
    # VWAP Calc
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['VWAP'] = (df['typical_price'] * df['volume']).cumsum() / df['volume'].cumsum()
    df['vol_sma'] = df['volume'].rolling(10).mean()
    
    # Shifts to use CLOSED candles only
    df['Buy_Trigger'] = (df['close'].shift(1) > df['VWAP'].shift(1)) & \
                        (df['close'].shift(2) < df['VWAP'].shift(2)) & \
                        (df['volume'].shift(1) > df['vol_sma'].shift(1) * 2)
    
    df['Sell_Trigger'] = (df['close'].shift(1) < df['VWAP'].shift(1)) & \
                         (df['close'].shift(2) > df['VWAP'].shift(2)) & \
                         (df['volume'].shift(1) > df['vol_sma'].shift(1) * 2)
    df['Entry_Price'] = df['open']
    return df

# --- 5. UI ---
symbol_input = st.sidebar.selectbox("Instrument", ["CRUDEOILM", "NIFTY", "BANKNIFTY", "SENSEX"])
auto_run = st.sidebar.toggle("Auto-Scanner")

if auto_run: st_autorefresh(interval=300000)

if st.sidebar.button("Run Scan") or auto_run:
    with st.spinner("Processing..."):
        key = get_instrument_key(symbol_input)
        if key:
            df = fetch_data(key, 5, TOKEN)
            if df is not None:
                df = apply_setup_logic(df)
                
                # Plot
                fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_width=[0.2, 0.7])
                fig.add_trace(go.Candlestick(x=df.index, open=df['open'], high=df['high'], low=df['low'], close=df['close']), row=1, col=1)
                fig.add_trace(go.Scatter(x=df.index, y=df['VWAP'], line=dict(color='cyan')), row=1, col=1)
                
                # Markers
                buys = df[df['Buy_Trigger']]
                sells = df[df['Sell_Trigger']]
                fig.add_trace(go.Scatter(x=buys.index, y=buys['Entry_Price'], mode='markers', marker=dict(color='lime', size=15, symbol='triangle-up')), row=1, col=1)
                fig.add_trace(go.Scatter(x=sells.index, y=sells['Entry_Price'], mode='markers', marker=dict(color='red', size=15, symbol='triangle-down')), row=1, col=1)
                
                st.plotly_chart(fig, use_container_width=True)
                
                # Log table
                signals = df[df['Buy_Trigger'] | df['Sell_Trigger']]
                if not signals.empty:
                    st.dataframe(signals[['Entry_Price', 'VWAP']])
                    # Logic for Email/GitHub here (omitted for brevity, matches previous block)
        else:
            st.error("Instrument not found. Try a different symbol name.")
