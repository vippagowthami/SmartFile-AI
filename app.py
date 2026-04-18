from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import time
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import streamlit as st
from chromadb import PersistentClient
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
from docx import Document
from google import genai
from google.genai import types
from dotenv import load_dotenv


load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)


APP_TITLE = "SmartFile-AI"
APP_SUBTITLE = "Ask questions about uploaded documents or use Gemini for general answers."
CHROMA_PATH = Path("chroma_db")
CHAT_HISTORY_PATH = Path("chat_history.json")
COLLECTION_NAME = "smart_file_documents"
CHAT_MODEL = "gemini-2.5-flash"
EMBED_MODEL = "gemini-embedding-001"
SIMILARITY_THRESHOLD = 0.55
MAX_UPLOADS = 5
EMBED_BATCH_SIZE = 16
GEMINI_RETRY_ATTEMPTS = 3
DOC_INTENT_KEYWORDS = {
    "file",
    "files",
    "document",
    "documents",
    "doc",
    "pdf",
    "txt",
    "uploaded",
    "upload",
    "report",
    "policy",
    "rules",
    "from the file",
    "from file",
    "in the file",
    "in the document",
}
TEXT_SPLITTER = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=200)


def get_api_key() -> str | None:
    return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")


@st.cache_resource(show_spinner=False)
def get_gemini_client() -> genai.Client | None:
    api_key = get_api_key()
    if not api_key:
        return None
    return genai.Client(api_key=api_key)


@st.cache_resource(show_spinner=False)
def get_chroma_collection():
    CHROMA_PATH.mkdir(exist_ok=True)
    client = PersistentClient(path=str(CHROMA_PATH))
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def initialize_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = load_chat_history()
    st.session_state.setdefault("indexed_files", set())


def clear_chat_history() -> None:
    st.session_state.messages = []
    save_chat_history()


def append_message(role: str, content: str) -> None:
    st.session_state.messages.append({"role": role, "content": content})
    save_chat_history()


def load_chat_history() -> list[dict[str, str]]:
    if not CHAT_HISTORY_PATH.exists():
        return []

    try:
        data = json.loads(CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []

        messages: list[dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "assistant"))
            content = str(item.get("content", ""))
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        return messages
    except Exception:
        return []


def save_chat_history() -> None:
    CHAT_HISTORY_PATH.write_text(chat_history_as_json(), encoding="utf-8")


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_resource_exhausted_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "resource_exhausted" in message or "429" in message or "resource exhausted" in message


def run_with_retry(operation, *, fallback_message: str | None = None):
    last_error: Exception | None = None
    for attempt in range(GEMINI_RETRY_ATTEMPTS):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if not is_resource_exhausted_error(exc) or attempt == GEMINI_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(2**attempt)

    if fallback_message is not None:
        return fallback_message
    if last_error is not None:
        raise last_error
    raise RuntimeError("Gemini operation failed unexpectedly.")


def extract_text_from_pdf(file_path: Path) -> str:
    reader = PdfReader(str(file_path))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return clean_text("\n".join(pages))


def extract_text_from_txt(file_path: Path) -> str:
    return clean_text(file_path.read_text(encoding="utf-8", errors="ignore"))


def extract_text_from_docx(file_path: Path) -> str:
    document = Document(str(file_path))
    paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    return clean_text("\n".join(paragraphs))


def extract_text_from_doc(file_path: Path) -> str:
    pythoncom = None
    word = None
    try:
        pythoncom = importlib.import_module("pythoncom")
        win32_client = importlib.import_module("win32com.client")

        pythoncom.CoInitialize()
        word = win32_client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        document = word.Documents.Open(str(file_path), ReadOnly=1)
        text = clean_text(document.Content.Text)
        document.Close(False)
        return text
    except Exception as exc:
        raise RuntimeError(
            "Could not extract text from .doc file in this environment. "
            "Install Microsoft Word or convert the file to .docx/.pdf/.txt."
        ) from exc
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def extract_text_from_file(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(file_path)
    if suffix == ".txt":
        return extract_text_from_txt(file_path)
    if suffix == ".docx":
        return extract_text_from_docx(file_path)
    if suffix == ".doc":
        return extract_text_from_doc(file_path)
    raise ValueError(f"Unsupported file type: {suffix}")


def split_text(text: str) -> list[str]:
    chunks = TEXT_SPLITTER.split_text(text)
    return [chunk for chunk in (clean_text(chunk) for chunk in chunks) if chunk]


def embed_texts(client: genai.Client, texts: list[str]) -> list[list[float]]:
    embeddings: list[list[float]] = []
    for start_index in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start_index : start_index + EMBED_BATCH_SIZE]

        def embed_batch() -> list[list[float]]:
            response = client.models.embed_content(model=EMBED_MODEL, contents=batch)
            if getattr(response, "embeddings", None):
                return [list(embedding.values) for embedding in response.embeddings]
            if getattr(response, "embedding", None):
                return [list(response.embedding.values)]
            raise RuntimeError("Gemini embedding response did not include vectors.")

        embeddings.extend(run_with_retry(embed_batch))

    return embeddings


def ingest_uploaded_file(client: genai.Client, collection, file_path: Path, original_name: str) -> int:
    text = extract_text_from_file(file_path)
    if not text:
        return 0

    chunks = split_text(text)
    if not chunks:
        return 0

    embeddings = embed_texts(client, chunks)
    signature = file_hash(file_path.read_bytes())[:16]

    ids = []
    documents = []
    metadatas = []
    for index, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
        ids.append(f"{signature}-{index}")
        documents.append(chunk)
        metadatas.append(
            {
                "source": original_name,
                "chunk_index": index,
                "file_signature": signature,
                "file_type": file_path.suffix.lower().lstrip("."),
            }
        )

    collection.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
    return len(chunks)


def get_unique_sources(collection) -> list[str]:
    data = collection.get(include=["metadatas"])
    sources = sorted({metadata.get("source", "Unknown") for metadata in data.get("metadatas", []) if metadata})
    return sources


def query_relevant_chunks(client: genai.Client, collection, question: str, top_k: int = 4) -> list[dict[str, Any]]:
    if collection.count() == 0:
        return []

    query_embedding = embed_texts(client, [question])[0]
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    chunks: list[dict[str, Any]] = []
    for document, metadata, distance in zip(documents, metadatas, distances, strict=False):
        similarity = 1 - float(distance)
        chunks.append(
            {
                "text": document,
                "source": metadata.get("source", "Unknown") if metadata else "Unknown",
                "chunk_index": metadata.get("chunk_index", -1) if metadata else -1,
                "similarity": similarity,
            }
        )
    return chunks


def classify_query(client: genai.Client, question: str, sources: list[str]) -> str:
    prompt = f"""Classify the user's question.
Uploaded files: {', '.join(sources) if sources else 'None'}
Question: {question}

Return JSON with:
- mode: "document" if the question should be answered from the uploaded files, otherwise "general"
- reason: short explanation
"""

    response = run_with_retry(
        lambda: client.models.generate_content(
            model=CHAT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=128,
                response_mime_type="application/json",
                response_schema={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["document", "general"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["mode", "reason"],
                },
            ),
        )
    )

    try:
        parsed = json.loads(response.text or "{}")
        return str(parsed.get("mode", "general"))
    except json.JSONDecodeError:
        return "general"


def is_likely_document_question(question: str, sources: list[str]) -> bool:
    lower_question = question.lower()
    if any(keyword in lower_question for keyword in DOC_INTENT_KEYWORDS):
        return True

    for source in sources:
        source_name = source.lower()
        source_stem = Path(source_name).stem
        if source_name and source_name in lower_question:
            return True
        if source_stem and source_stem in lower_question:
            return True

    return False


def generate_document_answer(client: genai.Client, question: str, chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "I could not find this information in the uploaded files."

    context_lines = []
    for chunk in chunks[:3]:
        context_lines.append(
            f"Source: {chunk['source']} | Chunk: {chunk['chunk_index']} | Similarity: {chunk['similarity']:.2f}\n{chunk['text']}"
        )

    prompt = f"""Answer the question using only the document excerpts below.
If the excerpts do not contain the answer, reply with exactly: I could not find this information in the uploaded files.

Question: {question}

Document excerpts:
{chr(10).join(context_lines)}
"""

    response = run_with_retry(
        lambda: client.models.generate_content(
            model=CHAT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=512,
            ),
        ),
        fallback_message="I could not find this information in the uploaded files.",
    )

    answer = (response.text or "").strip()
    if not answer:
        return "I could not find this information in the uploaded files."

    if answer == "I could not find this information in the uploaded files.":
        return answer

    sources = []
    for chunk in chunks:
        source = chunk["source"]
        if source not in sources:
            sources.append(source)

    if "(Source:" not in answer and sources:
        answer = f"{answer} (Source: {', '.join(sources)})"

    return answer


def generate_general_answer(client: genai.Client, question: str) -> str:
    prompt = f"""You are a helpful general-purpose assistant.
Answer the user's question clearly and accurately.
Do not mention uploaded files unless the user asks about them.

Question: {question}
"""

    response = run_with_retry(
        lambda: client.models.generate_content(
            model=CHAT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.4,
                max_output_tokens=512,
            ),
        ),
        fallback_message="I could not generate a response right now.",
    )

    answer = (response.text or "").strip()
    return answer or "I could not generate a response right now."


def route_question(client: genai.Client, collection, question: str, sources: list[str]) -> str:
    chunks = query_relevant_chunks(client, collection, question)

    highest_similarity = chunks[0]["similarity"] if chunks else 0.0
    if chunks and highest_similarity >= SIMILARITY_THRESHOLD:
        return generate_document_answer(client, question, chunks)

    if sources:
        heuristic_document_mode = is_likely_document_question(question, sources)
        if heuristic_document_mode:
            if chunks and highest_similarity >= SIMILARITY_THRESHOLD:
                return generate_document_answer(client, question, chunks)
            return "I could not find this information in the uploaded files."

    return generate_general_answer(client, question)


def render_chat() -> None:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])


def chat_history_as_json() -> str:
    return json.dumps(st.session_state.messages, indent=2, ensure_ascii=False)


def chat_history_as_text() -> str:
    lines: list[str] = []
    for message in st.session_state.messages:
        role = str(message.get("role", "assistant")).upper()
        content = str(message.get("content", ""))
        lines.append(f"{role}:\n{content}\n")
    return "\n".join(lines).strip() + "\n"


def render_hero(indexed_file_count: int, chunk_count: int) -> None:
    st.markdown(
        f"""
        <section class="hero-shell">
            <div class="hero-card">
                <p class="hero-kicker">Smart Retrieval Workspace</p>
                <h1 class="hero-title">{APP_TITLE}</h1>
                <p class="hero-subtitle">{APP_SUBTITLE}</p>
                <div class="hero-metrics">
                    <div class="metric-pill">
                        <span class="metric-label">Indexed Files</span>
                        <span class="metric-value">{indexed_file_count}</span>
                    </div>
                    <div class="metric-pill">
                        <span class="metric-label">Stored Chunks</span>
                        <span class="metric-value">{chunk_count}</span>
                    </div>
                    <div class="metric-pill">
                        <span class="metric-label">Mode</span>
                        <span class="metric-value">Doc + General AI</span>
                    </div>
                </div>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="📄",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    initialize_state()

    client = get_gemini_client()
    collection = get_chroma_collection()

    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=DM+Serif+Display:ital@0;1&display=swap');

        :root,
        :root[data-theme="light"],
        :root[data-user-theme="light"] {
            color-scheme: light;
            --bg-1: #ffffff;
            --bg-2: #ffffff;
            --ink-1: #111111;
            --ink-2: #2f2f2f;
            --ink-soft: #5f5f5f;
            --accent: #0f766e;
            --accent-strong: #0d5f59;
            --accent-2: #ea580c;
            --card: rgba(255, 255, 255, 0.98);
            --card-border: rgba(17, 17, 17, 0.14);
            --hero-shadow: 0 16px 36px rgba(0, 0, 0, 0.08);
            --surface: rgba(255, 255, 255, 0.98);
            --surface-2: rgba(255, 255, 255, 0.94);
            --sidebar-bg: #ffffff;
        }

        :root[data-user-theme="dark"],
        :root[data-theme="dark"] {
            color-scheme: dark;
            --bg-1: #000000;
            --bg-2: #000000;
            --ink-1: #ffffff;
            --ink-2: #e6e6e6;
            --ink-soft: #cfcfcf;
            --accent-strong: #14b8a6;
            --accent-2: #fb923c;
            --card: rgba(8, 8, 8, 0.95);
            --card-border: rgba(255, 255, 255, 0.14);
            --hero-shadow: 0 18px 44px rgba(0, 0, 0, 0.7);
            --surface: rgba(0, 0, 0, 0.95);
            --surface-2: rgba(0, 0, 0, 0.88);
            --sidebar-bg: #000000;
        }

        html, body,
        [data-testid="stAppViewContainer"],
        [data-testid="stHeader"],
            background: var(--bg-1) !important;
            color: var(--ink-1) !important;
        }

        :root[data-user-theme="light"] [data-testid="stAppViewContainer"],
        :root[data-theme="light"] [data-testid="stAppViewContainer"],
        :root[data-user-theme="light"] [data-testid="stHeader"],
        :root[data-theme="light"] [data-testid="stHeader"],
        :root[data-user-theme="light"] [data-testid="stToolbar"],
        :root[data-theme="light"] [data-testid="stToolbar"] {
            background: #ffffff !important;
        }

        :root[data-user-theme="dark"] [data-testid="stAppViewContainer"],
        :root[data-theme="dark"] [data-testid="stAppViewContainer"],
        :root[data-user-theme="dark"] [data-testid="stHeader"],
        :root[data-theme="dark"] [data-testid="stHeader"],
        :root[data-user-theme="dark"] [data-testid="stToolbar"],
        :root[data-theme="dark"] [data-testid="stToolbar"] {
            background: #000000 !important;
        }

        .stApp {
            font-family: "Space Grotesk", "Segoe UI", sans-serif;
            color: var(--ink-1);
            background: var(--bg-1);
        }

        .block-container {
            padding-top: 1.4rem;
            padding-bottom: 2.4rem;
            max-width: 1060px;
        }

        h1, h2, h3, h4, p, li, label, span, div {
            color: var(--ink-1);
        }

        p, li {
            color: var(--ink-2);
            line-height: 1.52;
        }

        [data-testid="stMarkdownContainer"],
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li,
        [data-testid="stMarkdownContainer"] span,
        [data-testid="stCaptionContainer"],
        [data-testid="stCaptionContainer"] p,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] li,
        [data-testid="stSidebar"] span,
        [data-testid="stSidebar"] label,
        .stFileUploader label,
        .stFileUploader p {
            color: var(--ink-1) !important;
        }

        section.hero-shell {
            margin: 0.18rem 0 1.05rem 0;
            animation: fadeUp 500ms ease;
        }

        .hero-card {
            background: linear-gradient(135deg, var(--surface), var(--surface-2));
            border: 1px solid var(--card-border);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 1.25rem 1.25rem 1.05rem 1.25rem;
            box-shadow: var(--hero-shadow);
            text-align: left;
        }

        .hero-kicker {
            margin: 0;
            font-size: 0.8rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: var(--accent);
            font-weight: 700;
        }

        .hero-title {
            margin: 0.26rem 0 0.34rem 0;
            font-family: "DM Serif Display", Georgia, serif;
            font-size: clamp(1.8rem, 3.2vw, 2.4rem);
            line-height: 1.08;
            color: var(--ink-1);
            text-wrap: balance;
        }

        .hero-subtitle {
            margin: 0;
            color: var(--ink-soft);
            font-size: 0.95rem;
            max-width: 680px;
        }

        .hero-metrics {
            display: flex;
            flex-wrap: wrap;
            gap: 0.58rem;
            margin-top: 0.9rem;
        }

        .metric-pill {
            background: color-mix(in srgb, var(--accent) 14%, transparent);
            border: 1px solid color-mix(in srgb, var(--accent) 34%, transparent);
            border-radius: 999px;
            padding: 0.4rem 0.72rem;
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
        }

        .metric-label {
            color: color-mix(in srgb, var(--ink-1) 74%, var(--accent));
            font-weight: 600;
            font-size: 0.79rem;
        }

        .metric-value {
            color: var(--ink-1);
            font-size: 0.84rem;
            font-weight: 700;
        }

        [data-testid="stSidebar"] {
            background: var(--sidebar-bg);
            border-right: 1px solid color-mix(in srgb, var(--ink-1) 10%, transparent);
            min-width: clamp(240px, 22vw, 320px) !important;
            width: clamp(240px, 22vw, 320px) !important;
        }

        :root[data-user-theme="light"] [data-testid="stSidebar"],
        :root[data-theme="light"] [data-testid="stSidebar"] {
            background: #ffffff !important;
            border-right-color: rgba(0, 0, 0, 0.12) !important;
            min-width: clamp(240px, 22vw, 320px) !important;
            width: clamp(240px, 22vw, 320px) !important;
        }

        :root[data-user-theme="dark"] [data-testid="stSidebar"],
        :root[data-theme="dark"] [data-testid="stSidebar"] {
            background: #000000 !important;
            border-right-color: rgba(255, 255, 255, 0.12) !important;
            min-width: clamp(240px, 22vw, 320px) !important;
            width: clamp(240px, 22vw, 320px) !important;
        }

        [data-testid="stSidebarNav"] {
            display: none !important;
        }

        [data-testid="stSidebar"] [data-baseweb="radio"] {
            color: var(--ink-1) !important;
        }

        [data-testid="stSidebar"] [data-baseweb="radio"] span,
        [data-testid="stSidebar"] [data-baseweb="radio"] label,
        [data-testid="stSidebar"] [data-testid="stRadio"] {
            color: var(--ink-1) !important;
        }

        [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"],
        [data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"] {
            background: var(--surface) !important;
            color: var(--ink-1) !important;
        }

        [data-testid="stSidebar"] [data-testid="stFileUploader"] button,
        [data-testid="stSidebar"] [data-testid="stFileUploader"] button span,
        [data-testid="stSidebar"] [data-testid="stFileUploader"] button svg {
            color: var(--ink-1) !important;
            fill: var(--ink-1) !important;
        }

        [data-testid="stSidebar"] [data-testid="stFileUploader"] button {
            background: var(--surface) !important;
            border: 1px solid color-mix(in srgb, var(--accent) 28%, transparent) !important;
        }

        :root[data-user-theme="light"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"],
        :root[data-theme="light"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"],
        :root[data-user-theme="light"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"],
        :root[data-theme="light"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"] {
            background: #ffffff !important;
            color: #111111 !important;
            border-color: rgba(0, 0, 0, 0.12) !important;
        }

        :root[data-user-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"],
        :root[data-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"],
        :root[data-user-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"],
        :root[data-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzoneInstructions"] {
            background: #000000 !important;
            color: #ffffff !important;
            border-color: rgba(255, 255, 255, 0.14) !important;
        }

        :root[data-user-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] *,
        :root[data-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] * {
            color: #f3f6fb !important;
            fill: #f3f6fb !important;
        }

        :root[data-user-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploader"] button,
        :root[data-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploader"] button {
            background: rgba(45, 212, 191, 0.18) !important;
            border-color: rgba(45, 212, 191, 0.48) !important;
        }

        :root[data-user-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploader"] button span,
        :root[data-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploader"] button span,
        :root[data-user-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploader"] button svg,
        :root[data-theme="dark"] [data-testid="stSidebar"] [data-testid="stFileUploader"] button svg {
            color: #f3f6fb !important;
            fill: #f3f6fb !important;
        }

        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
            padding-top: 1rem;
            gap: 0.44rem;
        }

        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3 {
            font-family: "DM Serif Display", Georgia, serif;
            color: var(--ink-1);
            letter-spacing: 0.01em;
            margin-bottom: 0.2rem;
        }

        [data-testid="stFileUploader"] {
            background: var(--card);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 0.45rem;
        }

        :root[data-user-theme="light"] [data-testid="stFileUploader"],
        :root[data-theme="light"] [data-testid="stFileUploader"] {
            background: #ffffff !important;
            border-color: rgba(0, 0, 0, 0.12) !important;
        }

        :root[data-user-theme="dark"] [data-testid="stFileUploader"],
        :root[data-theme="dark"] [data-testid="stFileUploader"] {
            background: #000000 !important;
            border-color: rgba(255, 255, 255, 0.14) !important;
        }

        [data-testid="stAlert"] {
            border-radius: 12px;
            border: 1px solid color-mix(in srgb, var(--accent) 24%, transparent);
        }

        .stButton > button,
        .stDownloadButton > button {
            border: 1px solid color-mix(in srgb, var(--accent) 32%, transparent);
            border-radius: 999px;
            background: linear-gradient(135deg, var(--accent), var(--accent-strong));
            color: #ffffff;
            font-weight: 600;
            transition: transform 160ms ease, box-shadow 160ms ease, filter 160ms ease;
            box-shadow: 0 9px 22px color-mix(in srgb, var(--accent) 28%, transparent);
            min-height: 2.45rem;
            letter-spacing: 0.01em;
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover {
            transform: translateY(-1px) scale(1.01);
            box-shadow: 0 13px 28px color-mix(in srgb, var(--accent) 36%, transparent);
            filter: saturate(1.08);
        }

        .stButton > button:focus,
        .stDownloadButton > button:focus {
            outline: 2px solid color-mix(in srgb, var(--accent-2) 60%, var(--accent));
            outline-offset: 2px;
        }

        [data-testid="stChatMessage"] {
            border-radius: 16px;
            padding: 0.72rem 0.88rem;
            margin-bottom: 0.54rem;
            border: 1px solid color-mix(in srgb, var(--accent) 20%, transparent);
            background: var(--card);
            box-shadow: 0 8px 18px color-mix(in srgb, #0f172a 12%, transparent);
            animation: fadeUp 280ms ease;
            align-items: flex-start;
        }

        [data-testid="stChatMessageContent"] p {
            margin: 0.12rem 0;
            color: var(--ink-1);
        }

        [data-testid="stChatMessageContent"],
        [data-testid="stChatMessageContent"] * {
            color: var(--ink-1) !important;
        }

        [data-testid="stChatMessageAvatarUser"] {
            background: var(--accent-2) !important;
            color: #fff !important;
        }

        [data-testid="stChatMessageAvatarAssistant"] {
            background: var(--accent) !important;
            color: #fff !important;
        }

        [data-testid="stChatInput"] {
            border-top: 1px solid color-mix(in srgb, var(--ink-1) 18%, transparent);
            background: var(--bg-1);
            backdrop-filter: blur(6px);
        }

        [data-testid="stChatInput"],
        [data-testid="stChatInput"] > div,
        [data-testid="stChatInput"] > div > div,
        [data-testid="stChatInput"] textarea {
            background: var(--surface) !important;
            color: var(--ink-1) !important;
        }

        :root[data-user-theme="light"] [data-testid="stChatInput"],
        :root[data-theme="light"] [data-testid="stChatInput"],
        :root[data-user-theme="light"] [data-testid="stChatInput"] > div,
        :root[data-theme="light"] [data-testid="stChatInput"] > div,
        :root[data-user-theme="light"] [data-testid="stChatInput"] > div > div,
        :root[data-theme="light"] [data-testid="stChatInput"] > div > div,
        :root[data-user-theme="light"] [data-testid="stChatInput"] textarea,
        :root[data-theme="light"] [data-testid="stChatInput"] textarea {
            background: #ffffff !important;
            color: #111111 !important;
            border-color: rgba(0, 0, 0, 0.14) !important;
        }

        :root[data-user-theme="dark"] [data-testid="stChatInput"],
        :root[data-theme="dark"] [data-testid="stChatInput"],
        :root[data-user-theme="dark"] [data-testid="stChatInput"] > div,
        :root[data-theme="dark"] [data-testid="stChatInput"] > div,
        :root[data-user-theme="dark"] [data-testid="stChatInput"] > div > div,
        :root[data-theme="dark"] [data-testid="stChatInput"] > div > div,
        :root[data-user-theme="dark"] [data-testid="stChatInput"] textarea,
        :root[data-theme="dark"] [data-testid="stChatInput"] textarea {
            background: #000000 !important;
            color: #ffffff !important;
            border-color: rgba(255, 255, 255, 0.14) !important;
        }

        [data-testid="stChatInput"] textarea,
        [data-testid="stTextInput"] input {
            color: var(--ink-1) !important;
        }

        [data-testid="stChatInput"] textarea::placeholder,
        [data-testid="stTextInput"] input::placeholder {
            color: var(--ink-soft) !important;
            opacity: 1;
        }

        :root[data-user-theme="dark"] [data-testid="stAlert"],
        :root[data-user-theme="dark"] [data-testid="stFileUploader"],
        :root[data-user-theme="dark"] [data-testid="stChatMessage"],
        :root[data-theme="dark"] [data-testid="stAlert"],
        :root[data-theme="dark"] [data-testid="stFileUploader"],
        :root[data-theme="dark"] [data-testid="stChatMessage"] {
            border-color: rgba(148, 163, 184, 0.35) !important;
        }

        :root[data-user-theme="dark"] [data-testid="stSidebar"] h2,
        :root[data-user-theme="dark"] [data-testid="stSidebar"] h3,
        :root[data-user-theme="dark"] .hero-title,
        :root[data-user-theme="dark"] .hero-subtitle,
        :root[data-user-theme="dark"] .metric-label,
        :root[data-user-theme="dark"] .metric-value,
        :root[data-theme="dark"] [data-testid="stSidebar"] h2,
        :root[data-theme="dark"] [data-testid="stSidebar"] h3,
        :root[data-theme="dark"] .hero-title,
        :root[data-theme="dark"] .hero-subtitle,
        :root[data-theme="dark"] .metric-label,
        :root[data-theme="dark"] .metric-value {
            color: var(--ink-1) !important;
        }

        :root[data-user-theme="dark"] [data-testid="stToolbar"] button,
        :root[data-theme="dark"] [data-testid="stToolbar"] button {
            background: rgba(15, 23, 42, 0.72) !important;
            border: 1px solid rgba(148, 163, 184, 0.45) !important;
            color: #f3f6fb !important;
        }

        :root[data-user-theme="dark"] [data-testid="stToolbar"] button svg,
        :root[data-theme="dark"] [data-testid="stToolbar"] button svg,
        :root[data-user-theme="dark"] [data-testid="stToolbar"] *,
        :root[data-theme="dark"] [data-testid="stToolbar"] * {
            color: #f3f6fb !important;
            fill: #f3f6fb !important;
        }

        :root[data-user-theme="dark"] [data-testid="stChatInput"] > div,
        :root[data-theme="dark"] [data-testid="stChatInput"] > div {
            background: rgba(15, 23, 42, 0.92) !important;
            border: 1px solid rgba(148, 163, 184, 0.45) !important;
            border-radius: 14px !important;
        }

        :root[data-user-theme="dark"] [data-testid="stChatInput"] button,
        :root[data-theme="dark"] [data-testid="stChatInput"] button {
            background: rgba(45, 212, 191, 0.25) !important;
            border: 1px solid rgba(45, 212, 191, 0.55) !important;
            color: #f3f6fb !important;
        }

        :root[data-user-theme="dark"] [data-testid="stChatInput"] button svg,
        :root[data-theme="dark"] [data-testid="stChatInput"] button svg {
            fill: #f3f6fb !important;
            color: #f3f6fb !important;
        }

        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        @media (max-width: 768px) {
            .block-container {
                padding-top: 1rem;
                padding-left: 0.8rem;
                padding-right: 0.8rem;
            }

            [data-testid="stSidebar"] {
                min-width: auto !important;
                width: auto !important;
            }

            .hero-card {
                border-radius: 16px;
                padding: 0.92rem;
            }

            .hero-title {
                font-size: 1.8rem;
            }

            .hero-metrics {
                gap: 0.45rem;
            }

            .metric-pill {
                width: 100%;
                justify-content: space-between;
            }

            .stButton > button,
            .stDownloadButton > button {
                width: 100%;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    indexed_sources_preview = get_unique_sources(collection)
    if not st.session_state.indexed_files:
        st.session_state.indexed_files.update(indexed_sources_preview)
    render_hero(indexed_file_count=len(indexed_sources_preview), chunk_count=collection.count())

    with st.sidebar:
        st.divider()
        st.header("Documents")
        uploaded_files = st.file_uploader(
            "Upload PDF, DOC, DOCX, or TXT files.",
            type=["pdf", "doc", "docx", "txt"],
            accept_multiple_files=True,
        )
        if uploaded_files and len(uploaded_files) > MAX_UPLOADS:
            st.error(f"Please upload at most {MAX_UPLOADS} files at a time.")
            uploaded_files = uploaded_files[:MAX_UPLOADS]

        if st.button("New chat"):
            clear_chat_history()
            st.rerun()

        if st.session_state.messages:
            st.download_button(
                "Download chat (JSON)",
                data=chat_history_as_json(),
                file_name="smart_file_chat_history.json",
                mime="application/json",
                use_container_width=True,
            )
            st.download_button(
                "Download chat (TXT)",
                data=chat_history_as_text(),
                file_name="smart_file_chat_history.txt",
                mime="text/plain",
                use_container_width=True,
            )

        st.divider()
        st.subheader("Index status")
        st.write(f"Stored chunks: {collection.count()}")
        indexed_sources = sorted(st.session_state.indexed_files)
        if indexed_sources:
            st.write("Indexed files:")
            for source in indexed_sources:
                st.write(f"- {source}")
        else:
            st.info("No documents have been indexed yet.")

        if not get_api_key():
            st.warning("Set GOOGLE_API_KEY or GEMINI_API_KEY in your environment to enable Gemini.")
        else:
            st.success("Gemini API key detected.")

    if uploaded_files:
        if client is None:
            st.error("Gemini client is unavailable because no API key was found.")
        else:
            with tempfile.TemporaryDirectory() as temp_dir:
                indexed_count = 0
                indexed_sources = []
                for uploaded_file in uploaded_files:
                    temp_path = Path(temp_dir) / uploaded_file.name
                    temp_path.write_bytes(uploaded_file.getbuffer())
                    try:
                        with st.spinner(f"Indexing {uploaded_file.name}..."):
                            chunk_count = ingest_uploaded_file(client, collection, temp_path, uploaded_file.name)
                            indexed_count += chunk_count
                            if chunk_count:
                                indexed_sources.append(uploaded_file.name)
                    except Exception as exc:
                        st.error(f"{uploaded_file.name}: {exc}")
                if indexed_count:
                    st.session_state.indexed_files.update(indexed_sources)
                    st.success(f"Indexed {indexed_count} chunks from {len(indexed_sources)} file(s).")

    render_chat()

    user_question = st.chat_input("Ask a question about your files or anything else")
    if user_question:
        append_message("user", user_question)
        with st.chat_message("user"):
            st.markdown(user_question)

        if client is None:
            assistant_answer = "Set GOOGLE_API_KEY or GEMINI_API_KEY to use Gemini."
        else:
            with st.spinner("Thinking..."):
                try:
                    assistant_answer = route_question(client, collection, user_question, sorted(st.session_state.indexed_files))
                except Exception as exc:
                    assistant_answer = f"Unable to answer right now: {exc}"

        append_message("assistant", assistant_answer)
        with st.chat_message("assistant"):
            st.markdown(assistant_answer)


if __name__ == "__main__":
    main()
