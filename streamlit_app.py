import os
import pandas as pd
import streamlit as st
from google.cloud import bigquery

st.set_page_config(page_title="Event Window Viewer", layout="wide")

# ---------------------------------
# BigQuery client
# ---------------------------------
@st.cache_resource
def get_bq_client():
    return bigquery.Client(project="dazn-data-analyti-prod-4a54d8")

client = get_bq_client()

# ---------------------------------
# Load one event into a dataframe
# ---------------------------------
@st.cache_data(ttl=300)
def load_event_timeline(article_id: str) -> pd.DataFrame:
    query = f"""
    SELECT
      TIMESTAMP_TRUNC(TIMESTAMP_MICROS(event_timestamp), MINUTE) AS minute_ts,
      CASE
        WHEN LOWER(application_type) = 'ios' THEN 'iOS'
        WHEN LOWER(application_type) IN ('android', 'android mobile app') THEN 'Android'
        WHEN LOWER(application_type) IN ('web', 'web.hybrid.2') THEN 'Web'
        WHEN LOWER(application_type) LIKE 'lr_html_%'
             OR application_type IN ('tvos', 'androidtv', 'roku') THEN 'Living Room'
        ELSE 'Other'
      END AS platform_group,
      event_name,
      COUNT(*) AS row_count,
      COUNT(DISTINCT viewer_id) AS distinct_viewers,
      COUNT(DISTINCT user_pseudo_id) AS distinct_pseudos
    FROM `dazn-data-analyti-prod-4a54d8.analytics_flattened_prod.ga4_events_flattened_prod`
    WHERE article_id = '{article_id}'
      AND event_name IN (
        'public_watch_party_available',
        'public_watch_party_action',
        'message_impression',
        'message_click',
        'reaction_click',
        'pin_message_action',
        'poll_impression',
        'poll_vote',
        'quiz_question_impression',
        'quiz_answer_selection',
        'gamification_impression',
        'gamification_action',
        'social_sharing_click'
      )
    GROUP BY 1, 2, 3
    ORDER BY 1, 2, 3
    """
    return client.query(query).to_dataframe()

# ---------------------------------
# Detect main event window
# ---------------------------------
def detect_main_event_window(df: pd.DataFrame):
    work = df.copy()
    work["minute_ts"] = pd.to_datetime(work["minute_ts"], utc=True)
    work["bucket_ts"] = work["minute_ts"].dt.floor("5min")

    total_5m = (
        work.groupby("bucket_ts", as_index=False)
        .agg(total_rows=("row_count", "sum"))
        .sort_values("bucket_ts")
    )

    if total_5m.empty:
        raise ValueError("No rows available for this event.")

    full_index = pd.date_range(
        start=total_5m["bucket_ts"].min(),
        end=total_5m["bucket_ts"].max(),
        freq="5min",
        tz="UTC",
    )

    signal = (
        total_5m.set_index("bucket_ts")
        .reindex(full_index, fill_value=0)
        .rename_axis("bucket_ts")
    )

    signal["smoothed"] = signal["total_rows"].rolling(3, min_periods=1, center=True).mean()

    peak_ts = signal["smoothed"].idxmax()
    peak_val = float(signal["smoothed"].max())
    threshold = max(20, peak_val * 0.25)

    y = signal["smoothed"].values
    idx = signal.index
    peak_i = list(idx).index(peak_ts)

    left = peak_i
    while left > 0 and y[left - 1] >= threshold:
        left -= 1

    right = peak_i
    while right < len(y) - 1 and y[right + 1] >= threshold:
        right += 1

    main_start = idx[left] - pd.Timedelta(minutes=30)
    main_end = idx[right] + pd.Timedelta(minutes=35)

    reporting_start = total_5m["bucket_ts"].min()
    reporting_end = total_5m["bucket_ts"].max()

    main_start = max(main_start, reporting_start)
    main_end = min(main_end, reporting_end)

    return main_start, main_end, peak_ts, peak_val

# ---------------------------------
# Streamlit UI
# ---------------------------------
st.title("Main Event Window Viewer")

article_id = st.text_input(
    "Article ID",
    value="yvq4i0runha709fohj32ppz4e0"
)

if st.button("Load event"):
    with st.spinner("Querying BigQuery..."):
        df = load_event_timeline(article_id)

    st.write(f"Rows returned: {len(df):,}")

    if df.empty:
        st.warning("No rows returned for this article_id.")
    else:
        try:
            main_start, main_end, peak_ts, peak_val = detect_main_event_window(df)

            st.subheader("Detected main event window")
            c1, c2, c3, c4 = st.columns(4)

            c1.metric("Start", main_start.strftime("%Y-%m-%d %H:%M UTC"))
            c2.metric("End", main_end.strftime("%Y-%m-%d %H:%M UTC"))
            c3.metric("Peak", peak_ts.strftime("%Y-%m-%d %H:%M UTC"))
            c4.metric("Window hours", f"{(main_end - main_start).total_seconds() / 3600:.2f}")

            st.subheader("Preview")
            st.dataframe(df.head(20), use_container_width=True)

        except Exception as e:
            st.error(f"Could not detect main event window: {e}")
