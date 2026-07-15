import os
import sys
import re
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
MAX_STORIES = 10


def keyword_score(text: str) -> int:
    return sum(1 for kw in AI_KEYWORDS if kw in text.lower())


def fetch_article_text(url: str) -> str:
    if not url:
        return ""
    try:
        r = _session.get(
            url, timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        )
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return ""
        text = r.text
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<nav[^>]*>.*?</nav>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<footer[^>]*>.*?</footer>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<header[^>]*>.*?</header>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        meaningful = [l for l in lines if len(l) > 60]
        return " ".join(meaningful)[:5000]
    except Exception:
        return ""


def extract_important_sentences(title: str, article_text: str, num_sentences: int = 8) -> str:
    if not article_text:
        return ""

    sentences = re.split(r'(?<=[.!?])\s+', article_text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 40]

    if not sentences:
        return ""

    title_words = set(w.lower() for w in re.findall(r'\w+', title) if len(w) > 2)

    def score(s: str, idx: int) -> float:
        s_lower = s.lower()
        score_val = 0.0
        for tw in title_words:
            if tw in s_lower:
                score_val += 2.0
        for kw in AI_KEYWORDS:
            if kw in s_lower:
                score_val += 1.5
        score_val += max(0, 5 - idx) * 0.5
        if len(s) > 150:
            score_val += 0.5
        if any(w in s_lower for w in ["according", "said", "announced", "released", "launched", "reported"]):
            score_val += 1.0
        return score_val

    scored = [(score(s, i), i, s) for i, s in enumerate(sentences)]
    scored.sort(key=lambda x: (-x[0], x[1]))

    selected = []
    seen_content = set()
    for _, idx, s in scored[:num_sentences * 2]:
        key = s[:50].lower()
        if key not in seen_content:
            seen_content.add(key)
            selected.append((idx, s))

    selected.sort(key=lambda x: x[0])
    selected = selected[:num_sentences]

    result = []
    for idx, s in selected:
        s_clean = re.sub(r'\s+', ' ', s).strip()
        s_clean = html.unescape(s_clean)
        result.append(s_clean)

    return "\n".join(result)


def get_article_summary(title: str, url: str) -> str:
    text = fetch_article_text(url)
    if not text:
        return ""
    summary = extract_important_sentences(title, text, 8)
    if summary and len(summary.split('\n')) >= 3:
        return summary
    return ""


def enrich_with_summaries(items: List[Dict]) -> List[Dict]:
    def process(item):
        summary = get_article_summary(item["title"], item["url"])
        if summary:
            item["summary"] = summary
        return item

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(process, items))
    return results


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
            "source": "Hacker News",
            "points": s.get("score", 0) or 0,
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
        results.sort(key=lambda x: x.get("points", 0), reverse=True)
        return results[:MAX_STORIES]
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
                        "source": "Hacker News",
                        "points": hit.get("points", 0) or 0,
                    })
        except Exception as e:
            print(f"[WARN] HN search '{q}' failed: {e}")
            continue
    results.sort(key=lambda x: x.get("points", 0), reverse=True)
    return results[:MAX_STORIES]


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
                        "source": "Dev.to",
                        "points": art.get("positive_reactions_count", 0) or 0,
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
    deduped.sort(key=lambda x: x.get("points", 0), reverse=True)
    return deduped[:MAX_STORIES]


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
            for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
                title = entry.findtext("{http://www.w3.org/2005/Atom}title", "")
                link_el = entry.find("{http://www.w3.org/2005/Atom}link")
                link = link_el.get("href", "") if link_el is not None else ""
                if keyword_score(title) >= 1:
                    results.append({"title": title, "url": link, "source": source_name, "points": 0})
            for item in root.iter("item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                if keyword_score(title) >= 1:
                    results.append({"title": title, "url": link, "source": source_name, "points": 0})
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
            },
            headers={"X-Api-Key": api_key},
            timeout=12,
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])
        return [
            {"title": a.get("title", ""), "url": a.get("url", ""), "source": a.get("source", {}).get("name", "News"), "points": 0}
            for a in articles if a.get("title") and keyword_score(a.get("title", "")) >= 1
        ]
    except Exception as e:
        print(f"[WARN] NewsAPI fetch failed: {e}")
        return []


MODEL_KEYWORDS = [
    "released", "launched", "announced", "unveiled", "new model", "new AI",
    "debuts", "introduces", "open source", "open-sourced", "weights",
    "8b", "27b", "70b", "120b", "405b", "transformer", "diffusion",
]


def is_model_news(title: str) -> bool:
    t = title.lower()
    has_ai = sum(1 for kw in AI_KEYWORDS if kw in t) >= 1
    has_model = sum(1 for mk in MODEL_KEYWORDS if mk in t) >= 1
    has_version = bool(re.search(r'\d+\.\d+\b', t))
    has_b_param = bool(re.search(r'\d+b\b', t))
    has_model_word = "model" in t or "models" in t
    if has_ai and (has_model or has_version or has_b_param or has_model_word):
        return True
    return False


def fetch_new_models() -> List[Dict]:
    queries = [
        "released AI model", "new AI model announced", "open source AI model",
        "launched LLM", "new GPT", "new Claude", "new Gemini",
    ]
    min_ts = int((datetime.now(timezone.utc) - timedelta(days=4)).timestamp())
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
                if key not in seen and is_model_news(title):
                    seen.add(key)
                    results.append({
                        "title": title,
                        "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                        "source": "Hacker News",
                        "points": (hit.get("points", 0) or 0) + 10,
                    })
        except Exception as e:
            print(f"[WARN] Models search '{q}' failed: {e}")
            continue
    results.sort(key=lambda x: x["points"], reverse=True)
    return results[:4]


def fetch_all_news() -> List[Dict]:
    seen = set()
    all_news = []

    fetchers = [fetch_new_models, fetch_hn_top, fetch_hn_search, fetch_devto, fetch_rss, fetch_newsapi]
    for fetcher in fetchers:
        for item in fetcher():
            key = item["title"].lower().strip()
            if key not in seen:
                seen.add(key)
                all_news.append(item)

    all_news.sort(key=lambda x: x.get("points", 0), reverse=True)
    all_news = all_news[:MAX_STORIES]

    model_count = sum(1 for n in all_news if is_model_news(n["title"]))
    if model_count < 2:
        extras = fetch_new_models()
        for ex in extras:
            if len(all_news) >= MAX_STORIES:
                break
            key = ex["title"].lower().strip()
            if key not in seen and ex not in all_news:
                seen.add(key)
                all_news.append(ex)
                model_count += 1

    print("Generating article summaries...")
    all_news = enrich_with_summaries(all_news)
    return all_news


def build_email_html(news: List[Dict]) -> str:
    date_str = datetime.now(IST).strftime("%B %d, %Y")
    items_html = ""
    for i, item in enumerate(news, 1):
        safe_title = html.escape(item["title"])
        safe_source = html.escape(item.get("source", ""))
        summary = item.get("summary", "")
        summary_html = ""
        if summary:
            for line in summary.split("\n"):
                if line.strip():
                    summary_html += f'<tr><td style="padding:2px 0;color:#374151;font-size:14px;line-height:1.65">{html.escape(line.strip())}</td></tr>\n'
        badge = f'<span style="background:#e5e7eb;color:#374151;font-size:12px;padding:2px 8px;border-radius:4px">{safe_source}</span>'
        no_summary_badge = '<span style="color:#9ca3af;font-size:13px;font-style:italic">Summary not available</span>' if not summary else ""
        items_html += f"""
        <tr>
            <td style="padding:18px 24px;border-bottom:1px solid #e5e7eb">
                <div style="margin-bottom:6px">
                    <span style="color:#6366f1;font-weight:700;font-size:15px">{i}.</span>
                    <span style="color:#111827;font-size:16px;font-weight:600">{safe_title}</span>
                    <span style="margin-left:8px">{badge}</span>
                </div>
                <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;padding-left:22px;border-left:3px solid #e0e7ff">
                {summary_html}
                </table>
                {no_summary_badge}
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
<h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;letter-spacing:-0.3px">Daily AI Brief</h1>
<p style="margin:4px 0 0;color:rgba(255,255,255,0.8);font-size:14px">{date_str}</p>
</td>
</tr>
<tr><td style="padding:6px 24px;background:#f0f9ff;font-size:13px;color:#1e40af;text-align:center;border-bottom:1px solid #e0f2fe">
Top {len(news)} AI stories — summarized for you
</td></tr>
<tr><td style="padding:0">
<table width="100%" cellpadding="0" cellspacing="0">
{items_html}
</table>
</td></tr>
<tr><td style="padding:18px 32px;background:#f8fafc;text-align:center;font-size:12px;color:#94a3b8;border-top:1px solid #e2e8f0">
Generated {datetime.now(IST).strftime("%I:%M %p %Z")} &bull; Concise AI daily digest
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
        plain += f"{i}. [{item['source']}] {item['title']}\n"
        summary = item.get("summary", "")
        if summary:
            for line in summary.split("\n"):
                if line.strip():
                    plain += f"   {line.strip()}\n"
        else:
            plain += "   [Summary not available]\n"
        plain += "\n"
    plain += f"Generated at {datetime.now(IST).strftime('%I:%M %p %Z')}"

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

    print(f"\nSummarized {len(news)} stories:")
    for i, n in enumerate(news, 1):
        print(f"\n  {i}. [{n['source']}] {n['title']}")
        summary = n.get("summary", "")
        if summary:
            for line in summary.split("\n"):
                print(f"     {line.strip()}")

    if not os.environ.get("GMAIL_USER") or not os.environ.get("GMAIL_APP_PASSWORD"):
        print("\nSet GMAIL_USER and GMAIL_APP_PASSWORD env vars to send email.")
        return

    send_email(news)
    print("Done!")


if __name__ == "__main__":
    main()
