"""
Client for a locally running Ollama server.

Running the model locally (rather than calling a hosted API) is the
core privacy/security property of this system: the natural-language
question and the database schema never leave the machine.
"""

import re

import requests

from src.core.logger import logger
from src.prompts.templates import build_prompt


class LLMConnectionError(Exception):
    pass


_CODE_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_sql(raw_response: str) -> str:
    """Strip markdown code fences and surrounding chatter from the model output."""
    match = _CODE_FENCE_RE.search(raw_response)
    text = match.group(1) if match else raw_response
    text = text.strip()

    # If the model added an explanation after the query, keep only the
    # first statement-looking chunk up to the first semicolon/newline gap.
    text = text.split("\n\n")[0].strip()
    return text


class OllamaClient:
    def __init__(self, host: str, model: str, timeout_seconds: int = 30):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate_sql(self, question: str, schema_text: str) -> str:
        prompt = build_prompt(question, schema_text)

        try:
            response = requests.post(
                f"{self.host}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to reach Ollama at {self.host}: {e}")
            raise LLMConnectionError(
                f"Could not reach local Ollama server at {self.host}. "
                f"Is `ollama serve` running and is the model pulled? ({e})"
            )

        data = response.json()
        raw_text = data.get("response", "")
        return _extract_sql(raw_text)
