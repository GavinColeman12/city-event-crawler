import streamlit as st
from src.config import load_config

config = load_config()

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
