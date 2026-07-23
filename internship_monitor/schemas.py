"""JSON Schema definitions for AI structured outputs."""

from __future__ import annotations

from typing import Any

# OpenAI / GitHub Models Structured Outputs require an object root (not a bare array)
# and, in strict mode, explicit required fields + additionalProperties: false.
NULLABLE_STRING: dict[str, Any] = {
    "anyOf": [
        {"type": "string"},
        {"type": "null"},
    ]
}

AI_JOB_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "role": {"type": "string"},
        "location": {"type": "string"},
        "apply_url": NULLABLE_STRING,
        "added": NULLABLE_STRING,
        "closed": {"type": "boolean"},
        "sponsorship": {
            "type": "string",
            "enum": [
                "available",
                "unavailable",
                "citizenship_required",
                "unknown",
            ],
        },
    },
    "required": [
        "company",
        "role",
        "location",
        "apply_url",
        "added",
        "closed",
        "sponsorship",
    ],
    "additionalProperties": False,
}

AI_JOBS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": AI_JOB_ITEM_SCHEMA,
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}

AI_JOBS_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "internship_jobs_extraction",
        "strict": True,
        "schema": AI_JOBS_RESPONSE_SCHEMA,
    },
}
