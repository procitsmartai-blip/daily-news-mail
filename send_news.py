import feedparser
import smtplib
import os
import json
import requests
import time

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import pytz

# ── credentials from GitHub Secrets ──────────────────────────────────────────
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_RECIPIENTS   = [e.strip() for e in os.environ["EMAIL_RECIPIENTS"].split(",")]

# ── RSS feed sources ──────────────────────────────────────────────────────────
RSS_FEEDS = {
    "🌍 Geopolitics": [
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://feeds.skynews.com/feeds/rss/world.xml",
    ],
    "💻 Technology": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "http://feeds.bbci.co.uk/news/technology/rss.xml",
    ],
    "💰 Finance": [
        "http://feeds.bbci.co.uk/news/business/rss.xml",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "https://feeds.marketwatch.com/marketwatch/topstories/",
    ],
    "🇮🇩 Indonesia": [
        "https://rss.kompas.com/breakingnews",
        "https://www.antaranews.com/rss/terkini.xml",
        "https://www.tempo.co/rss/terkini",
    ],
}

# ── fetch market data (no yfinance — uses direct free APIs) ──────────────────
def fetch_yahoo_quote(symbol):
    """Direct Yahoo Finance API — works on GitHub Actions."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    data  = resp.json()["chart"]["result"][0]
    closes = [c for c in data["indicators"]["quote"][0]["close"] if c is not None]
    if len(closes) < 2:
        raise ValueError("Not enough data points")
    prev  = closes[-2]
    close = closes[-1]
    return close, ((close - prev) / prev) * 100

def fetch_usd_idr():
    """Frankfurter API for reliable USD/IDR rate."""
    resp = requests.get(
        "https://api.frankfurter.app/latest?from=USD&to=IDR",
        timeout=10
    )
    resp.raise_for_status()
    rate = resp.json()["rates"]["IDR"]
    # get yesterday rate for % change
    resp2 = requests.get(
        "https://api.frankfurter.app/latest?from=USD&to=IDR&date=prev",
        timeout=10
    )
    # frankfurter doesn't support prev directly; use Yahoo as fallback for change
    return rate, None

def fetch_markets():
    results = []

    # USD/IDR via Yahoo (most reliable for forex)
    try:
        close, chg = fetch_yahoo_quote("IDR=X")
        results.append({
            "label": "USD/IDR", "value": f"{close:,.0f}",
            "change_pct": round(chg, 2),
            "direction": "up" if chg >= 0 else "down",
        })
    except Exception as e:
        print(f"  [warn] USD/IDR failed: {e}")
        results.append({"label": "USD/IDR", "value": "N/A", "change_pct": 0, "direction": "flat"})

    # IHSG via Yahoo
    try:
        close, chg = fetch_yahoo_quote("%5EJKSE")
        results.append({
            "label": "IHSG", "value": f"{close:,.0f}",
            "change_pct": round(chg, 2),
            "direction": "up" if chg >= 0 else "down",
        })
    except Exception as e:
        print(f"  [warn] IHSG failed: {e}")
        results.append({"label": "IHSG", "value": "N/A", "change_pct": 0, "direction": "flat"})

    # Gold via Yahoo
    try:
        close, chg = fetch_yahoo_quote("GC%3DF")
        results.append({
            "label": "Gold", "value": f"${close:,.1f}",
            "change_pct": round(chg, 2),
            "direction": "up" if chg >= 0 else "down",
        })
    except Exception as e:
        print(f"  [warn] Gold failed: {e}")
        results.append({"label": "Gold", "value": "N/A", "change_pct": 0, "direction": "flat"})

    # Brent crude via Yahoo
    try:
        close, chg = fetch_yahoo_quote("BZ%3DF")
        results.append({
            "label": "Brent", "value": f"${close:,.1f}",
            "change_pct": round(chg, 2),
            "direction": "up" if chg >= 0 else "down",
        })
    except Exception as e:
        print(f"  [warn] Brent failed: {e}")
        results.append({"label": "Brent", "value": "N/A", "change_pct": 0, "direction": "flat"})

    return results

# ── fetch raw articles from RSS feeds ────────────────────────────────────────
def fetch_articles(feeds, limit=15):
    articles = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                articles.append({
                    "title":     entry.get("title", "").strip(),
                    "summary":   entry.get("summary", entry.get("description", ""))[:400].strip(),
                    "link":      entry.get("link", ""),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"  [warn] Could not fetch {url}: {e}")
    return articles[:limit]

# ── call Groq with retry + backoff ────────────────────────────────────────────
def call_groq(prompt, max_tokens=1500):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       "llama-3.1-8b-instant",
                    "messages":    [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens":  max_tokens,
                },
                timeout=30,
            )
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"  [rate limit] Waiting {wait}s (attempt {attempt+1}/{max_retries})...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return raw
        except requests.exceptions.HTTPError as e:
            if attempt < max_retries - 1:
                time.sleep(15 * (attempt + 1))
            else:
                raise
    raise RuntimeError("Groq failed after max retries")

# ── summarise + sentiment for one category ────────────────────────────────────
def summarize_category(category, articles):
    prompt = f"""You are a sharp, concise news curator. Below are raw RSS articles about {category}.

Pick the TOP 5 most important stories. For each return:
- headline: punchy, max 12 words
- summary: 2-3 sentences a busy executive reads in 10 seconds
- url: the original link
- sentiment: exactly one of: Positive, Neutral, Negative

Return ONLY raw JSON, no markdown fences, no preamble:
{{
  "articles": [
    {{"headline": "...", "summary": "...", "url": "...", "sentiment": "..."}}
  ]
}}

Articles:
{json.dumps(articles, indent=2, ensure_ascii=False)}
"""
    raw = call_groq(prompt)
    return json.loads(raw)["articles"]

# ── executive summary across all sections ─────────────────────────────────────
def build_exec_summary(sections):
    all_headlines = []
    for category, articles in sections.items():
        for a in articles:
            all_headlines.append(f"[{category}] {a['headline']}")

    prompt = f"""You are a senior analyst writing a morning briefing intro for a busy Indonesian executive.

Based on these today's top headlines, write a 3-sentence executive summary that:
- Highlights the 2-3 most globally significant developments
- Notes any direct relevance to Indonesia or Southeast Asia
- Is written in confident, neutral, professional English

Return ONLY the 3-sentence paragraph. No preamble, no labels, no markdown.

Headlines:
{chr(10).join(all_headlines)}
"""
    return call_groq(prompt, max_tokens=300).strip()

# ── build market snapshot HTML block ─────────────────────────────────────────
def market_html(markets):
    cards = ""
    for m in markets:
        if m["direction"] == "up":
            arrow = "&#9650;"
            color = "#16a34a"
        elif m["direction"] == "down":
            arrow = "&#9660;"
            color = "#dc2626"
        else:
            arrow = "&#9644;"
            color = "#6b7280"

        change_str = f"{arrow} {abs(m['change_pct'])}%" if m["value"] != "N/A" else "N/A"
        cards += f"""
        <td style="padding:0 4px;">
          <div style="background:#f8fafc;border-radius:6px;padding:8px 10px;
                      border:0.5px solid #e2e8f0;text-align:left;">
            <p style="margin:0 0 2px;font-size:10px;color:#94a3b8;">{m['label']}</p>
            <p style="margin:0 0 1px;font-size:14px;font-weight:600;color:#111;">{m['value']}</p>
            <p style="margin:0;font-size:11px;color:{color};">{change_str}</p>
          </div>
        </td>"""
    return f"""
    <tr>
      <td style="padding:16px 28px 4px;">
        <p style="margin:0 0 8px;font-size:11px;font-weight:600;color:#94a3b8;
                  letter-spacing:.06em;text-transform:uppercase;">Markets</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>{cards}</tr>
        </table>
      </td>
    </tr>"""

# ── sentiment badge ────────────────────────────────────────────────────────────
def sentiment_badge(sentiment):
    styles = {
        "Positive": ("background:#dcfce7;color:#15803d;", "Positive"),
        "Negative": ("background:#fee2e2;color:#dc2626;", "Negative"),
        "Neutral":  ("background:#f3f4f6;color:#6b7280;", "Neutral"),
    }
    style, label = styles.get(sentiment, styles["Neutral"])
    return (f'<span style="{style}font-size:10px;padding:2px 8px;'
            f'border-radius:99px;font-weight:600;white-space:nowrap;">{label}</span>')

# ── build full HTML email ──────────────────────────────────────────────────────
def build_html(sections, exec_summary, markets):
    jakarta  = pytz.timezone("Asia/Jakarta")
    date_str = datetime.now(jakarta).strftime("%A, %d %B %Y")

    article_rows = ""
    for category, items in sections.items():
        article_rows += f"""
        <tr>
          <td style="padding:24px 28px 4px;">
            <p style="margin:0;font-size:15px;font-weight:700;color:#111;
                      border-left:3px solid #2563eb;padding-left:10px;">{category}</p>
          </td>
        </tr>"""
        for i, art in enumerate(items, 1):
            border = "border-bottom:0.5px solid #f0f0f0;" if i < len(items) else ""
            badge  = sentiment_badge(art.get("sentiment", "Neutral"))
            article_rows += f"""
        <tr>
          <td style="padding:10px 28px;{border}">
            <div style="display:flex;align-items:flex-start;
                        justify-content:space-between;gap:8px;margin-bottom:4px;">
              <p style="margin:0;font-size:13px;font-weight:600;color:#111;
                        line-height:1.4;flex:1;">{i}. {art['headline']}</p>
              {badge}
            </div>
            <p style="margin:0 0 6px;font-size:12px;color:#555;line-height:1.6;">{art['summary']}</p>
            <a href="{art['url']}" style="font-size:11px;color:#2563eb;text-decoration:none;">
              Read more &rarr;
            </a>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f4f4f5;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#f4f4f5;padding:28px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:10px;overflow:hidden;
                    box-shadow:0 1px 4px rgba(0,0,0,.08);">

        <!-- header -->
        <tr>
          <td style="background:#0f172a;padding:22px 28px;">
            <p style="margin:0;font-size:20px;font-weight:700;color:#fff;">Daily Briefing</p>
            <p style="margin:4px 0 0;font-size:12px;color:#94a3b8;">
              {date_str} &nbsp;&middot;&nbsp; Jakarta, Indonesia</p>
          </td>
        </tr>

        <!-- executive summary -->
        <tr>
          <td style="padding:20px 28px 8px;">
            <div style="background:#f0f7ff;border-left:3px solid #2563eb;
                        padding:12px 14px;border-radius:0 6px 6px 0;">
              <p style="margin:0 0 6px;font-size:10px;font-weight:700;color:#2563eb;
                        text-transform:uppercase;letter-spacing:.08em;">Today's summary</p>
              <p style="margin:0;font-size:13px;color:#374151;line-height:1.65;">
                {exec_summary}</p>
            </div>
          </td>
        </tr>

        <!-- market snapshot -->
        {market_html(markets)}

        <!-- articles -->
        {article_rows}

        <!-- footer -->
        <tr>
          <td style="background:#f8fafc;border-top:0.5px solid #e2e8f0;
                     padding:14px 28px;margin-top:8px;">
            <p style="margin:0;font-size:11px;color:#94a3b8;text-align:center;">
              Curated automatically &middot; Delivered every day at 7:00 AM WIB
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

# ── send via Gmail SMTP ───────────────────────────────────────────────────────
def send_email(html):
    jakarta = pytz.timezone("Asia/Jakarta")
    subject = f"Daily Briefing — {datetime.now(jakarta).strftime('%d %b %Y')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = ", ".join(EMAIL_RECIPIENTS)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, EMAIL_RECIPIENTS, msg.as_string())

    print(f"✅ Email sent to {EMAIL_RECIPIENTS}")

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print("Fetching market data...")
    markets = fetch_markets()
    print(f"  Got {len(markets)} market tickers")

    sections = {}
    for category, feeds in RSS_FEEDS.items():
        print(f"Fetching: {category}")
        articles = fetch_articles(feeds)
        if not articles:
            print(f"  [skip] No articles found")
            continue
        print(f"  Summarising with Groq...")
        try:
            top5 = summarize_category(category, articles)
            sections[category] = top5
            print(f"  Got {len(top5)} articles")
        except Exception as e:
            print(f"  [error] {e}")
        time.sleep(3)

    if not sections:
        raise RuntimeError("No sections produced — aborting.")

    print("Building executive summary...")
    exec_summary = build_exec_summary(sections)
    time.sleep(2)

    html = build_html(sections, exec_summary, markets)
    send_email(html)

if __name__ == "__main__":
    main()
