import requests
import time
import io
from datetime import datetime
import pytz
from PIL import Image, ImageDraw, ImageFont

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DHAN_ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJwX2lwIjoiIiwic19pcCI6IiIsImlzcyI6ImRoYW4iLCJwYXJ0bmVySWQiOiIiLCJleHAiOjE3Nzc0MDI2NzMsImlhdCI6MTc3NzMxNjI3MywidG9rZW5Db25zdW1lclR5cGUiOiJTRUxGIiwid2ViaG9va1VybCI6Imh0dHBzOi8vd2ViLmRoYW4uY28vaW5kZXgvcHJvZmlsZSIsImRoYW5DbGllbnRJZCI6IjExMDgwNjYwOTQifQ.n77Ab0M-xWH7-CMYvOJakUra7Mp2ZAnvpjA1yRJZ4_tR6ky4NXfeDh_452JHzMHDLi2l4vZzghjPOXP1Fv0ehw"
DHAN_CLIENT_ID    = "1108066094"
# Shivu account
TELEGRAM_TOKEN_1   = "8584181321:AAHBBTFlhGPs-mBgbRHXLkJME9FaqJh5ofE"
TELEGRAM_CHAT_ID_1 = "653488319"

# Manju account
TELEGRAM_TOKEN_2   = "8243416633:AAFjISDBXvhqGsM8xvOkWOeQ4eEmhMPlkNU"
TELEGRAM_CHAT_ID_2 = "567677761"

TELEGRAM_ACCOUNTS = [
    {"token": TELEGRAM_TOKEN_1, "chat_id": TELEGRAM_CHAT_ID_1, "name": "Shivu"},
    {"token": TELEGRAM_TOKEN_2, "chat_id": TELEGRAM_CHAT_ID_2, "name": "Manju"},
]

API_BASE        = "https://api.dhan.co/v2"
OPTIONCHAIN_URL = f"{API_BASE}/optionchain"
EXPIRY_LIST_URL = f"{API_BASE}/optionchain/expirylist"

INDICES = {
    "NIFTY":  {"Scrip": 13, "Segments": ["IDX_I", "NSE_FNO"], "step": 50},
    "SENSEX": {"Scrip": 1,  "Segments": ["BSE_FNO", "IDX_I"], "step": 100},
}

ATM_RANGE = 5  # ATM ± 5 strikes

# ──────────────────────────────────────────────
# COLOURS  (matching your dashboard)
# ──────────────────────────────────────────────
BG          = (10,  12,  18)    # #0a0c12
HEADER_BG   = (28,  34,  48)    # #1c2230
ROW_BG      = (10,  12,  18)
ROW_ALT     = (16,  20,  30)
ATM_BG      = (255, 255, 255)
ATM_FG      = (0,   0,   0)
STRIKE_BG   = (20,  24,  36)
CYAN_BG     = (0,   188, 212)   # max CE highlight
PINK_BG     = (233, 30,  99)    # max PE highlight
TEXT_WHITE  = (255, 255, 255)
TEXT_GREY   = (148, 163, 184)
TEXT_BLACK  = (0,   0,   0)

# ──────────────────────────────────────────────
# FONT  — use default PIL bitmap font (no file needed)
# ──────────────────────────────────────────────
def get_font(size=15, bold=False):
    try:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold \
               else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def _headers():
    return {
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id": DHAN_CLIENT_ID,
        "Content-Type": "application/json"
    }

def fmt(val):
    return f"{val/1e5:.2f}L"

def send_telegram_text(msg):
    for acc in TELEGRAM_ACCOUNTS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{acc['token']}/sendMessage",
                json={"chat_id": acc["chat_id"], "text": msg, "parse_mode": "Markdown"},
                timeout=10
            )
            print(f"  ✅ Text sent to {acc['name']}")
        except Exception as e:
            print(f"  ❌ Failed to send text to {acc['name']}: {e}")

def send_telegram_image(img_bytes, caption=""):
    for acc in TELEGRAM_ACCOUNTS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{acc['token']}/sendPhoto",
                data={"chat_id": acc["chat_id"], "caption": caption, "parse_mode": "Markdown"},
                files={"photo": ("table.png", img_bytes, "image/png")},
                timeout=15
            )
            print(f"  ✅ Image sent to {acc['name']}")
        except Exception as e:
            print(f"  ❌ Failed to send image to {acc['name']}: {e}")

# ──────────────────────────────────────────────
# IMAGE TABLE GENERATOR
# ──────────────────────────────────────────────
def build_table_image(index_name, ltp, atm, expiry, pcr, rows,
                      max_c_delta, max_p_delta, max_c_vol, max_p_vol):

    COLS   = ["C-Vol", "C-ΔOI", "STRIKE", "P-ΔOI", "P-Vol"]
    WIDTHS = [120, 120, 100, 120, 120]   # column widths
    ROW_H  = 34
    HDR_H  = 40
    TOP_H  = 60    # top info bar height
    PAD    = 10

    total_w = sum(WIDTHS) + PAD * 2
    n_rows  = len(rows)
    total_h = TOP_H + HDR_H + n_rows * ROW_H + PAD

    img  = Image.new("RGB", (total_w, total_h), BG)
    draw = ImageDraw.Draw(img)

    fn       = get_font(14)
    fn_bold  = get_font(14, bold=True)
    fn_small = get_font(12)
    fn_top   = get_font(15, bold=True)

    # ── Top info bar ──
    draw.rectangle([0, 0, total_w, TOP_H], fill=HEADER_BG)
    draw.text((PAD, 8),
              f"{index_name}  |  LTP: {ltp:,.0f}  |  ATM: {int(atm)}  |  PCR: {pcr:.2f}  |  Expiry: {expiry}",
              font=fn_top, fill=TEXT_WHITE)
    draw.text((PAD, 30),
              f"Generated: {datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%d-%b-%Y  %H:%M IST')}",
              font=fn_small, fill=TEXT_GREY)

    # ── Column headers ──
    x = PAD
    y = TOP_H
    for i, (col, w) in enumerate(zip(COLS, WIDTHS)):
        draw.rectangle([x, y, x + w, y + HDR_H], fill=HEADER_BG)
        # center text
        bbox = draw.textbbox((0, 0), col, font=fn_bold)
        tw   = bbox[2] - bbox[0]
        draw.text((x + (w - tw) // 2, y + 10), col, font=fn_bold, fill=TEXT_GREY)
        x += w

    # ── Data rows ──
    for ri, row in enumerate(rows):
        y    = TOP_H + HDR_H + ri * ROW_H
        x    = PAD
        is_atm = row["strike"] == atm
        row_bg = ATM_BG if is_atm else (ROW_ALT if ri % 2 else ROW_BG)

        # Values
        c_vol_s  = fmt(row["c_vol"])
        c_d_s    = fmt(row["c_delta"]) + ("▲" if row["c_delta"] >= 0 else "▼")
        strike_s = str(int(row["strike"]))
        p_d_s    = fmt(row["p_delta"]) + ("▲" if row["p_delta"] >= 0 else "▼")
        p_vol_s  = fmt(row["p_vol"])

        values   = [c_vol_s, c_d_s, strike_s, p_d_s, p_vol_s]

        for ci, (val, w) in enumerate(zip(values, WIDTHS)):
            # Determine cell background
            if is_atm and ci == 2:
                cell_bg  = ATM_BG
                cell_fg  = ATM_FG
                cell_font = fn_bold
            elif ci == 2:
                cell_bg  = STRIKE_BG
                cell_fg  = TEXT_WHITE
                cell_font = fn_bold
            elif ci == 0 and row["c_vol"] == max_c_vol:
                cell_bg  = CYAN_BG
                cell_fg  = TEXT_BLACK
                cell_font = fn_bold
            elif ci == 1 and row["c_delta"] == max_c_delta:
                cell_bg  = CYAN_BG
                cell_fg  = TEXT_BLACK
                cell_font = fn_bold
            elif ci == 3 and row["p_delta"] == max_p_delta:
                cell_bg  = PINK_BG
                cell_fg  = TEXT_BLACK
                cell_font = fn_bold
            elif ci == 4 and row["p_vol"] == max_p_vol:
                cell_bg  = PINK_BG
                cell_fg  = TEXT_BLACK
                cell_font = fn_bold
            else:
                cell_bg  = row_bg
                cell_fg  = ATM_FG if is_atm else TEXT_WHITE
                cell_font = fn

            draw.rectangle([x, y, x + w, y + ROW_H], fill=cell_bg)

            # Center text in cell
            bbox = draw.textbbox((0, 0), val, font=cell_font)
            tw   = bbox[2] - bbox[0]
            th   = bbox[3] - bbox[1]
            draw.text((x + (w - tw) // 2, y + (ROW_H - th) // 2),
                      val, font=cell_font, fill=cell_fg)
            x += w

        # Thin row separator
        draw.line([(PAD, y + ROW_H), (total_w - PAD, y + ROW_H)],
                  fill=(30, 36, 54), width=1)

    # Save to bytes
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ... (ALL YOUR IMPORTS & CONFIG — NO CHANGE)

# ──────────────────────────────────────────────
# FETCH & ALERT
# ──────────────────────────────────────────────
def fetch_and_alert(index_name, cfg):
    # Step 1: Get nearest expiry
    found_expiry, used_seg = None, None
    for seg in cfg["Segments"]:
        r = requests.post(EXPIRY_LIST_URL,
                          json={"UnderlyingScrip": cfg["Scrip"], "UnderlyingSeg": seg},
                          headers=_headers(), timeout=10)
        exp_list = r.json().get("data", [])
        if exp_list:
            found_expiry, used_seg = exp_list[0], seg
            break

    if not found_expiry:
        print(f"[{index_name}] No expiry found.")
        return

    # Step 2: Fetch option chain
    r_oc = requests.post(OPTIONCHAIN_URL,
                         json={"UnderlyingScrip": cfg["Scrip"], "UnderlyingSeg": used_seg, "Expiry": found_expiry},
                         headers=_headers(), timeout=10)
    data_sec = r_oc.json().get("data", {})
    oc_map   = data_sec.get("oc", {})
    ltp      = float(data_sec.get("last_price") or 0)

    if not oc_map or ltp == 0:
        print(f"[{index_name}] No OC data.")
        return

    # Step 3: Build ATM ± 5 rows
    atm  = round(ltp / cfg["step"]) * cfg["step"]
    rows = []
    for strike_s, legs in oc_map.items():
        strike_f = float(strike_s)
        if abs(strike_f - atm) <= (cfg["step"] * ATM_RANGE):
            ce = legs.get("ce", {})
            pe = legs.get("pe", {})
            c_delta = int(ce.get("oi", 0)) - int(ce.get("previous_oi") or 0)
            p_delta = int(pe.get("oi", 0)) - int(pe.get("previous_oi") or 0)
            rows.append({
                "strike":  strike_f,
                "c_ltp":   float(ce.get("last_price", 0)),
                "p_ltp":   float(pe.get("last_price", 0)),
                "c_oi":    int(ce.get("oi", 0)),
                "p_oi":    int(pe.get("oi", 0)),
                "c_delta": c_delta,
                "p_delta": p_delta,
                "c_vol":   int(ce.get("volume", 0)),
                "p_vol":   int(pe.get("volume", 0)),
            })

    if not rows:
        return

    rows = sorted(rows, key=lambda x: x["strike"], reverse=True)

    # Step 4: Identify max values for highlighting (no change)
    max_c_delta = max(r["c_delta"] for r in rows)
    max_p_delta = max(r["p_delta"] for r in rows)
    max_c_vol   = max(r["c_vol"]   for r in rows)
    max_p_vol   = max(r["p_vol"]   for r in rows)

    total_c_oi = sum(r["c_oi"] for r in rows)
    total_p_oi = sum(r["p_oi"] for r in rows)
    pcr        = total_p_oi / total_c_oi if total_c_oi else 0

    # Step 5: Generate image (NO CHANGE)
    img_bytes = build_table_image(
        index_name, ltp, atm, found_expiry, pcr, rows,
        max_c_delta, max_p_delta, max_c_vol, max_p_vol
    )

    # ──────────────────────────────────────────────
    # ✅ NEW: SMART TELEGRAM TEXT (OI SENTIMENT)
    # ──────────────────────────────────────────────
    msg_lines = []
    msg_lines.append(f"📊 *{index_name}*  |  LTP: `{ltp:,.0f}`\n")

    for r in rows:
        strike = int(r["strike"])
        c_delta = r["c_delta"]
        p_delta = r["p_delta"]

        # Sentiment logic
        if c_delta > p_delta:
            icon = "🔴"   # Bearish
        elif p_delta > c_delta:
            icon = "🟢"   # Bullish
        else:
            icon = "⚪"

        # Strike formatting
        if strike == atm:
            strike_txt = f"{icon} {strike} ATM"
        else:
            diff = int((strike - atm) / cfg["step"])
            sign = f"+{diff}" if diff > 0 else f"{diff}"
            strike_txt = f"{icon} {strike} {sign}"

        left  = f"{r['c_vol']:,}"
        right = f"{r['p_vol']:,}"

        line = f"`{left:<10}`  {strike_txt:^14}  `{right:>10}`"
        msg_lines.append(line)

    msg_lines.append(f"\nPCR: `{pcr:.2f}`")

    final_msg = "\n".join(msg_lines)

    # ✅ Send BOTH image + new text
    send_telegram_image(img_bytes, caption="")   # keep image clean
    send_telegram_text(final_msg)

    print(f"[{index_name}] Image + Smart text alert sent ✅")

# ──────────────────────────────────────────────
# MARKET HOURS CHECK
# ──────────────────────────────────────────────
def is_market_open():
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    if now.weekday() >= 5:
        return False, f"Weekend ({now.strftime('%A')})"
    open_time  = now.replace(hour=9,  minute=0, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if open_time <= now <= close_time:
        return True, now.strftime('%H:%M IST')
    return False, f"Market closed — {now.strftime('%H:%M IST')}"

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
if __name__ == "__main__":
    open_status, reason = is_market_open()
    if not open_status:
        print(f"⏸ Skipping — {reason}")
    else:
        print(f"✅ Market is open — {reason}")
        for name, cfg in INDICES.items():
            try:
                fetch_and_alert(name, cfg)
            except Exception as e:
                print(f"[{name}] Error: {e}")
