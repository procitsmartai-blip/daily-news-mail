import feedparser
import smtplib
import os
import json
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import pytz

# ── credentials from GitHub Secrets ──────────────────────────────────────────
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
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

# ── ask Gemini to pick and summarise top 5 ───────────────────────────────────
def summarize_with_gemini(category, articles):
    prompt = f"""You are a sharp, concise news curator. Below are raw RSS articles about {category}.

Pick the TOP 5 most important stories and for each write:
- A punchy headline (max 12 words)
- A clear 2-3 sentence summary a busy executive can read in 10 seconds
- The original URL

Return ONLY raw JSON — no markdown fences, no preamble — exactly like this:
{{
  "articles": [
    {{"headline": "...", "summary": "...", "url": "..."}}
  ]
}}

Articles:
{json.dumps(articles, indent=2, ensure_ascii=False)}
"""

    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1500},
        },
        timeout=30,
    )
    resp.raise_for_status()

    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    # strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)["articles"]

# ── build the HTML email ──────────────────────────────────────────────────────
def build_html(sections):
    jakarta = pytz.timezone("Asia/Jakarta")
    date_str = datetime.now(jakarta).strftime("%A, %d %B %Y")

    body_rows = ""
    for category, items in sections.items():
        body_rows += f"""
        <tr>
          <td style="padding:28px 0 6px;">
            <p style="margin:0;font-size:17px;font-weight:700;color:#111;
                      border-left:3px solid #2563eb;padding-left:12px;">{category}</p>
          </td>
        </tr>"""

        for i, art in enumerate(items, 1):
            border = "border-bottom:1px solid #f0f0f0;" if i < len(items) else ""
            body_rows += f"""
        <tr>
          <td style="padding:14px 0;{border}">
            <p style="margin:0 0 6px;font-size:15px;font-weight:600;color:#111;
                      line-height:1.45;">{i}. {art['headline']}</p>
            <p style="margin:0 0 8px;font-size:14px;color:#444;line-height:1.65;">{art['summary']}</p>
            <a href="{art['url']}"
               style="font-size:13px;color:#2563eb;text-decoration:none;">Read more →</a>
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
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:32px 0;">
    <tr><td align="center">
      <table width="620" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:10px;overflow:hidden;
                    box-shadow:0 1px 4px rgba(0,0,0,.08);">

        <!-- header -->
        <tr>
          <td style="background:#0f172a;padding:26px 32px;">
            <p style="margin:0;font-size:22px;font-weight:700;
                      color:#fff;letter-spacing:-.3px;">🗞️ Daily Briefing</p>
            <p style="margin:6px 0 0;font-size:13px;color:#94a3b8;">
              {date_str} &nbsp;·&nbsp; Jakarta, Indonesia</p>
          </td>
        </tr>

        <!-- articles -->
        <tr>
          <td style="padding:4px 32px 32px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              {body_rows}
            </table>
          </td>
        </tr>

        <!-- footer -->
        <tr>
          <td style="background:#f8fafc;border-top:1px solid #e2e8f0;
                     padding:18px 32px;">
            <p style="margin:0;font-size:12px;color:#94a3b8;text-align:center;">
              Curated automatically · Delivered every day at 7:00 AM WIB
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
    subject = f"🗞️ Daily Briefing — {datetime.now(jakarta).strftime('%d %b %Y')}"

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
    sections = {}
    for category, feeds in RSS_FEEDS.items():
        print(f"Fetching: {category}")
        articles = fetch_articles(feeds)
        if not articles:
            print(f"  [skip] No articles found for {category}")
            continue
        print(f"  Summarising with Gemini...")
        try:
            top5 = summarize_with_gemini(category, articles)
            sections[category] = top5
            print(f"  ✓ Got {len(top5)} articles")
        except Exception as e:
            print(f"  [error] Gemini failed for {category}: {e}")

    if not sections:
        raise RuntimeError("No sections were produced — aborting.")

    html = build_html(sections)
    send_email(html)

if __name__ == "__main__":
    main()
