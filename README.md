# SmartFile-AI

SmartFile-AI is a Streamlit app where you can upload files and ask questions.
It supports:PDF,DOC,DOCX,TXT
The app does:
- text extraction from uploaded files
- chunking and embedding
- storage in local ChromaDB
- retrieval-based answers from uploaded files
- source mention in file-based answers
- exact fallback when answer is not in files:
- I could not find this information in the uploaded files.
- general answer fallback using Gemini
- chat history save and download

## Project Files
- app.py: main app
- config.py: constants
- requirements.txt: dependencies
- chat_history.json: stored chat
- chroma_db/: local vector database
- sample_files/: test files

## Setup
1. Install dependencies

```bash
pip install -r requirements.txt
```

2. Add API key in `.env`
```env
GOOGLE_API_KEY=your_key_here
```
You can also use `GEMINI_API_KEY`.

3. Run
```bash
streamlit run app.py
```
## Notes
- `.doc` file reading needs Microsoft Word in Windows.
- If `.doc` fails, convert to `.docx`/`.pdf`/`.txt`.
- `chroma_db` is persistent, so indexed data remains after restart.
