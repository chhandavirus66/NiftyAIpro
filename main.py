import asyncio
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote_plus

import flet as ft
import flet_charts as fch
import numpy as np
import pandas as pd
import requests
import yfinance as yf


# ============================================================
# NIFTY PRO QUANT TERMINAL - FLET 1.0
# PC-engine integration for Android
#
# Data layers:
#   1) Yahoo Finance: NIFTY/VIX/OHLCV/global/sector snapshots
#   2) Google News RSS: market news + important-person headlines
#   3) NSE public endpoint: FII/DII institutional-flow snapshot
#   4) Optional DhanHQ API: live NIFTY option-chain OI/PCR/IV
#
# IMPORTANT:
# - News sentiment is headline-based, not a claim of causality.
# - FII/DII is a public institutional-flow snapshot; "big money bias"
#   is an analytical proxy, not proprietary institutional order flow.
# - Dhan credentials are kept in memory only and are never written to disk.
# ============================================================


class AdaptiveQuantConfig:
    def __init__(self):
        self.atr_multiplier = 1.0
        self.optimization_score = 0.0

    def auto_tune(self, wins, losses, total):
        if total <= 0:
            return "No data to optimize."
        win_rate = (wins / total) * 100.0
        self.optimization_score = win_rate
        if win_rate < 70:
            self.atr_multiplier = 1.3
            return f"Accuracy {win_rate:.1f}%. Volatility buffer set to 1.3x."
        self.atr_multiplier = 1.0
        return f"Accuracy {win_rate:.1f}%. Baseline volatility buffer retained."


quant_config = AdaptiveQuantConfig()


# ----------------------------- HELPERS -----------------------------
def safe_float(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def fmt_num(value, decimals=2):
    value = safe_float(value)
    if value == 0:
        return "0"
    return f"{value:,.{decimals}f}"


def pct_change(current, previous):
    current = safe_float(current)
    previous = safe_float(previous)
    if previous == 0:
        return 0.0
    return ((current - previous) / previous) * 100.0


def calculate_atr(df, period=14):
    if df is None or df.empty or len(df) < 2:
        return 0.0
    try:
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        prev_close = close.shift(1)
        tr1 = (high - low).abs()
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = tr1.combine(tr2, max).combine(tr3, max)
        return safe_float(tr.rolling(period, min_periods=1).mean().iloc[-1])
    except Exception:
        return 0.0


def calculate_vwap(df):
    if df is None or df.empty:
        return 0.0
    try:
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        typical = (high + low + close) / 3.0
        if "Volume" in df.columns:
            volume = df["Volume"].fillna(0).astype(float)
            total_volume = safe_float(volume.sum())
            if total_volume > 0:
                return safe_float((typical * volume).sum() / total_volume)
        return safe_float(typical.mean())
    except Exception:
        return 0.0


def calculate_volume_signal(df):
    if df is None or df.empty or "Volume" not in df.columns:
        return "Volume unavailable", 0.0
    try:
        volume = df["Volume"].fillna(0).astype(float)
        if len(volume) < 6:
            return "Insufficient volume data", 0.0
        current = safe_float(volume.iloc[-1])
        baseline = safe_float(volume.iloc[-6:-1].mean())
        if baseline <= 0:
            return "Volume unavailable", 0.0
        change = ((current - baseline) / baseline) * 100.0
        if change >= 25:
            return f"Volume surge (+{change:.0f}% vs 5-bar avg)", change
        if change <= -25:
            return f"Volume contraction ({change:.0f}% vs 5-bar avg)", change
        return f"Volume normal ({change:+.0f}% vs 5-bar avg)", change
    except Exception:
        return "Volume unavailable", 0.0


def calculate_pivots(day_high, day_low, prev_close):
    pivot = (day_high + day_low + prev_close) / 3.0
    s1 = (2.0 * pivot) - day_high
    r1 = (2.0 * pivot) - day_low
    return pivot, s1, r1


def fetch_history(symbol, period="1d", interval="1m"):
    ticker = yf.Ticker(symbol)
    return ticker.history(
        period=period,
        interval=interval,
        auto_adjust=False,
        prepost=False,
        timeout=12,
    )


def fetch_quote(symbol):
    try:
        df = fetch_history(symbol, "5d", "1d")
        if df is None or df.empty:
            return None
        closes = df["Close"].dropna()
        if closes.empty:
            return None
        last = safe_float(closes.iloc[-1])
        prev = safe_float(closes.iloc[-2]) if len(closes) > 1 else last
        return {
            "price": last,
            "change": pct_change(last, prev),
            "time": str(closes.index[-1]),
        }
    except Exception:
        return None


# ----------------------------- NEWS ENGINE -----------------------------
POSITIVE_WORDS = {
    "surge", "rally", "gain", "gains", "growth", "strong", "stronger", "record",
    "optimism", "positive", "eases", "ease", "cuts", "cut", "stimulus", "deal",
    "agreement", "recovery", "boost", "upgrade", "inflow", "bullish", "support",
    "lower inflation", "rate cut", "ceasefire", "peace", "investment",
}
NEGATIVE_WORDS = {
    "fall", "falls", "drop", "drops", "loss", "losses", "weak", "weaker", "warning",
    "risk", "risks", "tariff", "tariffs", "war", "conflict", "inflation", "hawkish",
    "selloff", "sell-off", "outflow", "downgrade", "recession", "crisis", "sanction",
    "sanctions", "uncertainty", "bearish", "rate hike", "hike", "default",
}

LEADERS = [
    "Donald Trump",
    "Narendra Modi",
    "Jerome Powell",
    "Christine Lagarde",
    "Xi Jinping",
    "Keir Starmer",
    "Emmanuel Macron",
    "RBI",
    "U.S. Federal Reserve",
]

NEWS_QUERIES = [
    ("India Markets", "NIFTY OR Sensex OR Indian stock market OR NSE"),
    ("India Economy", "India economy OR RBI OR inflation OR GDP"),
    ("Global Macro", "Federal Reserve OR Fed OR US economy OR crude oil"),
    ("World Leaders", 'Trump OR Modi OR Powell OR Xi Jinping OR Lagarde statement markets'),
]


def sentiment_score(text):
    text = (text or "").lower()
    score = 0
    hits = []
    for word in POSITIVE_WORDS:
        if word in text:
            score += 1
            hits.append(f"+{word}")
    for word in NEGATIVE_WORDS:
        if word in text:
            score -= 1
            hits.append(f"-{word}")
    if score >= 2:
        label = "POSITIVE"
    elif score <= -2:
        label = "NEGATIVE"
    else:
        label = "MIXED"
    return score, label, hits[:5]


def leader_match(text):
    lower = (text or "").lower()
    for leader in LEADERS:
        if leader.lower() in lower:
            return leader
    # Common shortened names
    aliases = {
        "trump": "Donald Trump",
        "modi": "Narendra Modi",
        "powell": "Jerome Powell",
        "lagarde": "Christine Lagarde",
        "xi": "Xi Jinping",
        "starmer": "Keir Starmer",
        "macron": "Emmanuel Macron",
    }
    for alias, name in aliases.items():
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            return name
    return ""


def fetch_rss(url):
    headers = {"User-Agent": "Mozilla/5.0 (NIFTY-Pro-Quant-Terminal)"}
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = []
    for item in root.findall(".//item")[:15]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source = (item.findtext("source") or "Google News").strip()
        if title:
            score, label, hits = sentiment_score(title)
            items.append({
                "title": title,
                "link": link,
                "published": pub,
                "source": source,
                "score": score,
                "sentiment": label,
                "hits": hits,
                "leader": leader_match(title),
            })
    return items


def fetch_news_bundle():
    all_items = []
    seen = set()
    for category, query in NEWS_QUERIES:
        url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(query)
            + "&hl=en-IN&gl=IN&ceid=IN:IN"
        )
        try:
            rows = fetch_rss(url)
            for row in rows:
                key = row["title"].lower()
                if key in seen:
                    continue
                seen.add(key)
                row["category"] = category
                all_items.append(row)
        except Exception:
            continue

    all_items.sort(key=lambda x: (bool(x.get("leader")), x.get("score", 0)), reverse=True)
    leader_items = [x for x in all_items if x.get("leader")]
    market_items = [x for x in all_items if not x.get("leader")]

    total_score = sum(x.get("score", 0) for x in all_items[:20])
    if total_score >= 3:
        overall = "POSITIVE"
    elif total_score <= -3:
        overall = "NEGATIVE"
    else:
        overall = "MIXED"
    return {
        "items": all_items[:20],
        "leaders": leader_items[:10],
        "market": market_items[:10],
        "overall": overall,
        "score": total_score,
        "updated": datetime.now().strftime("%H:%M:%S"),
    }


# ----------------------------- NSE FII/DII -----------------------------
def fetch_fii_dii():
    url = "https://www.nseindia.com/api/fiidiiTradeReact"
    headers = {
        "User-Agent": "Mozilla/5.0 (Android 16; Mobile)",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "Connection": "keep-alive",
    }
    session = requests.Session()
    session.headers.update(headers)
    try:
        session.get("https://www.nseindia.com/", timeout=8)
        response = session.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list) and data:
            return data
        return []
    except Exception:
        return []


def parse_fii_dii(rows):
    if not rows:
        return None
    fii = None
    dii = None
    for row in rows:
        text = str(row).lower()
        if "fii" in text or "fpi" in text:
            fii = row
        if "dii" in text:
            dii = row

    def extract(row):
        if not isinstance(row, dict):
            return None
        def get_num(*keys):
            for key in keys:
                if key in row:
                    return safe_float(row.get(key), None)
            return None
        return {
            "buy": get_num("buyValue", "buy_value", "buyValueGross"),
            "sell": get_num("sellValue", "sell_value", "sellValueGross"),
            "net": get_num("netValue", "net_value", "netValueGross"),
            "date": row.get("date") or row.get("tradeDate") or row.get("timestamp") or "",
        }

    return {"fii": extract(fii), "dii": extract(dii)}


# ----------------------------- DHAN OPTION CHAIN -----------------------------
def dhan_request(client_id, token, endpoint, payload):
    headers = {
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
        "Accept": "application/json",
    }
    response = requests.post(
        "https://api.dhan.co/v2/" + endpoint,
        headers=headers,
        json=payload,
        timeout=12,
    )
    response.raise_for_status()
    return response.json()


def dhan_get_option_chain(client_id, token):
    if not client_id or not token:
        return None, "Dhan API not connected"
    try:
        expiry_response = dhan_request(
            client_id,
            token,
            "optionchain/expirylist",
            {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"},
        )
        expiries = expiry_response.get("data") or []
        if not expiries:
            return None, "Dhan: no active NIFTY expiry returned"

        today = datetime.now().date().isoformat()
        future = [x for x in expiries if str(x) >= today]
        expiry = future[0] if future else expiries[0]

        chain = dhan_request(
            client_id,
            token,
            "optionchain",
            {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I", "Expiry": expiry},
        )
        return {"expiry": expiry, "payload": chain}, "OK"
    except Exception as exc:
        return None, f"Dhan error: {str(exc)[:100]}"


def summarize_option_chain(result):
    if not result:
        return None
    payload = result.get("payload", {})
    data = payload.get("data") or {}
    oc = data.get("oc") or {}
    if not oc:
        return None

    total_ce_oi = 0.0
    total_pe_oi = 0.0
    total_ce_vol = 0.0
    total_pe_vol = 0.0
    total_ce_oi_chg = 0.0
    total_pe_oi_chg = 0.0
    strikes = []
    for strike, pair in oc.items():
        strike_val = safe_float(strike)
        strikes.append(strike_val)
        ce = pair.get("ce") or {}
        pe = pair.get("pe") or {}
        ce_oi = safe_float(ce.get("oi"))
        pe_oi = safe_float(pe.get("oi"))
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_vol += safe_float(ce.get("volume"))
        total_pe_vol += safe_float(pe.get("volume"))
        total_ce_oi_chg += ce_oi - safe_float(ce.get("previous_oi"))
        total_pe_oi_chg += pe_oi - safe_float(pe.get("previous_oi"))

    pcr = total_pe_oi / total_ce_oi if total_ce_oi else 0.0
    if pcr >= 1.10:
        bias = "PUT-OI SUPPORT"
    elif pcr <= 0.90:
        bias = "CALL-OI PRESSURE"
    else:
        bias = "BALANCED OI"

    atm = safe_float(data.get("last_price"))
    nearest = min(strikes, key=lambda x: abs(x - atm)) if strikes and atm else 0
    atm_row = oc.get(str(nearest)) or oc.get(f"{nearest:.6f}") or {}
    ce = atm_row.get("ce") or {}
    pe = atm_row.get("pe") or {}

    return {
        "expiry": result.get("expiry", ""),
        "underlying": atm,
        "pcr": pcr,
        "bias": bias,
        "ce_oi": total_ce_oi,
        "pe_oi": total_pe_oi,
        "ce_oi_chg": total_ce_oi_chg,
        "pe_oi_chg": total_pe_oi_chg,
        "ce_vol": total_ce_vol,
        "pe_vol": total_pe_vol,
        "atm": nearest,
        "atm_ce": safe_float(ce.get("last_price")),
        "atm_pe": safe_float(pe.get("last_price")),
    }


# ----------------------------- ANALYSIS ENGINE -----------------------------
def compute_technical_engine(df, vix=0.0, news_score=0, pcr=0.0):
    if df is None or df.empty:
        return {}
    closes = df["Close"].dropna().astype(float)
    if len(closes) < 5:
        return {}

    price = safe_float(closes.iloc[-1])
    vwap = calculate_vwap(df)
    atr = calculate_atr(df, 14)
    ema9 = safe_float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
    ema21 = safe_float(closes.ewm(span=21, adjust=False).mean().iloc[-1])
    momentum = pct_change(closes.iloc[-1], closes.iloc[-6]) if len(closes) >= 6 else 0.0

    score = 0
    reasons = []
    if price > vwap:
        score += 1
        reasons.append("price>VWAP")
    else:
        score -= 1
        reasons.append("price<VWAP")
    if ema9 > ema21:
        score += 1
        reasons.append("EMA9>EMA21")
    else:
        score -= 1
        reasons.append("EMA9<EMA21")
    if momentum > 0.10:
        score += 1
        reasons.append("momentum+")
    elif momentum < -0.10:
        score -= 1
        reasons.append("momentum-")
    if vix and vix > 18:
        reasons.append("VIX elevated")
    if pcr >= 1.10:
        score += 1
        reasons.append("PCR support")
    elif pcr and pcr <= 0.90:
        score -= 1
        reasons.append("PCR pressure")
    if news_score >= 3:
        score += 1
        reasons.append("news+")
    elif news_score <= -3:
        score -= 1
        reasons.append("news-")

    if score >= 3:
        verdict = "BULLISH"
    elif score <= -3:
        verdict = "BEARISH"
    else:
        verdict = "WAIT"

    return {
        "price": price,
        "vwap": vwap,
        "atr": atr,
        "ema9": ema9,
        "ema21": ema21,
        "momentum": momentum,
        "score": score,
        "verdict": verdict,
        "reasons": ", ".join(reasons),
    }


# ============================================================
# FLET APP
# ============================================================
async def main(page: ft.Page):
    page.title = "NIFTY PRO QUANT TERMINAL"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#020409"
    page.padding = 8
    page.scroll = ft.ScrollMode.AUTO
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER

    state = {
        "scanning": False,
        "price": 0.0,
        "vwap": 0.0,
        "atr": 0.0,
        "vix": 0.0,
        "pivot": 0.0,
        "verdict": "WAIT",
        "reason": "Run LIVE SCAN.",
        "news": {"items": [], "leaders": [], "market": [], "overall": "MIXED", "score": 0},
        "fii_dii": None,
        "options": None,
        "last_news_fetch": 0.0,
        "last_flow_fetch": 0.0,
        "last_option_fetch": 0.0,
        "dhan_client": "",
        "dhan_token": "",
    }

    # ---------------- HEADER / TERMINAL ----------------
    time_text = ft.Text("--:--:--", size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.CYAN_300)
    header_row = ft.Row(
        controls=[
            ft.Column(
                controls=[
                    ft.Text("NIFTY QUANT AI", size=18, weight=ft.FontWeight.W_900, color=ft.Colors.BLUE_400),
                    ft.Text("MOTO G85 MOBILE TERMINAL • PC ENGINE INTEGRATION", size=8, color=ft.Colors.CYAN_700, weight=ft.FontWeight.BOLD),
                ],
                spacing=1,
            ),
            time_text,
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
    )

    price_text = ft.Text("₹0.00", size=30, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE)
    live_status = ft.Text("Waiting...", size=10, color=ft.Colors.WHITE_54)

    # ---------------- CHART ----------------
    chart_series = fch.LineChartData(
        points=[],
        stroke_width=2,
        color=ft.Colors.CYAN_400,
        curved=True,
        rounded_stroke_cap=True,
    )
    line_chart = fch.LineChart(
        data_series=[chart_series],
        border=ft.Border.all(1, ft.Colors.WHITE_10),
        expand=True,
        min_y=0,
        max_y=1,
        min_x=0,
        max_x=1,
        interactive=True,
    )
    chart_container = ft.Container(
        content=line_chart,
        height=140,
        padding=8,
        bgcolor="#0A1128",
        border_radius=10,
        visible=False,
    )

    # ---------------- NUMERIC DATA ----------------
    sup_text = ft.Text("--", size=11)
    res_text = ft.Text("--", size=11)
    vix_text = ft.Text("--", size=11)
    atr_text = ft.Text("--", size=11)
    vwap_text = ft.Text("--", size=11)
    pivot_text = ft.Text("--", size=11)
    ema_text = ft.Text("--", size=11)
    pcr_text = ft.Text("--", size=11)

    def data_box(title, ref):
        return ft.Column(
            controls=[
                ft.Text(title, size=8, color=ft.Colors.WHITE_54, weight=ft.FontWeight.BOLD),
                ref,
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )

    data_row = ft.Container(
        content=ft.Row(
            controls=[
                data_box("S1", sup_text),
                data_box("R1", res_text),
                data_box("VIX", vix_text),
                data_box("ATR", atr_text),
                data_box("VWAP", vwap_text),
                data_box("PCR", pcr_text),
            ],
            alignment=ft.MainAxisAlignment.SPACE_EVENLY,
            scroll=ft.ScrollMode.AUTO,
        ),
        bgcolor="#0A1128",
        padding=8,
        border_radius=10,
        border=ft.Border.all(1, "#1C2A4A"),
    )

    # ---------------- ENGINE CARDS ----------------
    engine_news_text = ft.Text("Awaiting live news...", size=9, color=ft.Colors.WHITE)
    engine_tech_text = ft.Text("Awaiting quant...", size=9, color=ft.Colors.WHITE)
    engine_live_text = ft.Text("Awaiting price action...", size=9, color=ft.Colors.WHITE)

    def engine_box(title, icon, color, ref):
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(controls=[ft.Icon(icon, size=12, color=color), ft.Text(title, size=9, weight=ft.FontWeight.BOLD, color=color)]),
                    ref,
                ],
                spacing=2,
            ),
            bgcolor="#0A1128",
            padding=6,
            border_radius=8,
            border=ft.Border.only(left=ft.BorderSide(3, color)),
        )

    box_news = engine_box("ENGINE 1: MACRO / MARKET SENTIMENT", ft.Icons.PUBLIC, ft.Colors.ORANGE_400, engine_news_text)
    box_tech = engine_box("ENGINE 2: QUANT + VIX + OI", ft.Icons.DATA_EXPLORATION, ft.Colors.PURPLE_400, engine_tech_text)
    box_live = engine_box("ENGINE 3: PRICE ACTION", ft.Icons.BOLT, ft.Colors.YELLOW_400, engine_live_text)

    # ---------------- VERDICT ----------------
    final_verdict_text = ft.Text("RUN LIVE SCAN", size=11, weight=ft.FontWeight.W_900, color=ft.Colors.WHITE)
    entry_text = ft.Text("ENTRY: --", size=10)
    target_text = ft.Text("TARGET: --", size=10)
    sl_text = ft.Text("SL: --", size=10)
    reason_text = ft.Text("REASON: Awaiting...", size=9, italic=True)

    final_box = ft.Container(
        content=ft.Column(
            controls=[
                final_verdict_text,
                ft.Divider(color=ft.Colors.WHITE_24, height=4),
                ft.Row(controls=[entry_text, target_text, sl_text], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(color=ft.Colors.WHITE_24, height=4),
                reason_text,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor="#070C1E",
        padding=10,
        border_radius=10,
        border=ft.Border.all(2, ft.Colors.BLUE_700),
    )

    # ---------------- NEWS / LEADER PANEL ----------------
    news_status = ft.Text("News engine: waiting", size=9, color=ft.Colors.CYAN_300)
    leader_status = ft.Text("Important-person monitor: waiting", size=9, color=ft.Colors.ORANGE_300)
    news_list = ft.ListView(expand=True, spacing=4, auto_scroll=False)
    leader_list = ft.ListView(expand=True, spacing=4, auto_scroll=False)

    def sentiment_color(label):
        if label == "POSITIVE":
            return ft.Colors.GREEN_400
        if label == "NEGATIVE":
            return ft.Colors.RED_400
        return ft.Colors.YELLOW_400

    def make_news_tile(item):
        title = item.get("title", "")
        label = item.get("sentiment", "MIXED")
        leader = item.get("leader", "")
        prefix = f"{leader}: " if leader else ""
        return ft.Container(
            bgcolor="#050A18",
            padding=6,
            border_radius=6,
            border=ft.Border.all(1, "#172341"),
            content=ft.Column(
                controls=[
                    ft.Text(prefix + title, size=9, color=ft.Colors.WHITE, max_lines=3),
                    ft.Row(
                        controls=[
                            ft.Text(item.get("source", "News"), size=7, color=ft.Colors.WHITE_54),
                            ft.Text(label, size=7, color=sentiment_color(label), weight=ft.FontWeight.BOLD),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                ],
                spacing=2,
            ),
        )

    # ---------------- INSTITUTIONAL FLOW ----------------
    big_money_status = ft.Text("FII/DII: waiting", size=10, color=ft.Colors.GREEN_400, weight=ft.FontWeight.BOLD)
    fii_detail = ft.Text("FII: --", size=9)
    dii_detail = ft.Text("DII: --", size=9)
    oi_change_status = ft.Text("Option-chain OI: Dhan not connected", size=10, color=ft.Colors.PURPLE_300, weight=ft.FontWeight.BOLD)
    oi_detail = ft.Text("PCR: -- | ATM: -- | Expiry: --", size=9)
    institutional_note = ft.Text(
        "Institutional bias is a transparent proxy using FII/DII + option OI; it is not proprietary order-flow data.",
        size=7,
        color=ft.Colors.WHITE_54,
    )

    # ---------------- AI MENTOR ----------------
    chat_list = ft.ListView(expand=True, spacing=4, auto_scroll=True)
    chat_list.controls.append(ft.Text("🤖 AI Mentor: System ready. Run LIVE SCAN.", size=10, color=ft.Colors.CYAN_200))

    user_input = ft.TextField(
        hint_text="Ask AI Mentor...",
        expand=True,
        bgcolor="#020409",
        text_size=11,
        height=35,
        content_padding=8,
        border=ft.OutlineInputBorder(
            border_radius=6,
            side=ft.BorderSide(width=1, color=ft.Colors.WHITE_24),
        ),
    )

    async def send_ai_message(e=None):
        q = (user_input.value or "").strip()
        if not q:
            return
        chat_list.controls.append(ft.Text(f"👤: {q}", size=10, color=ft.Colors.WHITE))
        user_input.value = ""

        ql = q.lower()
        tech = state.get("technical", {})
        opt = state.get("options") or {}
        news = state.get("news") or {}
        fii = state.get("fii_dii") or {}

        if any(x in ql for x in ("news", "leader", "statement", "trump", "modi", "powell", "sentiment")):
            leaders = news.get("leaders") or []
            if leaders:
                top = leaders[0]
                reply = (
                    f"🤖 Mentor: Latest monitored headline mentioning {top.get('leader')}: "
                    f"{top.get('title')}. Headline sentiment: {top.get('sentiment')}. "
                    "This is headline classification, not proof that the statement caused the market move."
                )
            else:
                reply = "🤖 Mentor: No matching important-person headline was retrieved in the latest RSS scan."
        elif any(x in ql for x in ("oi", "option", "pcr")):
            if opt:
                reply = (
                    f"🤖 Mentor: PCR {opt.get('pcr', 0):.2f}, OI bias {opt.get('bias')}, "
                    f"ATM {opt.get('atm', 0):.0f}, expiry {opt.get('expiry')}. "
                    "Use OI as confirmation rather than a standalone signal."
                )
            else:
                reply = "🤖 Mentor: Option-chain data is not connected. Connect Dhan API to enable live OI/PCR."
        elif any(x in ql for x in ("fii", "dii", "institution", "big money")):
            if fii:
                reply = f"🤖 Mentor: {big_money_status.value}. FII/DII values are the latest retrieved public snapshot."
            else:
                reply = "🤖 Mentor: FII/DII public snapshot is currently unavailable."
        elif any(x in ql for x in ("why", "reason", "setup", "up", "down", "analysis", "kya", "kyun")):
            verdict = state.get("verdict", "WAIT")
            p = state.get("price", 0)
            v = state.get("vwap", 0)
            reasons = tech.get("reasons", "")
            reply = (
                f"🤖 Mentor: Current setup {verdict}. Price ₹{p:,.2f}, VWAP ₹{v:,.2f}. "
                f"Factors: {reasons or 'waiting for data'}. This is an analytical signal, not a guarantee."
            )
        else:
            reply = (
                "🤖 Mentor: I can explain the current setup, news/leader headlines, FII/DII flow, "
                "option OI/PCR, VIX, VWAP, ATR or price action."
            )

        chat_list.controls.append(ft.Text(reply, size=10, color=ft.Colors.CYAN_200))
        chat_list.update()

    user_input.on_submit = send_ai_message
    send_btn_chat = ft.IconButton(icon=ft.Icons.SEND, icon_size=16, icon_color=ft.Colors.BLUE_400, on_click=send_ai_message, tooltip="Send")

    # ---------------- DHAN SETUP ----------------
    dhan_client_input = ft.TextField(label="Dhan Client ID", password=False, text_size=10)
    dhan_token_input = ft.TextField(label="Dhan Access Token", password=True, can_reveal_password=True, text_size=10)
    dhan_connection_status = ft.Text("Not connected", size=9, color=ft.Colors.YELLOW_400)

    def connect_dhan(e=None):
        state["dhan_client"] = (dhan_client_input.value or "").strip()
        state["dhan_token"] = (dhan_token_input.value or "").strip()
        if state["dhan_client"] and state["dhan_token"]:
            dhan_connection_status.value = "Credentials loaded in memory. Live OI will be fetched on scan."
            dhan_connection_status.color = ft.Colors.GREEN_400
        else:
            dhan_connection_status.value = "Enter both Client ID and Access Token."
            dhan_connection_status.color = ft.Colors.YELLOW_400
        dhan_connection_status.update()

    dhan_setup = ft.Container(
        bgcolor="#07101F",
        padding=8,
        border_radius=8,
        border=ft.Border.all(1, "#263B5E"),
        content=ft.Column(
            controls=[
                ft.Text("DHAN OPTION-CHAIN CONNECTION", size=10, color=ft.Colors.PURPLE_300, weight=ft.FontWeight.BOLD),
                ft.Text("Optional: enables live NIFTY OI / PCR / ATM option data. Credentials stay in app memory only.", size=7, color=ft.Colors.WHITE_54),
                dhan_client_input,
                dhan_token_input,
                ft.Button("CONNECT DHAN", icon=ft.Icons.LINK, on_click=connect_dhan, bgcolor=ft.Colors.PURPLE_700, color=ft.Colors.WHITE),
                dhan_connection_status,
            ],
            spacing=5,
        ),
    )

    # ---------------- FLOW PANEL ----------------
    ai_flow_content = ft.Container(
        expand=True,
        bgcolor="#0A1128",
        border_radius=12,
        padding=10,
        border=ft.Border.all(1, "#1C2A4A"),
        content=ft.Column(
            controls=[
                ft.Row(controls=[ft.Icon(ft.Icons.SECURITY, color=ft.Colors.GREEN_400, size=14), ft.Text("INSTITUTIONAL FLOW & OI", weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_400, size=11)]),
                ft.Container(
                    bgcolor="#020409",
                    padding=6,
                    border_radius=6,
                    content=ft.Column(controls=[big_money_status, fii_detail, dii_detail, oi_change_status, oi_detail, institutional_note], spacing=2),
                ),
                ft.Divider(color=ft.Colors.WHITE_24, height=8),
                ft.Row(controls=[ft.Icon(ft.Icons.SUPPORT_AGENT, color=ft.Colors.BLUE_400, size=14), ft.Text("AI TRADING MENTOR (LIVE CONTEXT)", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_400, size=11)]),
                ft.Container(content=chat_list, expand=True, bgcolor="#020409", padding=6, border_radius=6),
                ft.Row(controls=[user_input, send_btn_chat], spacing=2),
                dhan_setup,
            ],
            spacing=6,
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------------- MACRO / RADAR ----------------
    radar_list = ft.ListView(expand=True, spacing=3)
    ohlc_list = ft.ListView(expand=True, spacing=3, auto_scroll=False)

    macro_content = ft.Container(
        expand=True,
        bgcolor="#0A1128",
        border_radius=12,
        padding=10,
        border=ft.Border.all(1, "#1C2A4A"),
        content=ft.Column(
            controls=[
                ft.Row(controls=[ft.Icon(ft.Icons.SATELLITE_ALT, color=ft.Colors.PURPLE_400, size=16), ft.Text("MACRO & SECTOR RADAR", weight=ft.FontWeight.BOLD, color=ft.Colors.PURPLE_400, size=12)]),
                news_status,
                radar_list,
                ft.Divider(color=ft.Colors.WHITE_24, height=8),
                ft.Row(controls=[ft.Icon(ft.Icons.RECORD_VOICE_OVER, color=ft.Colors.ORANGE_400, size=14), ft.Text("IMPORTANT PERSON / CENTRAL-BANK WATCH", weight=ft.FontWeight.BOLD, color=ft.Colors.ORANGE_400, size=10)]),
                leader_status,
                ft.Container(content=leader_list, height=170, bgcolor="#020409", padding=5, border_radius=6),
                ft.Divider(color=ft.Colors.WHITE_24, height=8),
                ft.Row(controls=[ft.Icon(ft.Icons.ACCESS_TIME, color=ft.Colors.CYAN_400, size=14), ft.Text("15-MIN OHLC DATA FEED", weight=ft.FontWeight.BOLD, color=ft.Colors.CYAN_400, size=10)]),
                ft.Container(content=ohlc_list, height=220, bgcolor="#020409", padding=5, border_radius=6),
            ],
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------------- BACKTEST ----------------
    backtest_result_text = ft.Text("Historical check will use downloaded NIFTY data.", size=11, color=ft.Colors.WHITE)
    backtest_dialog = ft.AlertDialog(
        title=ft.Text("ALGO TUNER", size=14, weight=ft.FontWeight.BOLD),
        content=ft.Container(content=backtest_result_text, width=290, height=190, padding=8),
        bgcolor="#111B2D",
        modal=True,
    )

    def update_chart(df):
        try:
            closes = [safe_float(x) for x in df["Close"].tail(40).tolist()]
            closes = [x for x in closes if x > 0]
            if len(closes) < 2:
                chart_container.visible = False
                return
            chart_series.points = [fch.LineChartDataPoint(i, value) for i, value in enumerate(closes)]
            line_chart.min_x = 0
            line_chart.max_x = max(1, len(closes) - 1)
            low, high = min(closes), max(closes)
            pad = max((high - low) * 0.10, 1.0)
            line_chart.min_y = low - pad
            line_chart.max_y = high + pad
            chart_container.visible = True
        except Exception:
            chart_container.visible = False

    async def run_backtest(e=None):
        page.show_dialog(backtest_dialog)
        backtest_result_text.value = "⏳ Running historical check..."
        backtest_result_text.update()
        try:
            df = await asyncio.to_thread(fetch_history, "^NSEI", "1y", "1d")
            if df is None or df.empty:
                raise RuntimeError("No historical NIFTY data returned.")
            closes = df["Close"].dropna().astype(float)
            if len(closes) < 30:
                raise RuntimeError("Not enough historical bars.")
            ma20 = closes.rolling(20).mean()
            returns = closes.pct_change()
            signals = (closes.shift(1) > ma20.shift(1)).astype(int) * 2 - 1
            strategy_returns = signals * returns
            valid = strategy_returns.dropna().iloc[20:]
            wins = int((valid > 0).sum())
            losses = int((valid <= 0).sum())
            total = wins + losses
            accuracy = (wins / total * 100) if total else 0.0
            result = quant_config.auto_tune(wins, losses, total)
            backtest_result_text.value = (
                f"Bars tested: {total}\nPositive bars: {wins}\nNon-positive bars: {losses}\n"
                f"Directional hit rate: {accuracy:.1f}%\n\n{result}\n\n"
                "Educational historical test; no slippage, brokerage or execution modelling."
            )
        except Exception as ex:
            backtest_result_text.value = f"Backtest/data error:\n{ex}"
        backtest_result_text.update()

    # ---------------- DATA REFRESH ENGINES ----------------
    async def refresh_news(force=False):
        now = time.time()
        if not force and now - state["last_news_fetch"] < 60:
            return
        bundle = await asyncio.to_thread(fetch_news_bundle)
        if bundle.get("items"):
            state["news"] = bundle
            state["last_news_fetch"] = now

            news_list.controls.clear()
            for item in bundle["items"][:10]:
                news_list.controls.append(make_news_tile(item))

            leader_list.controls.clear()
            for item in bundle["leaders"][:6]:
                leader_list.controls.append(make_news_tile(item))
            if not bundle["leaders"]:
                leader_list.controls.append(ft.Text("No monitored important-person headline retrieved.", size=8, color=ft.Colors.WHITE_54))

            news_status.value = f"News engine: {len(bundle['items'])} headlines • headline sentiment {bundle['overall']} • {bundle['updated']}"
            leader_status.value = f"Important-person monitor: {len(bundle['leaders'])} matching headlines • {bundle['updated']}"

    async def refresh_flow(force=False):
        now = time.time()
        if not force and now - state["last_flow_fetch"] < 60:
            return
        rows = await asyncio.to_thread(fetch_fii_dii)
        parsed = parse_fii_dii(rows)
        if parsed:
            state["fii_dii"] = parsed
            state["last_flow_fetch"] = now
            fii = parsed.get("fii") or {}
            dii = parsed.get("dii") or {}
            fii_net = fii.get("net")
            dii_net = dii.get("net")
            fii_detail.value = f"FII/FPI Net: {fmt_num(fii_net, 2) if fii_net is not None else '--'}"
            dii_detail.value = f"DII Net: {fmt_num(dii_net, 2) if dii_net is not None else '--'}"
            combined = safe_float(fii_net) + safe_float(dii_net)
            if combined > 0:
                big_money_status.value = "BIG MONEY PROXY: NET POSITIVE"
                big_money_status.color = ft.Colors.GREEN_400
            elif combined < 0:
                big_money_status.value = "BIG MONEY PROXY: NET NEGATIVE"
                big_money_status.color = ft.Colors.RED_400
            else:
                big_money_status.value = "BIG MONEY PROXY: MIXED / FLAT"
                big_money_status.color = ft.Colors.YELLOW_400
        else:
            fii_detail.value = "FII/FPI: public snapshot unavailable"
            dii_detail.value = "DII: public snapshot unavailable"
            big_money_status.value = "BIG MONEY PROXY: DATA UNAVAILABLE"

    async def refresh_options(force=False):
        now = time.time()
        if not force and now - state["last_option_fetch"] < 15:
            return
        if not state["dhan_client"] or not state["dhan_token"]:
            return
        result, message = await asyncio.to_thread(
            dhan_get_option_chain,
            state["dhan_client"],
            state["dhan_token"],
        )
        if result:
            summary = summarize_option_chain(result)
            if summary:
                state["options"] = summary
                state["last_option_fetch"] = now
                pcr_text.value = f"{summary['pcr']:.2f}"
                oi_change_status.value = f"OI BIAS: {summary['bias']}"
                oi_change_status.color = ft.Colors.GREEN_400 if summary["pcr"] >= 1.1 else ft.Colors.RED_400 if summary["pcr"] <= 0.9 else ft.Colors.PURPLE_300
                oi_detail.value = (
                    f"PCR {summary['pcr']:.2f} | ATM {summary['atm']:.0f} | Exp {summary['expiry']} | "
                    f"CE OI {summary['ce_oi']/1e5:.1f}L | PE OI {summary['pe_oi']/1e5:.1f}L"
                )
        else:
            oi_change_status.value = message

    async def refresh_macro_radar():
        symbols = [
            ("HDFC BANK", "HDFCBANK.NS"),
            ("RELIANCE", "RELIANCE.NS"),
            ("NIFTY BANK", "^NSEBANK"),
            ("NIFTY IT", "^CNXIT"),
            ("DOW JONES", "^DJI"),
            ("NASDAQ", "^IXIC"),
            ("CRUDE OIL", "CL=F"),
            ("USD/INR", "INR=X"),
        ]
        radar_list.controls.clear()

        async def one(name, symbol):
            return name, await asyncio.to_thread(fetch_quote, symbol)

        results = await asyncio.gather(*(one(a, b) for a, b in symbols))
        for name, q in results:
            if q:
                ch = q["change"]
                label = f"{q['price']:,.2f}  ({ch:+.2f}%)"
                color = ft.Colors.GREEN_400 if ch > 0 else ft.Colors.RED_400 if ch < 0 else ft.Colors.WHITE
            else:
                label = "--"
                color = ft.Colors.WHITE_54
            radar_list.controls.append(
                ft.Row(
                    controls=[
                        ft.Text(name, size=9, color=ft.Colors.WHITE_70, weight=ft.FontWeight.BOLD),
                        ft.Text(label, size=9, color=color),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                )
            )

    async def refresh_ohlc():
        try:
            df15 = await asyncio.to_thread(fetch_history, "^NSEI", "5d", "15m")
            if df15 is None or df15.empty:
                return
            ohlc_list.controls.clear()
            for _, row in df15.tail(12).iterrows():
                ts = str(row.name).replace("+00:00", "")[-16:]
                ohlc_list.controls.append(
                    ft.Text(
                        f"{ts}  O {safe_float(row.get('Open')):.1f}  H {safe_float(row.get('High')):.1f}  "
                        f"L {safe_float(row.get('Low')):.1f}  C {safe_float(row.get('Close')):.1f}",
                        size=8,
                        color=ft.Colors.WHITE_70,
                    )
                )
        except Exception:
            ohlc_list.controls.clear()
            ohlc_list.controls.append(ft.Text("15-min feed unavailable", size=8, color=ft.Colors.YELLOW_400))

    async def fetch_all_data(e=None):
        if state["scanning"]:
            return
        state["scanning"] = True
        live_status.value = "Scanning..."
        live_status.update()
        try:
            # Primary price feed.
            n_hist = await asyncio.to_thread(fetch_history, "^NSEI", "1d", "1m")
            if n_hist is None or n_hist.empty:
                raise RuntimeError("No NIFTY data returned.")
            close_series = n_hist["Close"].dropna()
            if close_series.empty:
                raise RuntimeError("NIFTY close unavailable.")

            price = safe_float(close_series.iloc[-1])
            vwap = calculate_vwap(n_hist)
            atr = calculate_atr(n_hist, 14)
            day_high = safe_float(n_hist["High"].max())
            day_low = safe_float(n_hist["Low"].min())
            previous_close = safe_float(close_series.iloc[-2]) if len(close_series) >= 2 else price
            pivot, s1, r1 = calculate_pivots(day_high, day_low, previous_close)

            # VIX.
            vix = 0.0
            try:
                vix_df = await asyncio.to_thread(fetch_history, "^INDIAVIX", "1d", "1m")
                if vix_df is not None and not vix_df.empty:
                    vix = safe_float(vix_df["Close"].dropna().iloc[-1])
            except Exception:
                pass

            # Refresh independent engines in parallel.
            await asyncio.gather(
                refresh_news(),
                refresh_flow(),
                refresh_options(),
                refresh_macro_radar(),
                refresh_ohlc(),
            )

            news = state.get("news") or {}
            opt = state.get("options") or {}
            tech = compute_technical_engine(
                n_hist,
                vix=vix,
                news_score=safe_float(news.get("score")),
                pcr=safe_float(opt.get("pcr")),
            )
            state["technical"] = tech
            state["price"] = price
            state["vwap"] = vwap
            state["atr"] = atr
            state["vix"] = vix
            state["pivot"] = pivot
            state["verdict"] = tech.get("verdict", "WAIT")

            volume_msg, volume_pct = calculate_volume_signal(n_hist)
            pcr = safe_float(opt.get("pcr"))
            news_label = news.get("overall", "MIXED")
            fii = state.get("fii_dii") or {}
            fii_net = safe_float((fii.get("fii") or {}).get("net"))
            dii_net = safe_float((fii.get("dii") or {}).get("net"))

            if state["verdict"] == "BULLISH":
                reason = f"Bullish structure: {tech.get('reasons', 'multi-factor confirmation')}."
            elif state["verdict"] == "BEARISH":
                reason = f"Bearish structure: {tech.get('reasons', 'multi-factor confirmation')}."
            else:
                reason = f"Mixed factors: {tech.get('reasons', 'awaiting confirmation')}."
            state["reason"] = reason

            # UI update.
            price_text.value = f"₹{price:,.2f}"
            sup_text.value = f"{s1:.0f}"
            res_text.value = f"{r1:.0f}"
            vix_text.value = f"{vix:.2f}" if vix else "--"
            atr_text.value = f"{atr:.1f}"
            vwap_text.value = f"{vwap:.1f}"
            pivot_text.value = f"{pivot:.1f}"
            ema_text.value = f"{tech.get('ema9', 0):.1f}/{tech.get('ema21', 0):.1f}"
            pcr_text.value = f"{pcr:.2f}" if pcr else "--"

            engine_news_text.value = (
                f"News: {news_label} ({news.get('score', 0):+d} score) | "
                f"Important-person hits: {len(news.get('leaders', []))}"
            )
            engine_tech_text.value = (
                f"Score {tech.get('score', 0):+d} | EMA9/21 {tech.get('ema9', 0):.1f}/{tech.get('ema21', 0):.1f} | "
                f"VIX {vix:.2f} | PCR {pcr:.2f}" if pcr else
                f"Score {tech.get('score', 0):+d} | EMA9/21 {tech.get('ema9', 0):.1f}/{tech.get('ema21', 0):.1f} | VIX {vix:.2f} | PCR unavailable"
            )
            engine_live_text.value = (
                f"Price ₹{price:,.2f} | VWAP ₹{vwap:,.2f} | {volume_msg} | "
                f"Support {s1:.0f} / Resistance {r1:.0f}"
            )

            if state["verdict"] == "BULLISH":
                final_verdict_text.value = "VERDICT: BULLISH"
            elif state["verdict"] == "BEARISH":
                final_verdict_text.value = "VERDICT: BEARISH"
            else:
                final_verdict_text.value = "VERDICT: WAIT / MIXED"

            buffer = atr * max(1.0, quant_config.atr_multiplier)
            entry_text.value = f"ENTRY: ₹{price:.0f}"
            target_text.value = f"TARGET: ₹{price + buffer * 1.5:.0f}" if state["verdict"] != "BEARISH" else f"TARGET: ₹{price - buffer * 1.5:.0f}"
            sl_text.value = f"SL: ₹{price - buffer:.0f}" if state["verdict"] != "BEARISH" else f"SL: ₹{price + buffer:.0f}"
            reason_text.value = f"REASON: {reason}"

            update_chart(n_hist)

            live_status.value = datetime.now().strftime("%H:%M:%S")

            # Update controls after the parallel engine calls.
            for control in (
                price_text, sup_text, res_text, vix_text, atr_text, vwap_text,
                pcr_text, engine_news_text, engine_tech_text, engine_live_text,
                final_verdict_text, entry_text, target_text, sl_text, reason_text,
                big_money_status, fii_detail, dii_detail, oi_change_status, oi_detail,
                news_status, leader_status, radar_list, leader_list, news_list,
                ohlc_list, chart_container, live_status,
            ):
                try:
                    control.update()
                except Exception:
                    pass

        except Exception as ex:
            live_status.value = "Data Error"
            reason_text.value = f"REASON: {str(ex)[:180]}"
            engine_news_text.value = "Market/news engine encountered a data-source error."
            for control in (live_status, reason_text, engine_news_text):
                try:
                    control.update()
                except Exception:
                    pass
        finally:
            state["scanning"] = False

    # ---------------- BUTTONS / AUTO SCAN ----------------
    scan_btn = ft.Button(
        content="LIVE SCAN",
        icon=ft.Icons.RADAR,
        bgcolor=ft.Colors.BLUE_700,
        color=ft.Colors.WHITE,
        on_click=fetch_all_data,
        expand=True,
    )
    backtest_btn = ft.Button(
        content="SCALP TEST",
        icon=ft.Icons.STAR,
        bgcolor=ft.Colors.PURPLE_700,
        color=ft.Colors.WHITE,
        on_click=run_backtest,
        expand=True,
    )
    auto_scan_switch = ft.Switch(label="Auto Scan (15s)", value=False, active_color=ft.Colors.GREEN_400)

    terminal_content = ft.Container(
        expand=True,
        content=ft.Column(
            controls=[
                header_row,
                ft.Row(controls=[price_text, live_status], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                chart_container,
                data_row,
                box_news,
                box_tech,
                box_live,
                final_box,
                ft.Row(controls=[scan_btn, backtest_btn], spacing=10),
                ft.Row(controls=[auto_scan_switch], alignment=ft.MainAxisAlignment.CENTER),
            ],
            expand=True,
            spacing=6,
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    tab_bar = ft.TabBar(
        tabs=[
            ft.Tab(label=ft.Text("Terminal"), icon=ft.Icons.DASHBOARD),
            ft.Tab(label=ft.Text("AI & Flow"), icon=ft.Icons.SUPPORT_AGENT),
            ft.Tab(label=ft.Text("Macro"), icon=ft.Icons.PUBLIC),
        ],
        scrollable=True,
    )
    tab_views = ft.TabBarView(expand=True, controls=[terminal_content, ai_flow_content, macro_content])
    main_tabs = ft.Tabs(
        length=3,
        selected_index=0,
        expand=True,
        content=ft.Column(expand=True, controls=[tab_bar, tab_views]),
    )

    page.add(ft.SafeArea(expand=True, content=main_tabs))

    async def update_clock():
        while True:
            try:
                time_text.value = datetime.now().strftime("%H:%M:%S")
                time_text.update()
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)

    async def auto_scan_loop():
        while True:
            try:
                if auto_scan_switch.value and not state["scanning"]:
                    await fetch_all_data()
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(15)

    page.run_task(update_clock)
    page.run_task(auto_scan_loop)


if __name__ == "__main__":
    ft.run(main)
