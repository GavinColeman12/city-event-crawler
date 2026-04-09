import streamlit as st
from pathlib import Path
from src.config import load_config
from src.vectorstore import get_collection

config = load_config()

# Auto-ingest sample data if vector store is empty (first run / cloud deploy)
client_id = config.get("client", {}).get("id")
collection = get_collection(client_id)
if collection.count() == 0:
    from src.parsers import parse_file, SUPPORTED_EXTENSIONS
    from src.chunker import chunk_documents
    from src.vectorstore import add_chunks

    sample_dir = Path(__file__).parent / "sample_data"
    if sample_dir.exists():
        for f in sorted(sample_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                docs = parse_file(str(f.resolve()))
                chunks = chunk_documents(docs)
                add_chunks(chunks, client_id)

st.set_page_config(
    page_title=f"{config['client']['name']} — HR Assistant",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Apply theme colors
primary = config.get("theme", {}).get("primary_color", "#1B6B4A")
accent = config.get("theme", {}).get("accent_color", "#2ECC71")

st.markdown(f"""
<style>
    .stApp {{
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }}
    [data-testid="stSidebar"] {{
        background-color: #f8f9fa;
    }}
    .stButton > button {{
        border-color: {primary};
        color: {primary};
    }}
    .stButton > button:hover {{
        background-color: {primary};
        color: white;
        border-color: {primary};
    }}
</style>
""", unsafe_allow_html=True)

st.sidebar.title(f"💼 {config['client']['name']}")
st.sidebar.markdown("**HR Policy Assistant**")
st.sidebar.markdown("---")

st.title(f"Welcome to {config['client']['name']} HR Assistant")
st.markdown("""
Navigate using the sidebar to:
- **Ask a Question** — Get instant answers about company policies
- **HR Dashboard** — View analytics and content gaps (admin)
- **Manage Policies** — Upload and manage policy documents (admin)
- **Settings** — Configure the assistant (admin)
""")
