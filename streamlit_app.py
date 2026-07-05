import os
import json
from typing import Any, Dict, List

import requests
import streamlit as st  # type: ignore[import-not-found]


DEFAULT_API_BASE_URL = os.getenv("TEXT2SQL_API_BASE_URL", "http://localhost:8000")


@st.cache_data(ttl=30)
def fetch_databases(api_base_url: str) -> Dict[str, str]:
    response = requests.get(f"{api_base_url}/databases", timeout=10)
    response.raise_for_status()
    payload = response.json()
    return payload.get("databases", {})


@st.cache_data(ttl=30)
def fetch_schema(api_base_url: str) -> Dict[str, List[str]]:
    response = requests.get(f"{api_base_url}/schema", timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_history(api_base_url: str, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": limit}
    if user_id.strip():
        params["user_id"] = user_id.strip()

    response = requests.get(f"{api_base_url}/history", params=params, timeout=10)
    response.raise_for_status()
    payload = response.json()
    return payload.get("history", [])


def ask_question(api_base_url: str, question: str, user_id: str, database_name: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "question": question,
        "user_id": user_id.strip() or None,
        "database_name": database_name,
    }
    response = requests.post(f"{api_base_url}/query", json=payload, timeout=60)
    response.raise_for_status()
    return response.json()


def ask_question_stream(
    api_base_url: str,
    question: str,
    user_id: str,
    database_name: str,
) -> tuple[Dict[str, Any], List[str]]:
    payload: Dict[str, Any] = {
        "question": question,
        "user_id": user_id.strip() or None,
        "database_name": database_name,
    }
    stages: List[str] = []
    result: Dict[str, Any] = {}

    with requests.post(
        f"{api_base_url}/query/stream",
        json=payload,
        timeout=60,
        stream=True,
        headers={"Accept": "text/event-stream"},
    ) as response:
        response.raise_for_status()
        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if not line or not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            event_type = payload.get("type")
            if event_type == "progress":
                stage = payload.get("stage", "working")
                stages.append(stage)
            elif event_type == "result":
                result = payload.get("data", {})

    return result, stages


def render_result_table(columns: List[str], rows: List[List[Any]]) -> None:
    if not columns or not rows:
        st.info("No result rows returned.")
        return

    records: List[Dict[str, Any]] = []
    for row in rows:
        record = {}
        for index, column in enumerate(columns):
            record[column] = row[index] if index < len(row) else None
        records.append(record)

    st.dataframe(records, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Text2SQL Frontend", page_icon="🧠", layout="wide")

    st.title("Secure Text-to-SQL")
    st.caption("Standalone Streamlit frontend for the existing FastAPI backend.")

    with st.sidebar:
        st.header("Connection")
        api_base_url = st.text_input("API base URL", value=DEFAULT_API_BASE_URL)
        user_id = st.text_input("User ID", value="demo")
        use_streaming = st.toggle("Streaming query progress", value=True)
        history_limit = st.slider("History limit", min_value=5, max_value=50, value=10, step=5)

        st.header("Schema")
        if st.button("Refresh schema", use_container_width=True):
            fetch_schema.clear()
            fetch_databases.clear()

    databases: Dict[str, str] = {}
    try:
        databases = fetch_databases(api_base_url)
    except requests.RequestException as exc:
        st.error(f"Could not load databases from API: {exc}")

    database_names = list(databases.keys()) or ["default"]
    selected_database = st.selectbox("Database", options=database_names, index=0)
    if selected_database in databases:
        st.caption(f"Selected database path: {databases[selected_database]}")

    question = st.text_area(
        "Question",
        placeholder="Ask a question about the data, for example: top 3 products by quantity sold",
        height=120,
    )

    submit = st.button("Run Query", type="primary", use_container_width=True)

    if submit:
        if not question.strip():
            st.warning("Please enter a question before running the query.")
        else:
            try:
                stages: List[str] = []
                if use_streaming:
                    with st.status("Running query", expanded=True) as status:
                        result, stages = ask_question_stream(api_base_url, question, user_id, selected_database)
                        if not stages:
                            status.write("working")
                        for stage in stages:
                            status.write(stage)
                        status.update(label="Query finished", state="complete", expanded=False)
                else:
                    result = ask_question(api_base_url, question, user_id, selected_database)
            except requests.RequestException as exc:
                st.error(f"Query failed: {exc}")
            else:
                st.subheader("Result")
                status = result.get("status", "unknown")
                st.write(f"Status: {status}")

                explanation = result.get("explanation")
                if explanation:
                    st.info(explanation)

                confidence = result.get("confidence")
                if confidence is not None:
                    st.write(f"Confidence: {confidence}")

                sql_text = result.get("safe_sql") or result.get("generated_sql")
                if sql_text:
                    st.code(sql_text, language="sql")

                reason = result.get("reason")
                if reason:
                    st.warning(reason)

                summary = result.get("summary")
                if summary:
                    st.success(summary)

                render_result_table(result.get("columns", []), result.get("rows", []))

    st.divider()
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Recent History")
        try:
            history_rows = fetch_history(api_base_url, user_id, limit=history_limit)
        except requests.RequestException as exc:
            st.error(f"Could not fetch history: {exc}")
        else:
            if not history_rows:
                st.info("No history entries yet.")
            else:
                st.dataframe(history_rows, use_container_width=True)

    with col_right:
        st.subheader("Allowed Schema")
        try:
            schema = fetch_schema(api_base_url)
        except requests.RequestException as exc:
            st.error(f"Could not fetch schema: {exc}")
        else:
            st.json(schema)


if __name__ == "__main__":
    main()
