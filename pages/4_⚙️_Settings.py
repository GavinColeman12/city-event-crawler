import streamlit as st
from src.config import load_config, save_config

config = load_config()

st.set_page_config(page_title="Settings", page_icon="⚙️", layout="wide")


st.title("⚙️ Settings")

# Greeting message
st.subheader("Greeting Message")
greeting = st.text_area(
    "Shown to employees when they first open the chat",
    value=config.get("chat", {}).get("greeting", ""),
    height=120,
)

# Suggested questions
st.subheader("Suggested Questions")
st.caption("One question per line")
current_questions = config.get("chat", {}).get("suggested_questions", [])
questions_text = st.text_area(
    "Questions shown as quick-access buttons",
    value="\n".join(current_questions),
    height=200,
)

# Sensitive topics
st.subheader("Sensitive Topics")
st.caption("One keyword per line. Queries matching these trigger the HR escalation message.")
current_topics = config.get("chat", {}).get("sensitive_topics", [])
topics_text = st.text_area(
    "Sensitive topic keywords",
    value="\n".join(current_topics),
    height=200,
)

# Retrieval settings
st.subheader("Retrieval Settings")
col1, col2 = st.columns(2)
top_k = col1.number_input(
    "Top K results",
    min_value=1, max_value=20,
    value=config.get("chat", {}).get("retrieval", {}).get("top_k", 5),
)
threshold = col2.slider(
    "Similarity threshold",
    min_value=0.0, max_value=1.0,
    value=float(config.get("chat", {}).get("retrieval", {}).get("similarity_threshold", 0.3)),
    step=0.05,
)

# Admin password
st.subheader("Change Admin Password")
new_password = st.text_input("New admin password", type="password")
confirm_password = st.text_input("Confirm new password", type="password")

st.markdown("---")

# Save
if st.button("💾 Save Settings", type="primary"):
    config["chat"]["greeting"] = greeting
    config["chat"]["suggested_questions"] = [q.strip() for q in questions_text.strip().split("\n") if q.strip()]
    config["chat"]["sensitive_topics"] = [t.strip() for t in topics_text.strip().split("\n") if t.strip()]
    config["chat"]["retrieval"]["top_k"] = top_k
    config["chat"]["retrieval"]["similarity_threshold"] = threshold

    if new_password:
        if new_password == confirm_password:
            config["auth"]["admin_password"] = new_password
        else:
            st.error("Passwords don't match.")
            st.stop()

    save_config(config)
    st.success("Settings saved!")
