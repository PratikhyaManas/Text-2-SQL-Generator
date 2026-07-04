"""
Append-only audit trail.

Every call into the pipeline is recorded here -- whether it succeeded,
was blocked by the validator, or errored during execution. This is
what "logs and audits queries" means in practice: a security review
should never have to trust in-memory state or scrollback logs, it
should be able to read a durable, structured record of exactly what
was asked, what SQL was generated, whether it was allowed to run, and
what happened when it did.
"""

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from typing import List, Optional


@dataclass
class AuditRecord:
    id: str
    timestamp: str
    question: str
    generated_sql: Optional[str]
    safe_sql: Optional[str]
    status: str  # "success" | "blocked" | "error"
    reason: Optional[str]
    row_count: Optional[int]
    execution_time_ms: Optional[float]


class AuditLogger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def record(
        self,
        question: str,
        generated_sql: Optional[str],
        safe_sql: Optional[str],
        status: str,
        reason: Optional[str] = None,
        row_count: Optional[int] = None,
        execution_time_ms: Optional[float] = None,
    ) -> AuditRecord:
        entry = AuditRecord(
            id=str(uuid.uuid4()),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            question=question,
            generated_sql=generated_sql,
            safe_sql=safe_sql,
            status=status,
            reason=reason,
            row_count=row_count,
            execution_time_ms=execution_time_ms,
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry)) + "\n")
        return entry

    def recent(self, limit: int = 20) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        records = [json.loads(line) for line in lines[-limit:]]
        return list(reversed(records))
