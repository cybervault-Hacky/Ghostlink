"""Phase 13 — observability: secret scrubbing, request correlation, no leaks."""

from __future__ import annotations

import contextlib
import io
import json

from portal_server.observability import (
    StructuredLogger,
    new_request_id,
    scrub_secrets,
    validate_request_id,
)


def _emit(logger: StructuredLogger, level: str, message: str, extra: dict | None = None) -> str:
    """Run the real emit path and return the emitted line."""
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        logger._emit(level, message, extra)
    return sink.getvalue()


def test_scrub_secrets_redacts_shaped_values() -> None:
    text = "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig dk_abc123:superSecretValue GL-ABCDEFGH234567AB "
    scrubbed = scrub_secrets(text)
    assert "superSecretValue" not in scrubbed
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed
    assert "GL-ABCDEFGH234567AB" not in scrubbed
    assert "<redacted>" in scrubbed


def test_scrub_secrets_redacts_bearer_in_headers() -> None:
    raw = "authorization: Bearer secret-token"
    scrubbed = scrub_secrets(raw)
    assert "secret-token" not in scrubbed
    assert scrubbed != raw


def test_scrub_secrets_is_defensive_for_exception_text() -> None:
    msg = "operation failed for token dk_k1:verylongsecretsegment and code 123456"
    cleaned = scrub_secrets(msg)
    assert "verylongsecretsegment" not in cleaned


def test_request_id_validation() -> None:
    assert validate_request_id("abc-123_XYZ") == "abc-123_XYZ"
    assert validate_request_id("../../etc") != "../../etc"
    assert validate_request_id("a" * 200) != "a" * 200
    assert validate_request_id("<script>") != "<script>"
    assert validate_request_id(None).startswith("req_")


def test_new_request_id_unique() -> None:
    ids = {new_request_id() for _ in range(50)}
    assert len(ids) == 50


def test_logger_never_emits_secret_fields() -> None:
    logger = StructuredLogger(level="info", log_format="json")
    line = _emit(
        logger,
        "info",
        "login",
        {
            "request_id": "req_x",
            "method": "POST",
            "route": "/api/v1/auth/signin",
            "status": 200,
            "password": "hunter2",
            "token": "super-secret-access-token",
            "authorization": "Bearer abc.def.ghi",
        },
    )
    assert "hunter2" not in line
    assert "super-secret-access-token" not in line
    assert "abc.def.ghi" not in line
    assert "req_x" in line


def test_logger_emits_only_safe_keys() -> None:
    logger = StructuredLogger(level="info", log_format="json", deployment_version="0.15.0")
    line = _emit(
        logger,
        "info",
        "request",
        {"request_id": "req1", "method": "GET", "status": 200, "secret": "leak"},
    )
    parsed = json.loads(line)
    assert "secret" not in parsed
    assert parsed["request_id"] == "req1"
    assert parsed["deployment_version"] == "0.15.0"


def test_logger_message_text_is_scrubbed() -> None:
    logger = StructuredLogger(level="info", log_format="json")
    line = _emit(logger, "info", "using dk_x:secrethunter2secret in flow", None)
    assert "secrethunter2secret" not in line


def test_logger_includes_environment_and_event() -> None:
    import json

    logger = StructuredLogger(
        level="info",
        log_format="json",
        environment="production",
        deployment_version="0.16.0",
    )
    line = _emit(
        logger,
        "info",
        "request",
        {"event": "request", "request_id": "req_1", "method": "GET", "status": 200},
    )
    parsed = json.loads(line)
    assert parsed["environment"] == "production"
    assert parsed["event"] == "request"
    assert parsed["deployment_version"] == "0.16.0"


def test_logger_error_class_is_safe() -> None:
    import json

    logger = StructuredLogger(level="info", log_format="json", environment="production")
    line = _emit(
        logger,
        "error",
        "unhandled_exception",
        {"event": "request_error", "request_id": "req_2", "error_class": "ValueError"},
    )
    parsed = json.loads(line)
    assert parsed["error_class"] == "ValueError"


def test_logger_lifecycle_event() -> None:
    import json

    logger = StructuredLogger(level="info", log_format="json", environment="staging")
    line = _emit(logger, "info", "startup", {"event": "startup", "database_backend": "postgresql"})
    parsed = json.loads(line)
    assert parsed["event"] == "startup"
    # Non-safe metadata (database_backend not allow-listed) is dropped.
    assert "database_backend" not in parsed
