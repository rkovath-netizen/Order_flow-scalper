import streamlit as st
import pandas as pd
import requests, urllib.parse, urllib.request, gzip, csv, smtplib
from datetime import datetime, time
import pytz
from github import Github
from streamlit_autorefresh import st_autorefresh
from email.mime.text import MIMEText

st.set_page_config(page_title="Institutional Scanner", layout="wide")
st.title("⚡ Institutional Signal Scanner")

# --- Initialize State ---
if 'trade_history' not in st.session_state:
    st.session_state.trade_history = pd.DataFrame(columns=['Symbol', 'Signal', 'Price', 'Time'])
if 'last_alerts' not in st.session_state:
    st.session_state.last_alerts = {} # To prevent duplicate alerts

# --- Data Helpers ---
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

def fetch_data(key):
    url = f'https://api.upstox.com/v3/historical-candle/intraday/{urllib.parse.quote(key)}/minutes/5'
    res = requests.get(url, headers={'Accept': 'application/json', 'Authorization': f'Bearer {st.secrets["UPSTOX_TOKEN"]}'})
    if res.status_code == 200:
        data = res.json().get('data', {}).get('candles', [])
        df = pd.DataFrame(data, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'oi'])
        df['VWAP'] = ((df['h']+df['l']+df['c'])/3 * df['v']).cumsum() / df['v'].cumsum()
        df['vol_sma'] = df['v'].rolling(10).mean()
        return df.iloc[-2] # Return the last closed candle
    return None

# --- Logic & Alerting ---
def scan_and_alert():
    inst_map = get_instrument_map()
    symbols = ["CRUDEOILM", "NIFTY", "BANKNIFTY", "SENSEX"]
    
    for sym in symbols:
        # Find exact key
        key = next((k for n, k in inst_map.items() if sym in n), None)
        if not key: continue
        
        candle = fetch_data(key)
        if candle is None: continue
        
        # Setup Trigger
        buy = (candle['c'] > candle['VWAP']) & (candle['v'] > candle['vol_sma'] * 2)
        sell = (candle['c'] < candle['VWAP']) & (candle['v'] > candle['vol_sma'] * 2)
        signal = "BUY" if buy else ("SELL" if sell else None)
        
        if signal:
            t_id = f"{sym}_{datetime.now().strftime('%H%M')}"
            if st.session_state.last_alerts.get(sym) != t_id:
                # Update Session State
                new_trade = {"Symbol": sym, "Signal": signal, "Price": candle['c'], "Time": datetime.now().strftime("%H:%M")}
                st.session_state.trade_history = pd.concat([pd.DataFrame([new_trade]), st.session_state.trade_history], ignore_index=True)
                
                # Log to GitHub
                try:
                    g = Github(st.secrets["GITHUB_PAT"])
                    repo = g.get_repo(st.secrets["GITHUB_REPO"])
                    repo.create_file(f"logs/{sym}_{t_id}.csv", "New Trade", st.session_state.trade_history.to_csv(), branch="main")
                except: pass
                
                # Email
                try:
                    msg = MIMEText(f"Signal: {signal} on {sym} at {candle['c']}")
                    msg['Subject'], msg['From'], msg['To'] = f"Alert: {sym} {signal}", st.secrets["GMAIL_USER"], st.secrets["TARGET_EMAIL"]
                    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
                        s.login(st.secrets["GMAIL_USER"], st.secrets["GMAIL_PASS"])
                        s.sendmail(st.secrets["GMAIL_USER"], st.secrets["TARGET_EMAIL"], msg.as_string())
                except: pass
                
                st.session_state.last_alerts[sym] = t_id

# --- Run ---
st_autorefresh(interval=300000)
if st.button("Manual Scan") or True:
    scan_and_alert()

st.subheader("📊 Trade History")
st.dataframe(st.session_state.trade_history, use_container_width=True)
