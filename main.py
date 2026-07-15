import os
import sys
import html
import smtplib
import ssl
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

try:
    import requests
except ImportError:
    print("Missing 'requests' library. Run: pip3 install requests")
    sys.exit(1)

_session = requests.Session()

IST = timezone(timedelta(hours=5, minutes=30))
AI_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "deep learning",
    "llm", "gpt", "openai", "anthropic", "claude", "gemini", "llama",
    "neural network", "transformer", "diffusion", "chatgpt", "copilot",
    "mistral", "hugging face", "langchain", "rag", "agent", "autonomous",
    "robotics", "computer vision", "nlp", "large language model",
    "fine-tuning", "rlhf", "generative ai", "genai", "perplexity",
    "cursor", "bolt", "cohere", "midjourney", "sora", "veo",
]


def keyword_score(text: str) -> int:
    text_lower = text.lower()
    return sum(1 for kw in AI_KEYWORDS if kw in text_lower)


def _fetch_hn_item(item_id: int, session: requests.Session) -> Optional[Dict]:
    try:
        s = session.get(
            f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json",
            timeout=8,
        ).json()
        title = s.get("title", "")
        ts = s.get("time", 0)
        if not title or keyword_score(title) < 1:
            return None
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
        if ts < cutoff:
            return None
        return {
            "title": title,
            "url": s.get("url") or f"https://news.ycombinator.com/item?id={item_id}",
            "points": s.get("score", 0) or 0,
            "source": "Hacker News",
        }
    except Exception:
        return None


def fetch_hn_top() -> List[Dict]:
    try:
        r = _session.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=15,
        )
        r.raise_for_status()
        ids = r.json()[:30]
        results = []
        with ThreadPoolExecutor(max_workers=15) as pool:
            futs = {pool.submit(_fetch_hn_item, sid, _session): sid for sid in ids}
            for fut in as_completed(futs, timeout=25):
                item = fut.result()
                if item:
                    results.append(item)
        results.sort(key=lambda x: x["points"], reverse=True)
        return results[:15]
    except Exception as e:
        print(f"[WARN] HN top fetch failed: {e}")
        return []


def fetch_hn_search() -> List[Dict]:
    queries = ["AI", "GPT", "LLM", "machine learning", "OpenAI", "Claude", "Gemini"]
    min_ts = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    seen = set()
    results = []
    for q in queries:
        try:
            r = _session.get(
                "https://hn.algolia.com/api/v1/search",
                params={
                    "query": q,
                    "tags": "story",
                    "hitsPerPage": 8,
                    "numericFilters": f"created_at_i>{min_ts}",
                },
                timeout=12,
            )
            r.raise_for_status()
            for hit in r.json().get("hits", []):
                title = hit.get("title", "")
                key = title.lower().strip()
                if key not in seen and keyword_score(title) >= 1:
                    seen.add(key)
                    results.append({
                        "title": title,
                        "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                        "points": hit.get("points", 0) or 0,
                        "source": "Hacker News",
                    })
        except Exception as e:
            print(f"[WARN] HN search '{q}' failed: {e}")
            continue
    results.sort(key=lambda x: x["points"], reverse=True)
    return results[:10]


def fetch_devto() -> List[Dict]:
    results = []
    for tag in ["ai", "machinelearning", "llm", "generative-ai"]:
        try:
            r = _session.get(
                "https://dev.to/api/articles",
                params={"tag": tag, "per_page": 8},
                timeout=12,
            )
            r.raise_for_status()
            for art in r.json():
                title = art.get("title", "")
                if keyword_score(title) >= 1:
                    results.append({
                        "title": title,
                        "url": art.get("url", ""),
                        "points": art.get("positive_reactions_count", 0) or 0,
                        "source": "Dev.to",
                    })
        except Exception as e:
            print(f"[WARN] Dev.to/{tag} failed: {e}")
            continue

    seen = set()
    deduped = []
    for item in results:
        key = item["title"].lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    deduped.sort(key=lambda x: x["points"], reverse=True)
    return deduped[:10]


def fetch_rss() -> List[Dict]:
    feeds = [
        ("https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", "NYT Tech"),
        ("https://feeds.feedburner.com/TechCrunch", "TechCrunch"),
    ]
    results = []
    for url, source_name in feeds:
        try:
            r = _session.get(url, timeout=12)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            ns = {"": "http://www.w3.org/2005/Atom"}
            for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
                title = entry.findtext("{http://www.w3.org/2005/Atom}title", "")
                link_el = entry.find("{http://www.w3.org/2005/Atom}link")
                link = link_el.get("href", "") if link_el is not None else ""
                if keyword_score(title) >= 1:
                    results.append({
                        "title": title,
                        "url": link,
                        "points": 0,
                        "source": source_name,
                    })
            for item in root.iter("item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                if keyword_score(title) >= 1:
                    results.append({
                        "title": title,
                        "url": link,
                        "points": 0,
                        "source": source_name,
                    })
        except Exception as e:
            print(f"[WARN] RSS {source_name} failed: {e}")
            continue
    return results


def fetch_newsapi() -> List[Dict]:
    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        return []
    try:
        r = _session.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "artificial intelligence OR AI OR machine learning OR LLM OR GPT",
                "language": "en",
                "sortBy": "popularity",
                "pageSize": 15,
                "from": (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S"),
            },
            headers={"X-Api-Key": api_key},
            timeout=12,
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "url": a.get("url", ""),
                "points": 0,
                "source": a.get("source", {}).get("name", "News"),
            }
            for a in articles if a.get("title") and keyword_score(a.get("title", "")) >= 1
        ]
    except Exception as e:
        print(f"[WARN] NewsAPI fetch failed: {e}")
        return []


def fetch_all_news() -> List[Dict]:
    seen = set()
    all_news = []

    fetchers = [fetch_hn_top, fetch_hn_search, fetch_devto, fetch_rss, fetch_newsapi]
    for fetcher in fetchers:
        for item in fetcher():
            key = item["title"].lower().strip()
            if key not in seen:
                seen.add(key)
                all_news.append(item)

    all_news.sort(key=lambda x: x.get("points", 0), reverse=True)
    return all_news[:20]


def build_email_html(news: List[Dict]) -> str:
    date_str = datetime.now(IST).strftime("%B %d, %Y")
    items_html = ""
    for i, item in enumerate(news, 1):
        safe_title = html.escape(item["title"])
        safe_url = html.escape(item["url"])
        safe_source = html.escape(item["source"])
        badge = f'<span style="background:#e5e7eb;color:#374151;font-size:12px;padding:2px 8px;border-radius:4px;margin-left:8px">{safe_source}</span>'
        score = f'<span style="color:#6b7280;font-size:13px"> | {item["points"]} pts</span>' if item.get("points") else ""
        items_html += f"""
        <tr>
            <td style="padding:14px 20px;border-bottom:1px solid #e5e7eb">
                <table width="100%" cellpadding="0" cellspacing="0"><tr>
                    <td width="28" valign="top" style="color:#9ca3af;font-weight:600;font-size:15px">{i}.</td>
                    <td valign="top">
                        <a href="{safe_url}" style="color:#1d4ed8;text-decoration:none;font-size:15px;font-weight:500;line-height:1.4" target="_blank">{safe_title}</a>
                        <div style="margin-top:5px;font-size:12px">{badge}{score}</div>
                    </td>
                </tr></table>
            </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 12px">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.08)">
<tr>
<td style="background:linear-gradient(135deg,#1e40af,#7c3aed);padding:28px 32px;text-align:center">
<img src="https://cdn-icons-png.flaticon.com/512/11866/11866939.png" width="44" height="44" alt="AI" style="vertical-align:middle;margin-bottom:8px">
<h1 style="margin:8px 0 4px;color:#ffffff;font-size:22px;font-weight:700;letter-spacing:-0.3px">Daily AI Brief</h1>
<p style="margin:0;color:rgba(255,255,255,0.8);font-size:14px">{date_str}</p>
</td>
</tr>
<tr><td style="padding:6px 24px;background:#f0f9ff;font-size:13px;color:#1e40af;text-align:center;border-bottom:1px solid #e0f2fe">
Top {len(news)} curated AI updates from across the web
</td></tr>
<tr><td style="padding:0">
<table width="100%" cellpadding="0" cellspacing="0">
{items_html}
</table>
</td></tr>
<tr><td style="padding:18px 32px;background:#f8fafc;text-align:center;font-size:12px;color:#94a3b8;border-top:1px solid #e2e8f0">
Generated {datetime.now(IST).strftime("%I:%M %p %Z")} &bull; Your daily AI digest
</td></tr>
</table>
</td></tr></table>
</body>
</html>"""


def send_email(news: List[Dict]):
    smtp_server = "smtp.gmail.com"
    smtp_port = 465
    sender = os.environ.get("GMAIL_USER") or os.environ.get("GMAIL_SENDER")
    password = os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("GMAIL_PASSWORD")
    recipient = os.environ.get("GMAIL_RECIPIENT", "lifeschool878@gmail.com")

    if not sender or not password:
        print("ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must be set")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Daily AI Brief — {datetime.now(IST).strftime('%b %d, %Y')}"
    msg["From"] = sender
    msg["To"] = recipient

    plain = f"Daily AI Brief — {datetime.now(IST).strftime('%b %d, %Y')}\n\n"
    for i, item in enumerate(news, 1):
        plain += f"{i}. [{item['source']}] {item['title']}\n   {item['url']}\n"
    plain += f"\nGenerated at {datetime.now(IST).strftime('%I:%M %p %Z')}"

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(build_email_html(news), "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    print(f"Email sent successfully to {recipient}")


def main():
    print("=== Daily AI Brief ===")
    print(f"Time: {datetime.now(IST).strftime('%Y-%m-%d %I:%M %p %Z')}")
    print("Fetching news...")

    news = fetch_all_news()

    if not news:
        print("ERROR: No news fetched. Skipping email.")
        sys.exit(1)

    print(f"Found {len(news)} stories:")
    for i, n in enumerate(news[:8], 1):
        print(f"  {i}. [{n['source']}] {n['title']} ({n.get('points', 0)} pts)")
    if len(news) > 8:
        print(f"  ... and {len(news)-8} more")

    if not os.environ.get("GMAIL_USER") or not os.environ.get("GMAIL_APP_PASSWORD"):
        print("\nSet GMAIL_USER and GMAIL_APP_PASSWORD env vars to send email.")
        print("For now, printing news to stdout only.")
        return

    send_email(news)
    print("Done!")


if __name__ == "__main__":
    main()
