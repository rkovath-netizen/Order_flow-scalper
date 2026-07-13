import streamlit as st
import pandas as pd
import requests, urllib.parse, urllib.request, gzip, csv, smtplib
from datetime import datetime
import pytz
from github import Github
from streamlit_autorefresh import st_autorefresh
from email.mime.text import MIMEText

st.set_page_config(page_title="Institutional Scanner", layout="wide")
st.title("⚡ Institutional Signal Scanner")

# --- Timezone Setup ---
IST = pytz.timezone('Asia/Kolkata')

# --- Initialize State ---
if 'trade_history' not in st.session_state:
    st.session_state.trade_history = pd.DataFrame(columns=['Symbol', 'Signal', 'Price', 'Time'])
if 'last_alerts' not in st.session_state:
    st.session_state.last_alerts = {}

# --- Heartbeat Monitor (IST) ---
st.sidebar.metric("Last Scan Time (IST)", datetime.now(IST).strftime("%H:%M:%S"))
st_autorefresh(interval=300000)

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

# --- Scan Loop ---
inst_map = get_instrument_map()
symbols = ["CRUDEOILM", "NIFTY", "BANKNIFTY", "SENSEX"]

for sym in symbols:
    key = next((k for n, k in inst_map.items() if sym in n), None)
    if not key: continue
    
    result = fetch_and_analyze(key)
    if not result: continue 
    
    signal, price = result
    if signal:
        now_ist = datetime.now(IST)
        t_id = f"{sym}_{now_ist.strftime('%d%b_%H%M')}"
        if st.session_state.last_alerts.get(sym) != t_id:
            # Update History
            new_trade = {"Symbol": sym, "Signal": signal, "Price": price, "Time": now_ist.strftime("%H:%M")}
            st.session_state.trade_history = pd.concat([pd.DataFrame([new_trade]), st.session_state.trade_history], ignore_index=True)
            
            # Log/Email (try/except block)
            try:
                g = Github(st.secrets["GITHUB_PAT"])
                repo = g.get_repo(st.secrets["GITHUB_REPO"])
                # Saving as index=False ensures the CSV doesn't have junk numbers
                repo.create_file(f"logs/{t_id}.csv", f"Trade: {sym} {signal}", st.session_state.trade_history.to_csv(index=False), branch="main")
                
                msg = MIMEText(f"Signal: {signal} on {sym} at {price}\nTime: {now_ist.strftime('%H:%M:%S')}")
                msg['Subject'], msg['From'], msg['To'] = f"Alert: {sym} {signal}", st.secrets["GMAIL_USER"], st.secrets["TARGET_EMAIL"]
                with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
                    s.login(st.secrets["GMAIL_USER"], st.secrets["GMAIL_PASS"])
                    s.sendmail(st.secrets["GMAIL_USER"], st.secrets["TARGET_EMAIL"], msg.as_string())
            except Exception as e:
                st.write(f"GitHub/Email Alert Failed: {e}")
            
            st.session_state.last_alerts[sym] = t_id

st.subheader("📊 Trade History")
if not st.session_state.trade_history.empty:
    st.dataframe(st.session_state.trade_history, use_container_width=True)
else:
    st.info("Scanner is running... waiting for volume triggers.")
