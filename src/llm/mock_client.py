"""
A tiny rule-based stand-in for a real local LLM.

This lets the entire pipeline (prompt -> "generation" -> validation ->
safe execution -> audit) be demoed and tested end-to-end without
requiring Ollama or any model weights to be installed. Swap
`llm_provider: mock` for `llm_provider: ollama` in configs/config.yaml
to use a real local model instead -- nothing else in the pipeline
changes, since both clients expose the same `generate_sql()` interface.
"""

import re


class MockLLMClient:
    """Deterministic keyword-matching 'model' for the sample e-commerce schema."""

    def generate_sql(self, question: str, schema_text: str) -> str:
        q = question.lower()

        if "how many customers" in q or "count of customers" in q:
            return "SELECT COUNT(*) AS customer_count FROM customers;"

        if "how many orders" in q or "count of orders" in q:
            return "SELECT COUNT(*) AS order_count FROM orders;"

        if "top" in q and "product" in q:
            n = self._extract_number(q, default=5)
            return (
                "SELECT p.name, SUM(oi.quantity) AS total_sold "
                "FROM order_items oi "
                "JOIN products p ON p.product_id = oi.product_id "
                "GROUP BY p.name "
                f"ORDER BY total_sold DESC LIMIT {n};"
            )

        if "customer" in q and ("email" in q or "list" in q):
            return "SELECT customer_id, name, email FROM customers LIMIT 20;"

        if "revenue" in q or "total sales" in q:
            return (
                "SELECT SUM(oi.quantity * p.price) AS total_revenue "
                "FROM order_items oi "
                "JOIN products p ON p.product_id = oi.product_id;"
            )

        if "drop" in q or "delete" in q or "update" in q or "insert" in q:
            # Deliberately "misbehave" like an unconstrained model might,
            # so the validator has something real to catch in a demo.
            return "DROP TABLE customers;"

        # Generic fallback: a safe, harmless default query.
        return "SELECT * FROM products LIMIT 10;"

    @staticmethod
    def _extract_number(text: str, default: int) -> int:
        match = re.search(r"\btop\s+(\d+)\b", text)
        return int(match.group(1)) if match else default
