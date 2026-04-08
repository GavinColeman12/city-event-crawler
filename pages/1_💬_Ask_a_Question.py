import streamlit as st
from src.config import load_config
from src.generator import generate_response

config = load_config()

st.set_page_config(page_title="Ask a Question", page_icon="💬", layout="centered")

# Custom CSS for chat
primary = config.get("theme", {}).get("primary_color", "#1B6B4A")
st.markdown(f"""
<style>
    .sensitive-box {{
        background-color: #fff3cd;
        border: 1px solid #ffc107;
        border-radius: 8px;
        padding: 12px;
        margin-top: 8px;
    }}
    .source-box {{
        background-color: #f8f9fa;
        border: 1px solid #dee2e6;
        border-radius: 8px;
        padding: 10px;
        font-size: 0.85em;
    }}
</style>
""", unsafe_allow_html=True)

st.title("💬 Ask a Question")

# Initialize chat history
if "messages" not in st.session_state:
    st.session_state.messages = []
if "greeted" not in st.session_state:
    st.session_state.greeted = False

# Show greeting on first load
if not st.session_state.greeted:
    greeting = config.get("chat", {}).get("greeting", "Hi! How can I help?")
    st.markdown(greeting)

    # Suggested questions
    suggested = config.get("chat", {}).get("suggested_questions", [])
    if suggested:
        st.markdown("**Quick questions:**")
        cols = st.columns(2)
        for i, question in enumerate(suggested):
            col = cols[i % 2]
            if col.button(question, key=f"suggest_{i}", use_container_width=True):
                st.session_state.messages.append({"role": "user", "content": question})
                st.session_state.greeted = True
                st.rerun()

    st.session_state.greeted = True

# Display chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and msg.get("sources"):
            with st.expander("📄 Policy Sources"):
                for src in msg["sources"]:
                    st.markdown(f"**{src['document']}** (relevance: {src['score']:.0%})")
                    st.caption(src["text"])
                    st.markdown("---")

# Chat input
placeholder = config.get("chat", {}).get("placeholder", "Ask about company policies...")
if prompt := st.chat_input(placeholder):
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Looking up our policies..."):
            # Build conversation history for context
            history = []
            for msg in st.session_state.messages[:-1]:
                if msg["role"] in ("user", "assistant"):
                    history.append({"role": msg["role"], "content": msg["content"]})

            result = generate_response(prompt, conversation_history=history, config=config)

        st.markdown(result["response"])

        if result["sources"]:
            with st.expander("📄 Policy Sources"):
                for src in result["sources"]:
                    st.markdown(f"**{src['document']}** (relevance: {src['score']:.0%})")
                    st.caption(src["text"])
                    st.markdown("---")

    st.session_state.messages.append({
        "role": "assistant",
        "content": result["response"],
        "sources": result["sources"],
    })
