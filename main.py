"""
OptiBot Mini-Clone — orchestrator.

Scrapes Zendesk articles, detects changes via SHA-256, and uploads only
deltas to the OpenAI Vector Store. Three paths:
  1. First run (no state) → upload everything
  2. No changes → skip, exit early
  3. Changes detected → upload only added + updated files

Usage: python main.py
Requires: OPENAI_API_KEY in environment or .env file.
"""

import sys
from pathlib import Path
from datetime import datetime

from scraper import scrape, load_state
from uploader import upload_to_vector_store, upload_delta_to_vector_store


def main():
    print("=" * 60)
    print("  OptiBot Mini-Clone")
    print(f"  {datetime.now().isoformat()}")
    print("=" * 60)
    
    # ── Scrape ──────────────────────────────────────────────────
    print("\n[1/2] Scraping Zendesk articles...")
    
    old_state = load_state()
    scrape_result = scrape(do_change_detection=True)
    
    added = scrape_result["added"]
    updated = scrape_result["updated"]
    skipped = scrape_result["skipped"]
    
    # ── Upload ──────────────────────────────────────────────────
    print("\n[2/2] Uploading to OpenAI...")
    
    try:
        if not old_state:
            # Path 1: First run — no previous state, upload everything
            print("  First run — uploading all articles")
            result = upload_to_vector_store()
        elif added == 0 and updated == 0:
            # Path 2: Nothing changed — skip
            print("  No changes — skipping upload")
            print("=" * 60)
            print(f"  Done at {datetime.now().isoformat()}")
            return
        else:
            # Path 3: Delta — only upload new + updated files
            delta_filenames = []
            articles = scrape_result.get("articles", {})
            for aid, info in articles.items():
                if aid not in old_state or old_state[aid]["hash"] != info["hash"]:
                    delta_filenames.append(info["filename"])
            
            print(f"  Uploading {len(delta_filenames)} changed file(s)")
            result = upload_delta_to_vector_store(
                articles_dir=Path(__file__).parent / "articles",
                delta_filenames=delta_filenames,
            )
        
        # ── Done ────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("  Pipeline complete")
        print(f"  {datetime.now().isoformat()}")
        print("=" * 60)
        print(f"  Scrape: {added} added, {updated} updated, {skipped} skipped")
        print(f"  Upload: {result['files_uploaded']} files sent to OpenAI")
        print(f"  Vector Store: {result['vector_store_id']}")
        print(f"  Assistant:    {result['assistant_id']}")
        
    except ValueError as e:
        print(f"\nError: {e}")
        print("Set OPENAI_API_KEY in your environment or .env file.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Load .env file if it exists
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    
    main()
