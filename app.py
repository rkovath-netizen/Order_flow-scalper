import streamlit as st
import pandas as pd
import requests
import urllib.parse
from datetime import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import io

# --- Page Config ---
st.set_page_config(page_title="Order Flow Scalper", layout="wide")
st.title("⚡ Institutional Setup: VWAP & Volume Scalper")

# --- 1. Security: Get Token from Streamlit Secrets ---
try:
    TOKEN = st.secrets["UPSTOX_TOKEN"]
except Exception as e:
    st.error("Missing Upstox Token! Please configure it in Streamlit Secrets.")
    st.stop()

# --- 2. Instrument Parser ---
@st.cache_data(ttl=3600) # Cache the master file for 1 hour to speed up reloads
def get_instrument_key(symbol_name):
    url = 'https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz'
    try:
        df_master = pd.read_csv(url)
        search_name = symbol_name.upper().strip()
        futures_df = df_master[df_master['instrument_type'] == 'FUT']
        symbol_df = futures_df[futures_df['tradingsymbol'].str.startswith(search_name)]
        
        if symbol_df.empty: return None
            
        symbol_df = symbol_df.copy()
        symbol_df['expiry'] = pd.to_datetime(symbol_df['expiry'])
        active_contracts = symbol_df[symbol_df['expiry'] >= pd.Timestamp.now().normalize()]
        active_contracts = active_contracts.sort_values('expiry')
        
        if active_contracts.empty: return None
        return active_contracts.iloc[0]['instrument_key']
    except Exception as e:
        return None

# --- 3. Data Fetching ---
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

# --- 4. Setup Logic ---
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

# --- UI & Execution ---
st.sidebar.header("Settings")
symbol_input = st.sidebar.selectbox("Select Instrument", ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOILM"])

if st.sidebar.button("Run Scanner"):
    with st.spinner(f"Fetching data for {symbol_input}..."):
        instrument_key = get_instrument_key(symbol_input)
        
        if not instrument_key:
            st.error(f"Could not find active futures contract for {symbol_input}.")
        else:
            df = fetch_data(instrument_key, interval=5, token=TOKEN)
            
            if df is not None and not df.empty:
                df = apply_setup_logic(df)
                
                # --- Plotting ---
                fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                                    vertical_spacing=0.03, row_width=[0.2, 0.7])

                fig.add_trace(go.Candlestick(x=df.index, open=df['open'], high=df['high'], low=df['low'], close=df['close'], name="Price"), row=1, col=1)
                fig.add_trace(go.Scatter(x=df.index, y=df['VWAP'], line=dict(color='blue', width=2), name='VWAP'), row=1, col=1)

                buy_signals = df[df['Buy_Trigger']]
                fig.add_trace(go.Scatter(x=buy_signals.index, y=buy_signals['Entry_Price'], mode='markers', 
                                         marker=dict(symbol='triangle-up', color='green', size=14, line=dict(width=2, color='white')), name='Buy Exec'), row=1, col=1)

                sell_signals = df[df['Sell_Trigger']]
                fig.add_trace(go.Scatter(x=sell_signals.index, y=sell_signals['Entry_Price'], mode='markers', 
                                         marker=dict(symbol='triangle-down', color='red', size=14, line=dict(width=2, color='white')), name='Sell Exec'), row=1, col=1)

                colors = ['green' if row['close'] >= row['open'] else 'red' for idx, row in df.iterrows()]
                fig.add_trace(go.Bar(x=df.index, y=df['volume'], marker_color=colors, name='Volume'), row=2, col=1)
                fig.add_trace(go.Scatter(x=df.index, y=df['vol_sma'], line=dict(color='orange', width=1), name='Avg Vol'), row=2, col=1)

                fig.update_layout(xaxis_rangeslider_visible=False, height=700, template="plotly_dark", margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig, use_container_width=True)

                # --- Triggers Log & Download ---
                triggers = df[df['Buy_Trigger'] | df['Sell_Trigger']].copy()
                if not triggers.empty:
                    triggers['Signal'] = triggers.apply(lambda row: 'BUY' if row['Buy_Trigger'] else 'SELL', axis=1)
                    export_df = triggers[['open', 'high', 'low', 'close', 'Entry_Price', 'VWAP', 'Signal']]
                    
                    st.subheader("Trade Logs (Today)")
                    st.dataframe(export_df)
                    
                    # Create CSV buffer for download
                    csv = export_df.to_csv().encode('utf-8')
                    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    
                    st.download_button(
                        label="Download Triggers CSV",
                        data=csv,
                        file_name=f"{symbol_input}_triggers_{ts_str}.csv",
                        mime="text/csv",
                    )
                else:
                    st.info("No setup triggers met today.")
            else:
                st.error("Failed to fetch candle data.")
