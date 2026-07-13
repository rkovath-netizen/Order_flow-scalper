import streamlit as st
import pandas as pd
import requests, urllib.parse, urllib.request, gzip, csv, smtplib
from datetime import datetime
from github import Github
from streamlit_autorefresh import st_autorefresh
from email.mime.text import MIMEText

st.set_page_config(page_title="Institutional Scanner", layout="centered")
st.title("⚡ Institutional Signal Scanner")

# --- Initialize State ---
if 'trade_history' not in st.session_state:
    st.session_state.trade_history = pd.DataFrame(columns=['Symbol', 'Signal', 'Price', 'Time'])
if 'last_alerts' not in st.session_state:
    st.session_state.last_alerts = {}

# --- Heartbeat Monitor ---
st.sidebar.metric("Last Scan Time", datetime.now().strftime("%H:%M:%S"))
st_autorefresh(interval=300000) # 5 min refresh

# --- Helpers ---
@st.cache_data(ttl=3600)
def get_instrument_map():
    url = 'https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz'
    mapping = {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})) as res:
            with gzip.GzipFile(fileobj=res) as un:
                reader = csv.DictReader((l.decode('utf-8') for l in un))
                for r in reader:
                    if r['instrument_type'].startswith('FUT'):
                        mapping[r['tradingsymbol']] = r['instrument_key']
        return mapping
    except: return {}

def fetch_and_analyze(key):
    url = f'https://api.upstox.com/v3/historical-candle/intraday/{urllib.parse.quote(key)}/minutes/5'
    res = requests.get(url, headers={'Accept': 'application/json', 'Authorization': f'Bearer {st.secrets["UPSTOX_TOKEN"]}'})
    if res.status_code != 200: return None
    
    data = res.json().get('data', {}).get('candles', [])
    if len(data) < 4: return None
    
    df = pd.DataFrame(data, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'oi'])
    df['VWAP'] = ((df['h']+df['l']+df['c'])/3 * df['v']).cumsum() / df['v'].cumsum()
    df['vol_sma'] = df['v'].rolling(10).mean()
    
    last, prev = df.iloc[-2], df.iloc[-3]
    buy = (last['c'] > last['VWAP']) & (prev['c'] < prev['VWAP']) & (last['v'] > last['vol_sma'] * 2)
    sell = (last['c'] < last['VWAP']) & (prev['c'] > prev['VWAP']) & (last['v'] > last['vol_sma'] * 2)
    return "BUY" if buy else ("SELL" if sell else None), last['c']

# --- Main Scan ---
inst_map = get_instrument_map()
symbols = ["CRUDEOILM", "NIFTY", "BANKNIFTY", "SENSEX"]

for sym in symbols:
    key = next((k for n, k in inst_map.items() if sym in n), None)
    if not key: continue
    
    res = fetch_and_analyze(key)
    if res:
        signal, price = res
        if signal:
            t_id = f"{sym}_{datetime.now().strftime('%H%M')}"
            if st.session_state.last_alerts.get(sym) != t_id:
                # Update table
                new_trade = {"Symbol": sym, "Signal": signal, "Price": price, "Time": datetime.now().strftime("%H:%M")}
                st.session_state.trade_history = pd.concat([pd.DataFrame([new_trade]), st.session_state.trade_history], ignore_index=True)
                # ... [Email & Github Logic stays here] ...
                st.session_state.last_alerts[sym] = t_id

# Display
st.subheader("📊 Trade History")
if not st.session_state.trade_history.empty:
    st.dataframe(st.session_state.trade_history, use_container_width=True)
else:
    st.info("Scanner is running. Waiting for signals... (Market might be closed)")
