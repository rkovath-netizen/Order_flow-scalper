import streamlit as st
import pandas as pd
import requests
import urllib.parse, urllib.request, gzip, csv
from datetime import datetime, time
import pytz
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from streamlit_autorefresh import st_autorefresh

# --- Config ---
st.set_page_config(page_title="Multi-Scalper", layout="wide")
st.title("⚡ Institutional Setup: Multi-Instrument Scanner")
IST = pytz.timezone('Asia/Kolkata')
TOKEN = st.secrets["UPSTOX_TOKEN"]

# --- Helper Functions ---
@st.cache_data(ttl=3600)
def get_instrument_key(symbol_name):
    url = 'https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            with gzip.GzipFile(fileobj=response) as uncompressed:
                reader = csv.DictReader((line.decode('utf-8') for line in uncompressed))
                matches = [r for r in reader if r.get('instrument_type', '').startswith('FUT') and symbol_name in r.get('tradingsymbol', '')]
        
        df = pd.DataFrame(matches)
        df['expiry'] = pd.to_datetime(df['expiry'])
        return df[df['expiry'] >= pd.Timestamp.now().normalize()].sort_values('expiry').iloc[0]['instrument_key']
    except: return None

def fetch_data(key):
    url = f'https://api.upstox.com/v3/historical-candle/intraday/{urllib.parse.quote(key)}/minutes/5'
    res = requests.get(url, headers={'Accept': 'application/json', 'Authorization': f'Bearer {TOKEN}'})
    if res.status_code == 200:
        data = res.json().get('data', {}).get('candles', [])
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
        # FIX: Localize to UTC first, then convert to IST
        df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
        df.set_index('timestamp', inplace=True)
        return df.sort_index().astype(float)
    return None

def apply_setup_logic(df):
    df['VWAP'] = ((df['high'] + df['low'] + df['close']) / 3 * df['volume']).cumsum() / df['volume'].cumsum()
    df['vol_sma'] = df['volume'].rolling(10).mean()
    df['Buy_Trigger'] = (df['close'].shift(1) > df['VWAP'].shift(1)) & (df['close'].shift(2) < df['VWAP'].shift(2)) & (df['volume'].shift(1) > df['vol_sma'].shift(1) * 2)
    df['Sell_Trigger'] = (df['close'].shift(1) < df['VWAP'].shift(1)) & (df['close'].shift(2) > df['VWAP'].shift(2)) & (df['volume'].shift(1) > df['vol_sma'].shift(1) * 2)
    df['Entry_Price'] = df['open']
    return df

# --- Multi-Tab Scanner ---
auto_run = st.sidebar.toggle("Auto-Refresh (5m)")
if auto_run: st_autorefresh(interval=300000)

tabs = st.tabs(["CRUDEOILM", "NIFTY", "BANKNIFTY", "SENSEX"])
instruments = ["CRUDEOILM", "NIFTY", "BANKNIFTY", "SENSEX"]

for i, tab in enumerate(tabs):
    with tab:
        sym = instruments[i]
        with st.spinner(f"Scanning {sym}..."):
            key = get_instrument_key(sym)
            if key:
                df = fetch_data(key)
                if df is not None:
                    df = apply_setup_logic(df)
                    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_width=[0.2, 0.7])
                    fig.add_trace(go.Candlestick(x=df.index, open=df['open'], high=df['high'], low=df['low'], close=df['close']), row=1, col=1)
                    fig.add_trace(go.Scatter(x=df.index, y=df['VWAP'], line=dict(color='cyan')), row=1, col=1)
                    # Add signals
                    buys, sells = df[df['Buy_Trigger']], df[df['Sell_Trigger']]
                    fig.add_trace(go.Scatter(x=buys.index, y=buys['Entry_Price'], mode='markers', marker=dict(color='lime', size=15, symbol='triangle-up')), row=1, col=1)
                    fig.add_trace(go.Scatter(x=buys.index, y=sells['Entry_Price'], mode='markers', marker=dict(color='red', size=15, symbol='triangle-down')), row=1, col=1)
                    fig.update_layout(height=600, template="plotly_dark")
                    st.plotly_chart(fig, use_container_width=True)
            else: st.error("Instrument not found.")
