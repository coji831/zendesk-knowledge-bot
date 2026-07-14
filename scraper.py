"""
Scrape Zendesk Help Center → clean Markdown.

Uses the Zendesk API to discover article URLs, then scrapes the actual HTML
pages (not the API body) to prove we can handle messy web content. Strips
nav, sidebar, ads, and other noise via CSS selectors, then converts to
Markdown with preserved links, headings, and code blocks.

Each article is SHA-256 hashed for delta detection — only changed articles
get re-uploaded by the daily cron job.
"""

import json
import hashlib
import re
import tempfile
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import html2text

# ─── Configuration ───────────────────────────────────────────────
ZENDESK_DOMAIN = "https://support.optisigns.com"
ARTICLES_API = f"{ZENDESK_DOMAIN}/api/v2/help_center/articles.json"
OUTPUT_DIR = Path(__file__).parent / "articles"
STATE_FILE = OUTPUT_DIR / "scrape_state.json"
PER_PAGE = 30
MIN_ARTICLES = 30

# CSS selectors for elements to strip from scraped pages.
# Tweak this list when scraping a different site.
NOISE_SELECTORS = [
    'nav', 'header', 'footer', 'aside',
    '[role="navigation"]', '[role="banner"]',
    '[role="contentinfo"]', '[role="complementary"]',
    '.breadcrumbs', '.article-meta', '.article-sidebar',
    '.article-votes', '.article-comments', '.article-subscribe',
    '.recent-articles', '.related-articles', '.promoted-articles',
    '.search', '.cookie-banner', '.social-share',
    'script', 'style', 'noscript',
]


def discover_articles(target_count: int = MIN_ARTICLES) -> list[dict]:
    """Discover article URLs via the Zendesk API — discovery only, not content."""
    articles = []
    url = f"{ARTICLES_API}?per_page={PER_PAGE}"
    
    print(f"Discovering articles via API...")
    
    while url and len(articles) < target_count:
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  API error: {e}")
            break
        
        for a in data.get("articles", []):
            articles.append({
                "id": a["id"],
                "title": a["title"],
                "html_url": a["html_url"],
                "updated_at": a["updated_at"],
            })
        
        url = data.get("next_page")
    
    print(f"  Found {len(articles)} articles")
    return articles


def scrape_article_html(html_url: str) -> str:
    """Fetch a messy HTML page and return clean Markdown.
    
    Finds the article content container, strips noise elements, and converts
    to Markdown. The fallback chain (.article-body → article → main → body)
    makes this resilient to Zendesk template changes.
    """
    resp = requests.get(html_url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    # Extract title
    title = None
    for selector in ['.article-header h1', 'h1']:
        el = soup.select_one(selector)
        if el:
            title = el.get_text().strip()
            break
    if not title:
        title_tag = soup.find('title')
        title = title_tag.get_text().split(' – ')[0].strip() if title_tag else "Untitled"
    
    # Find content container with graceful fallback
    body = None
    for selector in ['.article-body', 'article', 'main']:
        el = soup.select_one(selector)
        if el:
            body = el
            break
    if not body:
        body = soup.find('body')
    
    # Strip noise: re-parse the body fragment so decompose() works cleanly
    body_soup = BeautifulSoup(str(body), 'html.parser')
    for selector in NOISE_SELECTORS:
        for element in body_soup.select(selector):
            element.decompose()
    
    # Convert to Markdown — preserve links for citations, code blocks, headings
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_links = False
    h.ignore_images = False
    h.mark_code = True
    
    md = h.handle(str(body_soup))
    md = re.sub(r'\n{3,}', '\n\n', md).strip()
    return md


def validate_extraction(scraped_md: str, article_id: int) -> bool:
    """Check scraped content against the API body to catch over-stripping.
    
    Compares word overlap between our scraped Markdown and Zendesk's clean
    API body. Returns True if >50% of meaningful words match.
    """
    try:
        api_url = f"{ZENDESK_DOMAIN}/api/v2/help_center/articles/{article_id}.json"
        api_data = requests.get(api_url, timeout=10).json()
        api_body = api_data['article']['body']
        
        api_words = set(re.findall(r'\b[a-zA-Z]{5,}\b', api_body.lower()))
        scraped_words = set(re.findall(r'\b[a-zA-Z]{5,}\b', scraped_md.lower()))
        
        if not api_words:
            return True
        
        overlap = api_words & scraped_words
        return len(overlap) / len(api_words) > 0.5
    except Exception:
        return True


def title_to_slug(title: str) -> str:
    """Convert 'How to Add a YouTube Video' → 'how-to-add-a-youtube-video'."""
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')


def save_article(article_meta: dict, markdown_body: str, output_dir: Path) -> dict:
    """Save article as .md with YAML frontmatter and return its state entry.
    
    The returned dict (id, filename, hash, updated_at) is collected into
    new_state for delta detection — comparing hashes between runs.
    """
    article_id = article_meta["id"]
    title = article_meta["title"]
    html_url = article_meta["html_url"]
    updated_at = article_meta.get("updated_at", "")
    
    slug = title_to_slug(title)
    filename = f"{article_id}-{slug}.md"
    filepath = output_dir / filename
    
    content_hash = hashlib.sha256(markdown_body.encode()).hexdigest()
    
    md_content = f"""---
id: {article_id}
title: "{title}"
source_url: {html_url}
updated_at: {updated_at}
scraped_at: {datetime.now().isoformat()}
extraction_method: html_scraping
---

# {title}

> Source: [{html_url}]({html_url})

{markdown_body}
"""
    filepath.write_text(md_content, encoding="utf-8")
    
    return {
        "id": article_id,
        "filename": filename,
        "hash": content_hash,
        "updated_at": updated_at,
    }


def load_state() -> dict:
    """Load previous run's article hashes. Returns {} on first run or corruption."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  State file corrupted ({e}) — starting fresh")
            return {}
    return {}


def save_state(state: dict):
    """Write state atomically: temp file first, then rename.
    
    If the disk fills mid-write, the real state file stays intact.
    """
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def detect_changes(new_state: dict, old_state: dict):
    """Compare article hashes to find what changed.
    
    Returns three lists: added, updated, skipped.
    - ID not in old_state → added
    - ID present but hash differs → updated
    - ID present and hash matches → skipped
    """
    added, updated, skipped = [], [], []
    
    for aid, info in new_state.items():
        if aid not in old_state:
            added.append(info)
        elif old_state[aid]["hash"] != info["hash"]:
            updated.append(info)
        else:
            skipped.append(info)
    
    return added, updated, skipped


def scrape(do_change_detection: bool = True) -> dict:
    """Run the full ETL pipeline: discover → scrape → validate → save → diff.
    
    Called by main.py. Returns counts: total_discovered, total_saved,
    added, updated, skipped, plus the new_state dict.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    old_state = load_state() if do_change_detection else {}
    
    articles = discover_articles()
    if len(articles) < MIN_ARTICLES:
        print(f"  Only {len(articles)} articles found (target: {MIN_ARTICLES})")
    
    # Build new_state from scratch — failed articles get no entry,
    # so the next successful scrape treats them as "added" (self-healing).
    new_state = {}
    saved, failed = 0, 0
    
    for i, article in enumerate(articles):
        try:
            print(f"  [{i+1}/{len(articles)}] {article['title'][:70]}")
            markdown = scrape_article_html(article["html_url"])
            
            if not validate_extraction(markdown, article["id"]):
                print(f"     Low word overlap with API — possible over-stripping")
            
            state_entry = save_article(article, markdown, OUTPUT_DIR)
            new_state[str(state_entry["id"])] = state_entry
            saved += 1
        except Exception as e:
            print(f"     Failed: {e}")
            failed += 1
    
    save_state(new_state)
    added, updated, skipped = detect_changes(new_state, old_state)
    
    result = {
        "total_discovered": len(articles),
        "total_saved": saved,
        "total_failed": failed,
        "added": len(added),
        "updated": len(updated),
        "skipped": len(skipped),
        "articles": new_state,
    }
    
    print(f"\n  Discovered: {result['total_discovered']}  |  "
          f"Saved: {saved}  |  Failed: {failed}")
    print(f"  Added: {result['added']}  |  "
          f"Updated: {result['updated']}  |  "
          f"Skipped: {result['skipped']}")
    
    return result


if __name__ == "__main__":
    scrape()
