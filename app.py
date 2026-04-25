import hashlib , importlib , json, os, re, time, urllib.error, urllib.request
from pathlib import Path
import streamlit as st
from chromadb import PersistentClient
from docx import Document
from dotenv import load_dotenv
from google import genai
from google.genai import types
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)

BASE_DIR = Path(__file__).resolve().parent
APP_TITLE = "SmartFile-AI"
DB_PATH = BASE_DIR / "chroma_db"
HISTORY_PATH = BASE_DIR / "chat_history.json"
FILES_PATH = BASE_DIR / "stored_files"
COLLECTION_NAME = "smart_file_documents"
CHAT_MODELS = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
OPENROUTER_MODELS = ["openai/gpt-4o-mini", "google/gemini-2.0-flash-001"]
LOCAL_EMBED_DIM = 256
MAX_FILES = 5
MAX_RETRIES = 3
THRESHOLD = 0.55
NOT_FOUND = "I could not find this information in the uploaded files."
GEN_FAIL = "I could not generate a response right now."
RESPONSE_STYLE = (
    "Response format:\n"
    "1) Start with a direct answer in 1-2 lines.\n"
    "2) Then write 'Main points:' and provide 3-5 bullet points.\n"
    "3) Highlight key terms using **bold** markdown."
)
USER_AVATAR = "🧑"
ASSISTANT_AVATAR = "🤖"
SMALL_TALK = {
    "hi": "Hi! Ask from uploaded files, or ask any general question.",
    "hello": "Hello! Ask from uploaded files, or ask any general question.",
    "hey": "Hey! Ask from uploaded files, or ask any general question.",
    "hii": "Hi! Ask from uploaded files, or ask any general question.",
    "thanks": "You are welcome!",
    "thank you": "You are welcome!",
    "ok": "Okay, what do you want to ask next?",
    "okay": "Okay, what do you want to ask next?",
}
DOC_HINTS = {
    "file","files","document","documents","resume","cv","pdf","doc","docx","txt","uploaded","from file","from the file","from document",
}
SUMMARY_HINTS = {
    "summarize","summary","summarise","briefly explain","give a summary","tell me about",
}
FILES_HINTS = {
    "what are the files uploaded","what files are uploaded","which files are uploaded","uploaded files","files uploaded","list the files","show the files",
}
SPLITTER = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=200)

def annotate_source(answer, source_label):
    if not answer:
        return answer
    if "Source:" in answer:
        return answer
    return f"{answer}\n\nSource: {source_label}"

def get_api_key():
    return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("OPENROUTER_API_KEY")

@st.cache_resource(show_spinner=False)
def get_llm_backend():
    key = get_api_key()
    if not key:
        return {"provider": "none", "client": None, "key": None}
    if key.startswith("sk-or-"):
        return {"provider": "openrouter", "client": None, "key": key}
    return {"provider": "gemini", "client": genai.Client(api_key=key), "key": key}

@st.cache_resource(show_spinner=False)
def get_collection():
    DB_PATH.mkdir(exist_ok=True)
    return PersistentClient(path=str(DB_PATH)).get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

def ensure_files_dir():
    FILES_PATH.mkdir(exist_ok=True)

def list_stored_files():
    ensure_files_dir()
    return sorted([p.name for p in FILES_PATH.iterdir() if p.is_file()])

def save_uploaded_file(uploaded_file):
    ensure_files_dir()
    path = FILES_PATH / uploaded_file.name
    path.write_bytes(uploaded_file.getbuffer())
    return path

def source_doc_ids(collection, source_name):
    found = collection.get(where={"source": source_name}, include=[])
    return found.get("ids", []) if found else []

def delete_source_from_collection(collection, source_name):
    ids = source_doc_ids(collection, source_name)
    if ids:
        collection.delete(ids=ids)

def delete_stored_file(collection, source_name):
    delete_source_from_collection(collection, source_name)
    p = FILES_PATH / source_name
    if p.exists():
        p.unlink()

def clear_all_stored_data(collection):
    for name in list_stored_files():
        p = FILES_PATH / name
        try:
            p.unlink()
        except Exception:
            pass
    all_items = collection.get(include=[])
    ids = all_items.get("ids", []) if all_items else []
    if ids:
        collection.delete(ids=ids)

def retry(fn):
    last = None
    for i in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e).lower()
            if not any(x in msg for x in ["429", "503", "resource_exhausted", "unavailable", "high demand"]) or i == MAX_RETRIES - 1:
                raise
            retry_match = re.search(r"retry in\s*([0-9]+(?:\.[0-9]+)?)s", msg)
            delay = float(retry_match.group(1)) if retry_match else min(4, 2**i)
            time.sleep(min(60, max(1, delay)))
    raise last if last else RuntimeError("Request failed")

def readable_error(err):
    msg = str(err)
    low = msg.lower()
    if "resource_exhausted" in low or "quota exceeded" in low or "429" in low:
        retry_match = re.search(r"retry in\s*([0-9]+(?:\.[0-9]+)?)s", low)
        wait_text = f" Please wait about {int(float(retry_match.group(1)))} seconds and retry." if retry_match else " Please wait a minute and retry."
        return "Gemini quota limit reached during indexing." + wait_text
    return msg

def generate_openrouter(key, model, prompt, temperature=0.2, max_tokens=512):
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost",
            "X-Title": APP_TITLE,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()

def generate_with_fallback(backend, prompt, temperature=0.2, fallback=GEN_FAIL):
    provider = backend.get("provider") if backend else "none"
    if provider == "gemini":
        client = backend.get("client")
        for model in CHAT_MODELS:
            try:
                r = retry(
                    lambda: client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(temperature=temperature, max_output_tokens=512),
                    )
                )
                txt = (r.text or "").strip()
                if txt:
                    return txt
            except Exception:
                pass
    elif provider == "openrouter":
        key = backend.get("key")
        for model in OPENROUTER_MODELS:
            try:
                txt = retry(lambda: generate_openrouter(key, model, prompt, temperature=temperature, max_tokens=512))
                if txt:
                    return txt
            except Exception:
                pass
    return fallback

def load_history():
    if not HISTORY_PATH.exists():
        return []
    try:
        return [
            m
            for m in json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(m, dict) and m.get("role") in {"user", "assistant"} and m.get("content")
        ]
    except Exception:
        return []

def save_history():
    HISTORY_PATH.write_text(json.dumps(st.session_state.messages, indent=2, ensure_ascii=False), encoding="utf-8")

def add_message(role, content):
    st.session_state.messages.append({"role": role, "content": content})
    save_history()

def extract_doc_text(path):
    py = wd = None
    try:
        py = importlib.import_module("pythoncom")
        w32 = importlib.import_module("win32com.client")
        py.CoInitialize()
        wd = w32.DispatchEx("Word.Application")
        wd.Visible = False
        wd.DisplayAlerts = 0
        doc = wd.Documents.Open(str(path), ReadOnly=1)
        txt = (doc.Content.Text or "").strip()
        doc.Close(False)
        return txt
    except Exception as e:
        raise RuntimeError("Could not read .doc file. Convert to .docx/.pdf/.txt.") from e
    finally:
        try:
            if wd:
                wd.Quit()
            if py:
                py.CoUninitialize()
        except Exception:
            pass

def extract_text(path):
    ext = path.suffix.lower()
    if ext == ".pdf":
        return " ".join((p.extract_text() or "").strip() for p in PdfReader(str(path)).pages).strip()
    if ext == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    if ext == ".docx":
        return " ".join(p.text.strip() for p in Document(str(path)).paragraphs if p.text.strip()).strip()
    if ext == ".doc":
        return extract_doc_text(path)
    raise ValueError(f"Unsupported file type: {ext}")

def local_embed_text(text, dim=LOCAL_EMBED_DIM):
    vec = [0.0] * dim
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    if not tokens:
        return vec
    for tok in tokens:
        h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest()[:8], 16)
        vec[h % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0:
        return vec
    return [v / norm for v in vec]

def collection_embedding_dim(collection):
    try:
        sample = collection.get(limit=1, include=["embeddings"])
        embeddings = sample.get("embeddings", []) if sample else []
        if embeddings and embeddings[0]:
            return len(embeddings[0])
    except Exception:
        pass
    return LOCAL_EMBED_DIM

def embed_texts(_backend, texts, collection=None):
    # Keep embedding size aligned with the collection's configured dimension.
    dim = collection_embedding_dim(collection) if collection is not None else LOCAL_EMBED_DIM
    # Local deterministic embeddings avoid external quota failures during indexing/retrieval.
    return [local_embed_text(t, dim=dim) for t in texts]

def index_file(backend, collection, file_path, file_name):
    sig = hashlib.sha256(file_path.read_bytes()).hexdigest()[:16]
    # Chroma expects exactly one top-level operator in complex metadata filters.
    existing = collection.get(
        where={"$and": [{"source": file_name}, {"file_signature": sig}]},
        include=[],
    )
    existing_ids = existing.get("ids", []) if existing else []
    if existing_ids:
        return len(existing_ids)

    text = extract_text(file_path)
    if not text:
        return 0
    # Keep retrieval accurate by replacing previous chunks from the same file name.
    delete_source_from_collection(collection, file_name)
    chunks = [c.strip() for c in SPLITTER.split_text(text) if c.strip()]
    if not chunks:
        return 0
    vecs = embed_texts(backend, chunks, collection=collection)
    ids = [f"{sig}-{i}" for i in range(len(chunks))]
    metas = [{"source": file_name, "chunk_index": i, "file_signature": sig} for i in range(len(chunks))]
    collection.upsert(ids=ids, documents=chunks, metadatas=metas, embeddings=vecs)
    return len(chunks)

def retrieve(backend, collection, question, k=4, source_name=None):
    if collection.count() == 0:
        return []
    qv = embed_texts(backend, [question], collection=collection)[0]
    kwargs = {
        "query_embeddings": [qv],
        "n_results": k,
        "include": ["documents", "metadatas", "distances"],
    }
    if source_name:
        kwargs["where"] = {"source": source_name}
    r = collection.query(**kwargs)
    docs = r.get("documents", [[]])[0]
    metas = r.get("metadatas", [[]])[0]
    dists = r.get("distances", [[]])[0]
    return [
        {
            "text": d,
            "source": m.get("source", "Unknown") if m else "Unknown",
            "chunk_index": m.get("chunk_index", -1) if m else -1,
            "sim": 1 - float(dist),
        }
        for d, m, dist in zip(docs, metas, dists)
    ]

def doc_question(question, sources):
    q = question.lower().strip()
    return any(k in q for k in DOC_HINTS) or any((s.lower() in q) or (Path(s).stem.lower() in q) for s in sources)

def mentioned_source(question, sources):
    q = question.lower().strip()
    for s in sources:
        s_low = s.lower()
        stem_low = Path(s).stem.lower()
        if s_low in q or stem_low in q:
            return s
    return None

def summary_question(question):
    q = question.lower().strip()
    return any(k in q for k in SUMMARY_HINTS) or q.startswith("summarize ") or q.startswith("summarise ")

def files_question(question):
    q = question.lower().strip()
    return any(k in q for k in FILES_HINTS)

def answer_uploaded_files(sources):
    if not sources:
        return "No files are currently uploaded or indexed."
    unique_sources = list(dict.fromkeys(sources))
    return "Uploaded files: " + ", ".join(unique_sources)

def source_suffix(sources):
    unique_sources = [s for s in dict.fromkeys(sources) if s]
    if not unique_sources:
        return ""
    return f"(source: {', '.join(unique_sources)})"

def retrieve_safe(backend, collection, question, k=4, source_name=None):
    try:
        return retrieve(backend, collection, question, k=k, source_name=source_name)
    except Exception:
        return []

def tokenize(text):
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 1]

def lexical_retrieve(collection, question, k=4, source_name=None):
    q_tokens = tokenize(question)
    if not q_tokens:
        return []
    q_set = set(q_tokens)
    where = {"source": source_name} if source_name else None
    data = collection.get(where=where, include=["documents", "metadatas"])
    docs = data.get("documents", []) if data else []
    metas = data.get("metadatas", []) if data else []
    ranked = []
    for d, m in zip(docs, metas):
        d_tokens = set(tokenize(d))
        if not d_tokens:
            continue
        overlap = len(q_set & d_tokens)
        if overlap == 0:
            continue
        score = overlap / max(1, len(q_set))
        ranked.append(
            {
                "text": d,
                "source": (m or {}).get("source", "Unknown"),
                "chunk_index": (m or {}).get("chunk_index", -1),
                "sim": float(score),
            }
        )
    ranked.sort(key=lambda x: x["sim"], reverse=True)
    return ranked[:k]

def answer_from_docs(backend, question, chunks):
    if not chunks:
        return NOT_FOUND
    context_chunks = chunks[:8] if summary_question(question) else chunks[:4]
    context = "\n".join(
        f"Source: {c['source']} | Chunk: {c['chunk_index']} | Similarity: {c['sim']:.2f}\n{c['text']}"
        for c in context_chunks
    )
    if summary_question(question):
        prompt = (
            "Write a concise summary based only on the excerpts. "
            "Use 3 to 5 short sentences. Do not mention the word 'excerpts'. "
            "If the excerpts are incomplete, say the summary is partial.\n"
            f"{RESPONSE_STYLE}\n\n"
            f"Question: {question}\n\nExcerpts:\n{context}"
        )
    else:
        prompt = (
            "Answer only from the excerpts. "
            f"If the answer is clearly absent, reply exactly: {NOT_FOUND}.\n"
            f"{RESPONSE_STYLE}\n\n"
            f"Question: {question}\n\nExcerpts:\n{context}"
        )
    ans = generate_with_fallback(backend, prompt, temperature=0, fallback=NOT_FOUND)
    if ans != NOT_FOUND:
        src = [c.get("source", "") for c in chunks]
        suffix = source_suffix(src)
        if suffix and "(source:" not in ans.lower():
            if summary_question(question):
                ans = f"{ans}\n\n{suffix}"
            else:
                ans = f"{ans} {suffix}"
    return ans

def general_prompt(question):
    return (
        "You are a helpful general-purpose chatbot. Answer the user's question directly and accurately. "
        "Be concise unless the user asks for detail. If the question is ambiguous, state the most likely meaning and note the ambiguity. "
        "If you are unsure, say so instead of inventing facts. Do not mention uploaded files unless the user asks about them.\n"
        f"{RESPONSE_STYLE}\n\n"
        f"Question: {question}"
    )

def answer_question(backend, collection, question, sources):
    q = question.lower().strip()
    if q in SMALL_TALK:
        return SMALL_TALK[q]

    if files_question(question):
        return answer_uploaded_files(sources)

    if doc_question(question, sources):
        chosen_source = mentioned_source(question, sources)
        top_k = 8 if summary_question(question) else 4
        chunks = retrieve_safe(backend, collection, question, k=top_k, source_name=chosen_source)
        if (not chunks) or (chunks and chunks[0]["sim"] < THRESHOLD and not summary_question(question)):
            chunks = lexical_retrieve(collection, question, k=top_k, source_name=chosen_source)
        if chunks and (chunks[0]["sim"] >= THRESHOLD or summary_question(question) or chunks[0]["sim"] > 0):
            return answer_from_docs(backend, question, chunks)
        return NOT_FOUND
    ans = generate_with_fallback(backend, general_prompt(question), temperature=0.2, fallback=GEN_FAIL)
    if ans == GEN_FAIL:
        return annotate_source(local_general_answer(question), "local fallback")
    return annotate_source(ans, "Gemini")

def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="📄", layout="wide")
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700&display=swap');
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@700;800;900&display=swap');
        html, body, [class*="css"], .stApp {font-family: 'Manrope', sans-serif;}
        h1 {font-size: 2rem !important; letter-spacing: -0.02em;}
        .brand-title {font-family: 'Outfit', sans-serif; font-size: 3.05rem; line-height: 1.05; letter-spacing: -0.03em; margin: 0 0 .25rem 0; font-weight: 900;}
        .brand-full {background: linear-gradient(90deg, #14b8a6 0%, #0ea5e9 45%, #2563eb 100%); -webkit-background-clip: text; background-clip: text; color: transparent;}
        h2 {font-size: 1.35rem !important;} h3 {font-size: 1.1rem !important;}
        .stCaption, [data-testid="stMetricLabel"] {font-size: 0.92rem !important;}
        [data-testid="stMetricValue"] {font-size: 1.4rem !important; font-weight: 700;}
        .stButton>button, .stDownloadButton>button {font-size: 0.95rem; padding: 0.45rem 0.85rem; border-radius: 10px;}
        .stChatInput input {font-size: 1rem !important;}
        [data-testid="stChatMessageContent"] p, [data-testid="stChatMessageContent"] li {font-size: 1rem !important; line-height: 1.6;}
        [data-testid="stSidebar"] * {font-size: 0.95rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    if "messages" not in st.session_state:
        st.session_state.messages = load_history()
    st.session_state.setdefault("indexed_files", set())

    backend = get_llm_backend()
    collection = get_collection()
    ensure_files_dir()
    data = collection.get(include=["metadatas"])
    st.session_state.indexed_files.update(sorted({m.get("source", "Unknown") for m in data.get("metadatas", []) if m}))
    st.session_state.indexed_files.update(list_stored_files())

    st.markdown(
        '<h1 class="brand-title"><span class="brand-full">SmartFile-AI</span></h1>',
        unsafe_allow_html=True,
    )
    st.caption("Ask from uploaded docs with sources, or ask anything generally.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Indexed Files", len(st.session_state.indexed_files))
    c2.metric("Stored Chunks", collection.count())
    c3.metric("Mode", "Doc + General")

    with st.sidebar:
        st.header("Documents")
        files = st.file_uploader("Upload PDF, DOC, DOCX, or TXT", type=["pdf", "doc", "docx", "txt"], accept_multiple_files=True)
        index_now = st.button("Store and Index uploaded files", use_container_width=True)
        if files and len(files) > MAX_FILES:
            st.error(f"Upload at most {MAX_FILES} files.")
            files = files[:MAX_FILES]

        if st.button("New chat"):
            st.session_state.messages = []
            save_history()
            st.rerun()

        if st.session_state.messages:
            st.download_button("Download JSON", json.dumps(st.session_state.messages, indent=2, ensure_ascii=False), "chat_history.json", "application/json")

        st.subheader("Index Status")
        st.write(f"Stored chunks: {collection.count()}")
        for s in sorted(st.session_state.indexed_files):
            st.write(f"- {s}")

        st.subheader("File Store (CRUD)")
        stored_files = list_stored_files()
        st.write(f"Stored file count: {len(stored_files)}")
        selected_file = st.selectbox("Select stored file", [""] + stored_files)
        c_upd, c_del = st.columns(2)
        if c_upd.button("Update/Reindex", use_container_width=True):
            if not selected_file:
                st.warning("Select a file to reindex.")
            else:
                p = FILES_PATH / selected_file
                try:
                    n = index_file(backend, collection, p, selected_file)
                    if n:
                        st.session_state.indexed_files.add(selected_file)
                    st.success(f"Reindexed {selected_file} with {n} chunks.")
                except Exception as e:
                    st.error(f"Update failed: {e}")
        if c_del.button("Delete", use_container_width=True):
            if not selected_file:
                st.warning("Select a file to delete.")
            else:
                delete_stored_file(collection, selected_file)
                st.session_state.indexed_files.discard(selected_file)
                st.success(f"Deleted {selected_file} from storage and index.")
        if st.button("Clear all stored files and index", use_container_width=True):
            clear_all_stored_data(collection)
            st.session_state.indexed_files = set()
            st.success("Cleared all stored files and indexed chunks.")

        if not get_api_key():
            st.warning("Set GOOGLE_API_KEY, GEMINI_API_KEY, or OPENROUTER_API_KEY in .env")
    if files and index_now:
        total = 0
        added = []
        for f in files:
            try:
                p = save_uploaded_file(f)
                with st.spinner(f"Indexing {f.name}..."):
                    n = index_file(backend, collection, p, f.name)
                    total += n
                    if n:
                        added.append(f.name)
            except Exception as e:
                st.error(f"{f.name}: {readable_error(e)}")
        if total:
            st.session_state.indexed_files.update(added)
            st.success(f"Indexed {total} chunks from {len(added)} file(s).")
            st.rerun()
    for m in st.session_state.messages:
        avatar = USER_AVATAR if m["role"] == "user" else ASSISTANT_AVATAR
        with st.chat_message(m["role"], avatar=avatar):
            st.markdown(m["content"])

    q = st.chat_input("Ask your question")
    if q:
        add_message("user", q)
        with st.chat_message("user", avatar=USER_AVATAR):
            st.markdown(q)
        if not backend or backend.get("provider") == "none":
            a = "Set GOOGLE_API_KEY, GEMINI_API_KEY, or OPENROUTER_API_KEY to start chatting."
        else:
            with st.spinner("Thinking..."):
                try:
                    a = answer_question(backend, collection, q, sorted(st.session_state.indexed_files))
                except Exception:
                    if doc_question(q, sorted(st.session_state.indexed_files)):
                        a = NOT_FOUND
                    else:
                        a = annotate_source(local_general_answer(q), "local fallback")
        add_message("assistant", a)
        with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
            st.markdown(a)
if __name__ == "__main__":
    main()
