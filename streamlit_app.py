import os
import json
import csv
import io
from pathlib import Path
from typing import Any, Dict, List

import requests
import streamlit as st  # type: ignore[import-not-found]


DEFAULT_API_BASE_URL = os.getenv("TEXT2SQL_API_BASE_URL", "http://localhost:8000")
SAVED_QUERIES_PATH = Path(".saved_queries.json")

# Set to "minimal" to remove icons globally, or "expressive" to enable them.
ICON_STYLE = "expressive"


def ui_text(text: str, icon: str = "") -> str:
    if ICON_STYLE == "expressive" and icon:
        return f"{icon} {text}"
    return text


def ui_stage(stage: str) -> str:
    if ICON_STYLE == "expressive":
        return f"➡️ {stage}"
    return stage


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


def preview_question(api_base_url: str, question: str, user_id: str, database_name: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "question": question,
        "user_id": user_id.strip() or None,
        "database_name": database_name,
    }
    response = requests.post(f"{api_base_url}/query/preview", json=payload, timeout=60)
    response.raise_for_status()
    return response.json()


def execute_approved_question(
    api_base_url: str,
    question: str,
    safe_sql: str,
    user_id: str,
    database_name: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "question": question,
        "safe_sql": safe_sql,
        "user_id": user_id.strip() or None,
        "database_name": database_name,
    }
    response = requests.post(f"{api_base_url}/query/approved", json=payload, timeout=60)
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


def load_saved_queries() -> List[Dict[str, str]]:
    if not SAVED_QUERIES_PATH.exists():
        return []
    try:
        payload = json.loads(SAVED_QUERIES_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]
    except Exception:
        return []


def persist_saved_queries(items: List[Dict[str, str]]) -> None:
    SAVED_QUERIES_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")


def to_csv_text(columns: List[str], rows: List[List[Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue()


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
    page_icon = "🧾" if ICON_STYLE == "expressive" else None
    st.set_page_config(page_title="Text2SQL Frontend", page_icon=page_icon, layout="wide")

    if "saved_queries" not in st.session_state:
        st.session_state["saved_queries"] = load_saved_queries()
    if "latest_preview" not in st.session_state:
        st.session_state["latest_preview"] = None
    if "latest_result" not in st.session_state:
        st.session_state["latest_result"] = None
    if "pending_approval" not in st.session_state:
        st.session_state["pending_approval"] = None
    if "question_input" not in st.session_state:
        st.session_state["question_input"] = ""

    st.title(ui_text("Secure Text-to-SQL", "🛡️"))
    st.caption("Standalone Streamlit frontend for the existing FastAPI backend.")

    with st.sidebar:
        st.header(ui_text("Connection", "🔌"))
        api_base_url = st.text_input(ui_text("API base URL", "🌐"), value=DEFAULT_API_BASE_URL)
        user_id = st.text_input(ui_text("User ID", "👤"), value="demo")
        require_approval = st.toggle(ui_text("Require query approval", "✅"), value=True)
        use_streaming = st.toggle(ui_text("Streaming query progress", "⚡"), value=True)
        history_limit = st.slider(ui_text("History limit", "📈"), min_value=5, max_value=50, value=10, step=5)

        st.header(ui_text("Saved Queries", "💾"))
        saved_items: List[Dict[str, str]] = st.session_state["saved_queries"]
        saved_names = [item.get("name", "unnamed") for item in saved_items]
        selected_saved = st.selectbox(ui_text("Saved query", "🗂️"), options=["(none)"] + saved_names, index=0)
        if st.button(ui_text("Load saved query", "📂"), use_container_width=True):
            if selected_saved != "(none)":
                selected_item = next((item for item in saved_items if item.get("name") == selected_saved), None)
                if selected_item:
                    st.session_state["question_input"] = selected_item.get("question", "")

        st.header(ui_text("Schema", "🧠"))
        if st.button(ui_text("Refresh schema", "🔄"), use_container_width=True):
            fetch_schema.clear()
            fetch_databases.clear()

    databases: Dict[str, str] = {}
    try:
        databases = fetch_databases(api_base_url)
    except requests.RequestException as exc:
        st.error(f"Could not load databases from API: {exc}")

    database_names = list(databases.keys()) or ["default"]
    selected_database = st.selectbox(ui_text("Database", "🛢️"), options=database_names, index=0)
    if selected_database in databases:
        st.caption(f"Selected database path: {databases[selected_database]}")

    question = st.text_area(
        ui_text("Question", "❓"),
        placeholder="Ask a question about the data, for example: top 3 products by quantity sold",
        height=120,
        key="question_input",
    )

    save_name = st.text_input(ui_text("Save current question as", "🏷️"), value="")
    if st.button(ui_text("Save Query", "💾")):
        if not save_name.strip() or not question.strip():
            st.warning("Enter both a query name and question to save.")
        else:
            saved_items = [item for item in st.session_state["saved_queries"] if item.get("name") != save_name.strip()]
            saved_items.append(
                {
                    "name": save_name.strip(),
                    "question": question.strip(),
                    "database_name": selected_database,
                }
            )
            st.session_state["saved_queries"] = saved_items
            persist_saved_queries(saved_items)
            st.success(ui_text("Query saved.", "✅"))

    submit = st.button(ui_text("Run Query", "🚀"), type="primary", use_container_width=True)

    if submit:
        if not question.strip():
            st.warning("Please enter a question before running the query.")
        else:
            try:
                preview = preview_question(api_base_url, question, user_id, selected_database)
                st.session_state["latest_preview"] = preview

                if preview.get("status") != "ready":
                    st.session_state["latest_result"] = None
                    st.session_state["pending_approval"] = None
                elif require_approval:
                    st.session_state["pending_approval"] = {
                        "question": question,
                        "safe_sql": preview.get("safe_sql", ""),
                        "database_name": selected_database,
                    }
                    st.session_state["latest_result"] = None
                else:
                    if use_streaming:
                        with st.status(ui_text("Running query", "⚙️"), expanded=True) as status:
                            result, stages = ask_question_stream(api_base_url, question, user_id, selected_database)
                            if not stages:
                                status.write(ui_text("working", "⏳"))
                            for stage in stages:
                                status.write(ui_stage(stage))
                            status.update(label=ui_text("Query finished", "✅"), state="complete", expanded=False)
                    else:
                        result = execute_approved_question(
                            api_base_url,
                            question,
                            preview.get("safe_sql", ""),
                            user_id,
                            selected_database,
                        )
                    st.session_state["latest_result"] = result
                    st.session_state["pending_approval"] = None
            except requests.RequestException as exc:
                st.error(f"{ui_text('Query failed:', '❌')} {exc}")

    preview = st.session_state.get("latest_preview")
    if preview:
        st.subheader(ui_text("Preview", "🔍"))
        st.write(f"{ui_text('Status:', '📌')} {preview.get('status', 'unknown')}")
        sql_text = preview.get("safe_sql") or preview.get("generated_sql")
        if sql_text:
            st.code(sql_text, language="sql")
        reason = preview.get("reason")
        if reason:
            st.warning(f"{ui_text('', '⚠️')}{' ' if ICON_STYLE == 'expressive' else ''}{reason}")

        plan_warnings = preview.get("plan_warnings", [])
        if plan_warnings:
            st.warning(ui_text("EXPLAIN warnings:", "⚠️") + "\n- " + "\n- ".join(plan_warnings))
        elif preview.get("status") == "ready":
            st.info(ui_text("No EXPLAIN plan warnings detected.", "✅"))

        plan_rows = preview.get("plan_rows", [])
        if plan_rows:
            st.caption(ui_text("Execution plan rows", "🧭"))
            st.dataframe(plan_rows, use_container_width=True)

    pending = st.session_state.get("pending_approval")
    if pending:
        st.info(ui_text("This query is waiting for approval.", "🕒"))
        if st.button(ui_text("Approve and Execute", "✅"), type="secondary", use_container_width=True):
            try:
                result = execute_approved_question(
                    api_base_url,
                    pending.get("question", ""),
                    pending.get("safe_sql", ""),
                    user_id,
                    pending.get("database_name", selected_database),
                )
            except requests.RequestException as exc:
                st.error(f"{ui_text('Approved execution failed:', '❌')} {exc}")
            else:
                st.session_state["latest_result"] = result
                st.session_state["pending_approval"] = None

    result = st.session_state.get("latest_result")
    if result:
        st.subheader(ui_text("Result", "📊"))
        status = result.get("status", "unknown")
        st.write(f"{ui_text('Status:', '📌')} {status}")

        explanation = result.get("explanation")
        if explanation:
            st.info(f"{ui_text('', '🧾')}{' ' if ICON_STYLE == 'expressive' else ''}{explanation}")

        confidence = result.get("confidence")
        if confidence is not None:
            st.write(f"{ui_text('Confidence:', '🎯')} {confidence}")

        sql_text = result.get("safe_sql") or result.get("generated_sql")
        if sql_text:
            st.code(sql_text, language="sql")

        reason = result.get("reason")
        if reason:
            st.warning(f"{ui_text('', '⚠️')}{' ' if ICON_STYLE == 'expressive' else ''}{reason}")

        summary = result.get("summary")
        if summary:
            st.success(f"{ui_text('', '✅')}{' ' if ICON_STYLE == 'expressive' else ''}{summary}")

        columns = result.get("columns", [])
        rows = result.get("rows", [])
        render_result_table(columns, rows)
        if columns and rows:
            csv_text = to_csv_text(columns, rows)
            st.download_button(
                label=ui_text("Download Result CSV", "⬇️"),
                data=csv_text,
                file_name="text2sql_result.csv",
                mime="text/csv",
            )

    st.divider()
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader(ui_text("Recent History", "🕘"))
        try:
            history_rows = fetch_history(api_base_url, user_id, limit=history_limit)
        except requests.RequestException as exc:
            st.error(f"{ui_text('Could not fetch history:', '❌')} {exc}")
        else:
            if not history_rows:
                st.info(ui_text("No history entries yet.", "ℹ️"))
            else:
                st.dataframe(history_rows, use_container_width=True)

    with col_right:
        st.subheader(ui_text("Allowed Schema", "📚"))
        try:
            schema = fetch_schema(api_base_url)
        except requests.RequestException as exc:
            st.error(f"{ui_text('Could not fetch schema:', '❌')} {exc}")
        else:
            st.json(schema)


if __name__ == "__main__":
    main()
