import streamlit as st
import os
import tempfile
from pathlib import Path
from src.config import load_config
from src.vectorstore import list_documents, delete_by_source
from src.parsers import parse_file, SUPPORTED_EXTENSIONS
from src.chunker import chunk_documents
from src.vectorstore import add_chunks

config = load_config()
client_id = config.get("client", {}).get("id")

st.set_page_config(page_title="Manage Policies", page_icon="📁", layout="wide")


st.title("📁 Manage Policy Documents")

# Upload section
st.subheader("Upload New Policy Document")
extensions = [ext.replace(".", "") for ext in SUPPORTED_EXTENSIONS]
uploaded_file = st.file_uploader(
    f"Supported formats: {', '.join(extensions)}",
    type=extensions,
)

if uploaded_file:
    if st.button("Ingest Document"):
        with st.spinner("Processing document..."):
            # Save to temp file
            suffix = Path(uploaded_file.name).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(uploaded_file.read())
                tmp_path = tmp.name

            try:
                # Parse, chunk, embed
                documents = parse_file(tmp_path)
                if not documents:
                    st.error("No content could be extracted from this file.")
                else:
                    # Use original filename as source
                    for doc in documents:
                        doc["metadata"]["source"] = uploaded_file.name

                    chunks = chunk_documents(documents)

                    # Fix chunk IDs to use original filename
                    for chunk in chunks:
                        chunk["metadata"]["source"] = uploaded_file.name

                    count = add_chunks(chunks, client_id)
                    st.success(f"Successfully ingested **{uploaded_file.name}** — {count} chunks created.")
            finally:
                os.unlink(tmp_path)

st.markdown("---")

# Existing documents
st.subheader("Ingested Documents")

docs = list_documents(client_id)
if docs:
    for source, chunk_count in sorted(docs.items()):
        col1, col2, col3 = st.columns([4, 1, 1])
        display_name = os.path.basename(source)
        col1.markdown(f"📄 **{display_name}**")
        col2.markdown(f"{chunk_count} chunks")
        if col3.button("🗑️ Delete", key=f"del_{source}"):
            deleted = delete_by_source(source, client_id)
            st.success(f"Deleted {deleted} chunks for {display_name}")
            st.rerun()
else:
    st.info("No documents ingested yet. Upload a document above or run `python ingest.py --path sample_data/`")
