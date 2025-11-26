import streamlit as st
import requests
import pandas as pd
import matplotlib.pyplot as plt

st.set_page_config(page_title="Option Sellers & Buyers Analyzer", layout="centered")
st.title("📊 Option Chain – Heavy Sellers & Buyers Analyzer")


# ---------------------------------------------------------
# FETCH OPTION CHAIN (INDEX + STOCK)
# ---------------------------------------------------------
@st.cache_data(ttl=60)
def get_option_chain(symbol):
    index_list = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]

    if symbol in index_list:
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    else:
        url = f"https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.nseindia.com/option-chain"
    }

    session = requests.Session()
    session.get("https://www.nseindia.com", headers=headers)

    data = session.get(url, headers=headers).json()
    records = data["records"]["data"]

    final = []
    for item in records:
        strike = item.get("strikePrice")
        ce = item.get("CE")
        pe = item.get("PE")

        ce_oi = ce.get("openInterest", 0) if ce else 0
        pe_oi = pe.get("openInterest", 0) if pe else 0

        ce_vol = ce.get("totalTradedVolume", 0) if ce else 0
        pe_vol = pe.get("totalTradedVolume", 0) if pe else 0

        final.append({
            "Strike": strike,
            "CE_OI": ce_oi,
            "PE_OI": pe_oi,
            "CE_VOL": ce_vol,
            "PE_VOL": pe_vol
        })

    df = pd.DataFrame(final).dropna().sort_values("Strike")
    return df


# ---------------------------------------------------------
# OI + Volume formatting
# ---------------------------------------------------------
def fmt(x):
    return f"{x/100000:.2f} lakh"


# ---------------------------------------------------------
# BUTTON UI (instead of dropdown)
# ---------------------------------------------------------
st.write("### Choose Index or Stock")

col1, col2, col3, col4 = st.columns(4)

symbol = None

with col1:
    if st.button("NIFTY"):
        symbol = "NIFTY"

with col2:
    if st.button("BANKNIFTY"):
        symbol = "BANKNIFTY"

with col3:
    if st.button("FINNIFTY"):
        symbol = "FINNIFTY"

with col4:
    if st.button("MIDCPNIFTY"):
        symbol = "MIDCPNIFTY"

# STOCK button
stock_name = st.text_input("Or enter any stock name (e.g., RELIANCE, TCS, SBIN)")
if st.button("Load Stock"):
    symbol = stock_name.upper()


if symbol is None:
    st.info("👆 Select an index above or enter a stock to load data")
    st.stop()


# ---------------------------------------------------------
# LOAD OPTION CHAIN
# ---------------------------------------------------------
df = get_option_chain(symbol)

total_ce = df["CE_OI"].sum()
total_pe = df["PE_OI"].sum()


# ---------------------------------------------------------
# DETERMINE DOMINANT SELLERS (HIGH OI)
# ---------------------------------------------------------
if total_pe > total_ce:
    dominant_sell = "PE"
    sell_df = df.sort_values("PE_OI", ascending=False).head(5)
else:
    dominant_sell = "CE"
    sell_df = df.sort_values("CE_OI", ascending=False).head(5)

st.subheader(f"🔥 Heavy Sellers ({dominant_sell}) – {symbol}")


# SELLERS TEXT
for _, row in sell_df.iterrows():
    strike = row["Strike"]
    ce = row["CE_OI"]
    pe = row["PE_OI"]

    st.markdown(f"""
### Strike: **{strike}**

{"**PE OI:** " + fmt(pe) if dominant_sell == "PE" else "**CE OI:** " + fmt(ce)}

➡️ **Heavy {'Put' if dominant_sell=='PE' else 'Call'} Writing**
---
""")


# SELLERS BAR CHART
st.write("### 📊 Heavy Sellers Chart")

chart_sell = sell_df.copy()
chart_sell["OI"] = chart_sell["PE_OI"] if dominant_sell == "PE" else chart_sell["CE_OI"]
chart_sell = chart_sell.sort_values("OI", ascending=True)

fig, ax = plt.subplots(figsize=(6, 3))
bars = ax.barh(chart_sell["Strike"].astype(str), chart_sell["OI"], color="skyblue", alpha=0.7)

for bar, oi in zip(bars, chart_sell["OI"]):
    ax.text(bar.get_width() + oi*0.01, bar.get_y() + bar.get_height()/2,
            f"{oi:,}", va='center')

ax.set_title(f"{dominant_sell} heavy sellers")
st.pyplot(fig)


# ---------------------------------------------------------
# BUYERS (HEAVY VOLUME)
# ---------------------------------------------------------
total_ce_vol = df["CE_VOL"].sum()
total_pe_vol = df["PE_VOL"].sum()

if total_pe_vol > total_ce_vol:
    dominant_buy = "PE"
    buy_df = df.sort_values("PE_VOL", ascending=False).head(5)
else:
    dominant_buy = "CE"
    buy_df = df.sort_values("CE_VOL", ascending=False).head(5)

st.subheader(f"🟢 Heavy Buyers ({dominant_buy}) – {symbol}")


# BUYERS TEXT
for _, row in buy_df.iterrows():
    strike = row["Strike"]
    ce_vol = row["CE_VOL"]
    pe_vol = row["PE_VOL"]

    st.markdown(f"""
### Strike: **{strike}**

{"**PE Volume:** " + str(pe_vol) if dominant_buy == "PE" else "**CE Volume:** " + str(ce_vol)}

➡️ **Heavy {'Put' if dominant_buy=='PE' else 'Call'} Buying**
---
""")


# BUYERS BAR CHART
st.write("### 📈 Heavy Buyers Chart")

chart_buy = buy_df.copy()
chart_buy["VOL"] = chart_buy["PE_VOL"] if dominant_buy == "PE" else chart_buy["CE_VOL"]
chart_buy = chart_buy.sort_values("VOL", ascending=True)

fig, ax = plt.subplots(figsize=(6, 3))
bars = ax.barh(chart_buy["Strike"].astype(str), chart_buy["VOL"], color="#99d98c", alpha=0.8)

for bar, vol in zip(bars, chart_buy["VOL"]):
    ax.text(bar.get_width() + vol*0.01,
            bar.get_y() + bar.get_height()/2,
            f"{vol:,}", va='center')

ax.set_title(f"{dominant_buy} heavy buyers")
st.pyplot(fig)


# DONE
# python3.11 -m streamlit run writerstreamBarBUY.py