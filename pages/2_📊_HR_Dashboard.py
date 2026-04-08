import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from src.config import load_config
from src.analytics import get_dashboard_data, get_raw_queries, get_all_categories

config = load_config()

st.set_page_config(page_title="HR Dashboard", page_icon="📊", layout="wide")

st.title("📊 HR Analytics Dashboard")

# ── Global Filters (sidebar) ─────────────────────────────────────────────────
st.sidebar.header("🔍 Global Filters")

days = st.sidebar.selectbox(
    "Time Range",
    [7, 14, 30, 60, 90],
    index=2,
    format_func=lambda x: f"Last {x} days",
)

all_categories = get_all_categories()
selected_categories = st.sidebar.multiselect(
    "Categories",
    options=[c.title() for c in all_categories],
    default=[c.title() for c in all_categories],
    help="Filter all charts by topic category",
)
selected_categories_lower = [c.lower() for c in selected_categories]

sensitivity_filter = st.sidebar.radio(
    "Query Sensitivity",
    ["All", "Sensitive Only", "Non-Sensitive Only"],
    index=0,
)

answer_filter = st.sidebar.radio(
    "Answer Status",
    ["All", "Answered Only", "Unanswered Only"],
    index=0,
)

# ── Load & filter raw data ────────────────────────────────────────────────────
raw = get_raw_queries(days=days)
dashboard_data = get_dashboard_data(days=days)

if raw:
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["date"] = df["timestamp"].dt.date

    # Apply global filters
    df = df[df["category"].isin(selected_categories_lower)]

    if sensitivity_filter == "Sensitive Only":
        df = df[df["was_sensitive"] == 1]
    elif sensitivity_filter == "Non-Sensitive Only":
        df = df[df["was_sensitive"] == 0]

    if answer_filter == "Answered Only":
        df = df[df["was_answered"] == 1]
    elif answer_filter == "Unanswered Only":
        df = df[df["was_answered"] == 0]
else:
    df = pd.DataFrame()

# ── Key Metrics ───────────────────────────────────────────────────────────────
total = len(df)
answered_count = len(df[df["was_answered"] == 1]) if not df.empty else 0
answer_rate = round(answered_count / total * 100, 1) if total > 0 else 0
sensitive_count = len(df[df["was_sensitive"] == 1]) if not df.empty else 0
avg_response = round(df["response_time_ms"].mean()) if not df.empty and total > 0 else 0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Questions", f"{total:,}")
col2.metric("Answer Rate", f"{answer_rate}%")
col3.metric("Sensitive Queries", f"{sensitive_count:,}")
col4.metric("Avg Response Time", f"{avg_response:,}ms")

st.markdown("---")

# ── Questions Over Time ──────────────────────────────────────────────────────
st.subheader("📈 Questions Over Time")

if not df.empty:
    time_col1, time_col2 = st.columns([3, 1])

    with time_col2:
        time_group = st.radio("Group by", ["Day", "Week"], key="time_group", horizontal=True)
        time_split = st.checkbox("Split by category", value=False, key="time_split")
        time_show_sensitive = st.checkbox("Overlay sensitive trend", value=False, key="time_sensitive")

    with time_col1:
        if time_group == "Week":
            df_time = df.copy()
            df_time["period"] = df_time["timestamp"].dt.to_period("W").apply(lambda r: r.start_time)
        else:
            df_time = df.copy()
            df_time["period"] = df_time["date"]

        if time_split:
            pivot = df_time.groupby(["period", "category"]).size().unstack(fill_value=0)
            pivot.columns = [c.title() for c in pivot.columns]
            st.line_chart(pivot)
        else:
            daily = df_time.groupby("period").size().rename("Questions")
            chart_df = pd.DataFrame(daily)

            if time_show_sensitive:
                sens_daily = df_time[df_time["was_sensitive"] == 1].groupby("period").size().rename("Sensitive")
                chart_df = chart_df.join(sens_daily, how="left").fillna(0)

            st.line_chart(chart_df)
else:
    st.info("No data for the selected filters.")

st.markdown("---")

# ── Category Breakdown ───────────────────────────────────────────────────────
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("📊 Category Breakdown")

    if not df.empty:
        cat_metric = st.radio(
            "Show",
            ["Question Count", "Answer Rate %", "Avg Response Time (ms)"],
            key="cat_metric",
            horizontal=True,
        )

        cat_group = df.groupby("category").agg(
            count=("query_text", "size"),
            answered=("was_answered", "sum"),
            avg_ms=("response_time_ms", "mean"),
        ).reset_index()
        cat_group["category"] = cat_group["category"].str.title()
        cat_group["answer_rate"] = (cat_group["answered"] / cat_group["count"] * 100).round(1)
        cat_group["avg_ms"] = cat_group["avg_ms"].round(0)

        if cat_metric == "Question Count":
            st.bar_chart(cat_group.set_index("category")["count"])
        elif cat_metric == "Answer Rate %":
            st.bar_chart(cat_group.set_index("category")["answer_rate"])
        else:
            st.bar_chart(cat_group.set_index("category")["avg_ms"])
    else:
        st.info("No data for the selected filters.")

with col_right:
    st.subheader("🔝 Top Questions")

    if not df.empty:
        top_n = st.slider("Show top N", min_value=5, max_value=25, value=10, key="top_n")
        top_cat_filter = st.selectbox(
            "Filter by category",
            ["All Categories"] + [c.title() for c in selected_categories_lower],
            key="top_cat",
        )

        df_top = df.copy()
        if top_cat_filter != "All Categories":
            df_top = df_top[df_top["category"] == top_cat_filter.lower()]

        top_qs = df_top.groupby("query_text").size().reset_index(name="Count")
        top_qs = top_qs.sort_values("Count", ascending=False).head(top_n)
        top_qs.columns = ["Question", "Count"]
        top_qs.index = range(1, len(top_qs) + 1)
        st.dataframe(top_qs, use_container_width=True)

        csv_top = top_qs.to_csv(index=False)
        st.download_button("📥 Download Top Questions CSV", csv_top, "top_questions.csv", "text/csv", key="dl_top")
    else:
        st.info("No data for the selected filters.")

st.markdown("---")

# ── Unanswered Questions (Content Gaps) ──────────────────────────────────────
st.subheader("⚠️ Unanswered Questions (Content Gaps)")
st.caption("Questions employees asked that the bot couldn't answer — tells you exactly what policies are missing.")

if not df.empty:
    unans_col1, unans_col2 = st.columns([3, 1])

    with unans_col2:
        unans_min_count = st.number_input("Min times asked", min_value=1, value=1, key="unans_min")
        unans_category = st.selectbox(
            "Category",
            ["All Categories"] + [c.title() for c in selected_categories_lower],
            key="unans_cat",
        )
        unans_sort = st.radio("Sort by", ["Times Asked", "Most Recent"], key="unans_sort")

    with unans_col1:
        df_unans = df[df["was_answered"] == 0].copy()
        if unans_category != "All Categories":
            df_unans = df_unans[df_unans["category"] == unans_category.lower()]

        if not df_unans.empty:
            unans_grouped = df_unans.groupby("query_text").agg(
                count=("query_text", "size"),
                last_asked=("timestamp", "max"),
                category=("category", "first"),
            ).reset_index()
            unans_grouped = unans_grouped[unans_grouped["count"] >= unans_min_count]

            if unans_sort == "Times Asked":
                unans_grouped = unans_grouped.sort_values("count", ascending=False)
            else:
                unans_grouped = unans_grouped.sort_values("last_asked", ascending=False)

            unans_display = unans_grouped[["query_text", "count", "category", "last_asked"]].copy()
            unans_display.columns = ["Question", "Times Asked", "Category", "Last Asked"]
            unans_display["Category"] = unans_display["Category"].str.title()
            unans_display["Last Asked"] = unans_display["Last Asked"].dt.strftime("%Y-%m-%d %H:%M")
            unans_display.index = range(1, len(unans_display) + 1)
            st.dataframe(unans_display, use_container_width=True)

            csv_unans = unans_display.to_csv(index=False)
            st.download_button("📥 Download Content Gaps CSV", csv_unans, "content_gaps.csv", "text/csv", key="dl_unans")
        else:
            st.success("No unanswered questions for these filters!")
else:
    st.info("No data for the selected filters.")

st.markdown("---")

# ── Sensitive Topics Deep Dive ───────────────────────────────────────────────
st.subheader("🔒 Sensitive Topics Analysis")
st.caption("Volume trends for sensitive queries — helps HR identify emerging concerns without exposing individual questions.")

if not df.empty:
    sens_col1, sens_col2 = st.columns([3, 1])

    with sens_col2:
        sens_view = st.radio("View", ["Trend Over Time", "By Category", "Answered vs Unanswered"], key="sens_view")

    with sens_col1:
        df_sens = df[df["was_sensitive"] == 1].copy()

        if not df_sens.empty:
            if sens_view == "Trend Over Time":
                sens_daily = df_sens.groupby("date").size().rename("Sensitive Queries")
                st.line_chart(sens_daily)

            elif sens_view == "By Category":
                sens_cat = df_sens.groupby("category").size().reset_index(name="Count")
                sens_cat["category"] = sens_cat["category"].str.title()
                st.bar_chart(sens_cat.set_index("category"))

            else:  # Answered vs Unanswered
                sens_answer = df_sens.groupby("was_answered").size().reset_index(name="Count")
                sens_answer["Status"] = sens_answer["was_answered"].map({1: "Answered from Docs", 0: "Deferred to HR"})
                st.bar_chart(sens_answer.set_index("Status")["Count"])
        else:
            st.info("No sensitive queries for these filters.")
else:
    st.info("No data for the selected filters.")

st.markdown("---")

# ── Response Performance ─────────────────────────────────────────────────────
st.subheader("⚡ Response Performance")

if not df.empty:
    perf_col1, perf_col2 = st.columns([3, 1])

    with perf_col2:
        perf_view = st.radio("View", ["By Category", "Over Time"], key="perf_view")

    with perf_col1:
        if perf_view == "By Category":
            perf_cat = df.groupby("category")["response_time_ms"].agg(["mean", "median", "max"]).round(0)
            perf_cat.index = [c.title() for c in perf_cat.index]
            perf_cat.columns = ["Avg (ms)", "Median (ms)", "Max (ms)"]
            st.dataframe(perf_cat, use_container_width=True)
        else:
            perf_daily = df.groupby("date")["response_time_ms"].mean().round(0).rename("Avg Response (ms)")
            st.line_chart(perf_daily)
else:
    st.info("No data for the selected filters.")

st.markdown("---")

# ── Raw Query Log ─────────────────────────────────────────────────────────────
with st.expander("📋 Full Query Log (click to expand)"):
    if not df.empty:
        log_search = st.text_input("Search queries", placeholder="Type to filter...", key="log_search")

        df_log = df[["timestamp", "query_text", "category", "was_answered", "was_sensitive", "response_time_ms"]].copy()
        df_log.columns = ["Timestamp", "Question", "Category", "Answered", "Sensitive", "Response (ms)"]
        df_log["Category"] = df_log["Category"].str.title()
        df_log["Answered"] = df_log["Answered"].map({1: "✅", 0: "❌"})
        df_log["Sensitive"] = df_log["Sensitive"].map({1: "⚠️", 0: ""})
        df_log["Timestamp"] = df_log["Timestamp"].dt.strftime("%Y-%m-%d %H:%M")

        if log_search:
            df_log = df_log[df_log["Question"].str.contains(log_search, case=False, na=False)]

        st.dataframe(df_log, use_container_width=True, height=400)

        csv_log = df_log.to_csv(index=False)
        st.download_button("📥 Download Full Log CSV", csv_log, "query_log.csv", "text/csv", key="dl_log")
    else:
        st.info("No queries logged yet.")
