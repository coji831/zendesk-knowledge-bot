"""
Upload scraped Markdown files to OpenAI Vector Store + create OptiBot assistant.

Flow: upload files → create/reuse vector store → attach (triggers chunking +
embedding) → poll for completion → create/reuse assistant with system prompt.
All via API — no UI drag-and-drop.
"""

import os
import time
from pathlib import Path
from openai import OpenAI

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Configuration ───────────────────────────────────────────────
ARTICLES_DIR = Path(__file__).parent / "articles"

# Verbatim from the assignment
SYSTEM_PROMPT = """You are OptiBot, the customer-support bot for OptiSigns.com.
• Tone: helpful, factual, concise.
• Only answer using the uploaded docs.
• Max 5 bullet points; else link to the doc.
• Cite up to 3 "Article URL:" lines per reply."""

ASSISTANT_NAME = "OptiBot"
VECTOR_STORE_NAME = "OptiSigns Support Docs"

# Chunking: OpenAI's built-in recursive text splitter (~800 tokens/chunk,
# ~400 overlap, respects Markdown heading boundaries). Explained in README.


def get_client() -> OpenAI:
    """Initialize OpenAI client. Reads OPENAI_API_KEY from environment."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY not set.\n"
            "  PowerShell: $env:OPENAI_API_KEY='sk-...'\n"
            "  Or create a .env file with OPENAI_API_KEY=sk-..."
        )
    return OpenAI(api_key=api_key)


def upload_files(client: OpenAI, articles_dir: Path) -> list:
    """Upload all .md files to OpenAI. Returns list of (file_id, filename)."""
    md_files = sorted(articles_dir.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"No .md files in {articles_dir}")
    
    print(f"Uploading {len(md_files)} files to OpenAI...")
    
    uploaded = []
    for i, filepath in enumerate(md_files):
        try:
            with open(filepath, "rb") as f:
                file_obj = client.files.create(file=f, purpose="assistants")
            uploaded.append((file_obj.id, filepath.name))
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(md_files)}...")
        except Exception as e:
            print(f"  Error uploading {filepath.name}: {e}")
    
    print(f"  {len(uploaded)}/{len(md_files)} uploaded")
    return uploaded


def create_or_get_vector_store(client: OpenAI) -> str:
    """Get existing vector store by name, or create a new one. Returns ID."""
    for vs in client.vector_stores.list().data:
        if vs.name == VECTOR_STORE_NAME:
            print(f"Using existing Vector Store: {vs.id}")
            return vs.id
    
    vs = client.vector_stores.create(name=VECTOR_STORE_NAME)
    print(f"Created Vector Store: {vs.id}")
    return vs.id


def attach_files_to_store(client: OpenAI, vector_store_id: str, 
                          uploaded_files: list[tuple[str, str]]):
    """Attach files to vector store and poll until embedding completes.
    
    OpenAI handles chunking → embedding → indexing asynchronously.
    We poll every 2 seconds until the batch status is "completed" or "failed".
    """
    file_ids = [fid for fid, _ in uploaded_files]
    print(f"Attaching {len(file_ids)} files to Vector Store...")
    
    batch = client.vector_stores.file_batches.create(
        vector_store_id=vector_store_id,
        file_ids=file_ids
    )
    
    print("  Waiting for embedding...", end="", flush=True)
    while True:
        status = client.vector_stores.file_batches.retrieve(
            vector_store_id=vector_store_id,
            batch_id=batch.id
        )
        if status.status == "completed":
            print(" done")
            break
        elif status.status == "failed":
            print(f" failed ({status.file_counts.failed} files)")
            break
        print(".", end="", flush=True)
        time.sleep(2)
    
    return batch


def create_or_get_assistant(client: OpenAI, vector_store_id: str) -> str:
    """Get existing OptiBot assistant by name, or create one.
    
    Uses gpt-4o-mini (200K TPM on Tier 1) instead of gpt-4o (10K TPM) —
    we hit the rate limit with 30 articles on first attempt.
    Temperature 0.3 keeps answers factual for support use.
    """
    for asst in client.beta.assistants.list().data:
        if asst.name == ASSISTANT_NAME:
            print(f"Using existing Assistant: {asst.id}")
            client.beta.assistants.update(
                assistant_id=asst.id,
                tools=[{"type": "file_search"}],
                tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
                temperature=0.3,
            )
            return asst.id
    
    assistant = client.beta.assistants.create(
        name=ASSISTANT_NAME,
        instructions=SYSTEM_PROMPT,
        model="gpt-4o-mini",
        tools=[{"type": "file_search"}],
        tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
        temperature=0.3,
    )
    print(f"Created Assistant: {assistant.id}")
    return assistant.id


# ─── Test Query ──────────────────────────────────────────────────
def test_assistant(client: OpenAI, assistant_id: str):
    """
    Test the assistant with the required sanity check question.
    """
    test_question = "How do I add a YouTube video?"
    
    print(f"\n🧪 Testing Assistant with: \"{test_question}\"")
    print("-" * 50)
    
    # Create a thread
    thread = client.beta.threads.create()
    
    # Add the user question
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=test_question,
    )
    
    # Run the assistant
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant_id,
    )
    
    # Wait for completion
    print("   Thinking...", end="")
    while True:
        run_status = client.beta.threads.runs.retrieve(
            thread_id=thread.id,
            run_id=run.id,
        )
        if run_status.status == "completed":
            print(" ✅\n")
            break
        elif run_status.status in ("failed", "cancelled", "expired"):
            print(f" ❌ (status: {run_status.status})\n")
            return
        else:
            print(".", end="", flush=True)
            time.sleep(1)
    
    # Get the assistant's response
    messages = client.beta.threads.messages.list(thread_id=thread.id)
    
    # Print the assistant's answer (first message is the latest = assistant reply)
    for msg in messages.data:
        if msg.role == "assistant":
            for content_block in msg.content:
                if content_block.type == "text":
                    print(content_block.text.value)
                    
                    # Also check for annotations (citations)
                    annotations = content_block.text.annotations
                    if annotations:
                        print("\n📎 Citations found:")
                        for ann in annotations:
                            if hasattr(ann, 'file_citation'):
                                print(f"   → Cited file: {ann.file_citation.file_id}")
            break
    
    print("-" * 50)
    
    return thread.id


def upload_delta_files(client: OpenAI, articles_dir: Path,
                       delta_filenames: list[str]) -> list:
    """Upload only the files that changed (added or updated).
    
    The key optimization for the daily cron job — instead of re-uploading
    all 30+ articles every day, only the delta goes to OpenAI.
    """
    if not delta_filenames:
        return []
    
    print(f"Uploading {len(delta_filenames)} delta file(s)...")
    
    uploaded = []
    for filename in delta_filenames:
        filepath = articles_dir / filename
        if not filepath.exists():
            print(f"  Missing: {filename} — skipping")
            continue
        try:
            with open(filepath, "rb") as f:
                file_obj = client.files.create(file=f, purpose="assistants")
            uploaded.append((file_obj.id, filename))
        except Exception as e:
            print(f"  Error uploading {filename}: {e}")
    
    print(f"  {len(uploaded)}/{len(delta_filenames)} uploaded")
    return uploaded


def upload_to_vector_store(articles_dir: Path = ARTICLES_DIR) -> dict:
    """Upload ALL articles and create assistant. For first run or full rebuild."""
    client = get_client()
    uploaded_files = upload_files(client, articles_dir)
    
    if not uploaded_files:
        raise RuntimeError("No files uploaded")
    
    vector_store_id = create_or_get_vector_store(client)
    batch = attach_files_to_store(client, vector_store_id, uploaded_files)
    assistant_id = create_or_get_assistant(client, vector_store_id)
    
    print(f"\nUpload complete — {len(uploaded_files)} files")
    print(f"  Vector Store: {vector_store_id}")
    print(f"  Assistant:    {assistant_id}")
    
    return {
        "vector_store_id": vector_store_id,
        "assistant_id": assistant_id,
        "files_uploaded": len(uploaded_files),
        "batch_id": batch.id,
    }


def upload_delta_to_vector_store(articles_dir: Path,
                                  delta_filenames: list[str]) -> dict:
    """Upload only changed files to the Vector Store. Daily cron entry point."""
    client = get_client()
    uploaded_files = upload_delta_files(client, articles_dir, delta_filenames)
    
    if not uploaded_files:
        print("Nothing to upload.")
        vs_id = create_or_get_vector_store(client)
        asst_id = create_or_get_assistant(client, vs_id)
        return {
            "vector_store_id": vs_id,
            "assistant_id": asst_id,
            "files_uploaded": 0,
            "batch_id": None,
        }
    
    vector_store_id = create_or_get_vector_store(client)
    batch = attach_files_to_store(client, vector_store_id, uploaded_files)
    assistant_id = create_or_get_assistant(client, vector_store_id)
    
    print(f"\nDelta upload complete — {len(uploaded_files)} files")
    print(f"  Vector Store: {vector_store_id}")
    print(f"  Assistant:    {assistant_id}")
    
    return {
        "vector_store_id": vector_store_id,
        "assistant_id": assistant_id,
        "files_uploaded": len(uploaded_files),
        "batch_id": batch.id,
    }


if __name__ == "__main__":
    import sys
    result = upload_to_vector_store()
    if "--test" in sys.argv:
        client = get_client()
        test_assistant(client, result["assistant_id"])
    
    result = upload_to_vector_store()
    
    # Optionally run the test query
    if "--test" in sys.argv:
        client = get_client()
        test_assistant(client, result["assistant_id"])
