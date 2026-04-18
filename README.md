# SmartFile-AI

SmartFile-AI is a Streamlit-based RAG assistant that lets you upload PDF, DOC, DOCX, and TXT files, indexes them with Gemini embeddings + ChromaDB, and answers with document-grounded responses (with source context) plus general AI fallback.

## Assignment Requirements Covered

- File upload: `.pdf`, `.doc`, `.docx`, `.txt`
- Text extraction and chunking
- Embeddings + local ChromaDB storage (persistent across reloads)
- Retrieval of relevant chunks on each query
- Source citation in document-based answers
- No hallucination rule for missing document answers:
  - `I could not find this information in the uploaded files.`
- General knowledge fallback via Gemini when document retrieval is not applicable
- Chat interface + chat history + export in Streamlit

## Tech Stack

- AI model: Google Gemini API
- Framework: LangChain
- Vector database: ChromaDB (local)
- Frontend: Streamlit

## Project Structure

```text
SmartFile-AI/
|-- app.py
|-- requirements.txt
|-- README.md
|-- chroma_db/      # auto-created
`-- sample_files/
```

## Setup

1. Install dependencies.

```bash
pip install -r requirements.txt
```

2. Configure API key.

Option A: Environment variable

```powershell
$env:GOOGLE_API_KEY="your_key_here"
```

You can also use `GEMINI_API_KEY` instead of `GOOGLE_API_KEY`.

Option B: `.env` file

```env
GOOGLE_API_KEY=your_key_here
```

3. Run the app.

```bash
streamlit run app.py
```

## How It Works

1. Upload one or more supported files.
2. Documents are parsed, chunked, embedded, and stored in ChromaDB.
3. On each user question, the app retrieves top matching chunks.
4. If strong document match exists, the answer is generated from context with citation.
5. If the question is document-related but data is missing, the app returns the exact no-hallucination message.
6. Otherwise, it falls back to Gemini general assistant mode.

## Notes

- `.doc` extraction on Windows uses Microsoft Word COM automation.
- If Word/COM is unavailable, convert `.doc` to `.docx`, `.pdf`, or `.txt`.
- Chroma data remains in `chroma_db/` for persistence across app restarts.
