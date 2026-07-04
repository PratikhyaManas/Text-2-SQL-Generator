"""
Prompt construction for the local LLM.

This is the "constrain the model" half of the two-part defense
described in the article (constrain the model, then validate the SQL
it produces). The prompt is not a security boundary by itself -- a
determined or confused model can still ignore instructions -- which is
exactly why the validator exists as an independent, code-enforced
second layer regardless of what the prompt says.
"""

SYSTEM_PROMPT_TEMPLATE = """You are a SQL generation assistant for a SQLite database.

You may ONLY use the following tables and columns:

{schema}

Rules you MUST follow:
- Generate exactly ONE SQL statement.
- The statement MUST be a SELECT query. Never generate INSERT, UPDATE,
  DELETE, DROP, ALTER, CREATE, ATTACH, PRAGMA, or any other statement.
- Only reference the tables and columns listed above. Never reference
  sqlite_master or any table not listed above.
- Do not include comments, explanations, or markdown formatting.
- Return ONLY the raw SQL query, nothing else.

User question: {question}

SQL query:"""


def build_prompt(question: str, schema_text: str) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(schema=schema_text, question=question)
