import io
import json
import logging
from datetime import datetime

import pytest

from signalnest.eventlog import Event, JsonFormatter, configure_logging, log_event


def test_json_logging_context_and_idempotent_setup():
    output = io.StringIO()
    root_handlers = logging.getLogger().handlers[:]
    logger = configure_logging(output)
    configure_logging(output)
    log_event(
        logger,
        Event.STORAGE_INITIALIZED,
        source_id="whu-undergrad-student",
        run_id="run-1",
        document_id=7,
    )
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert datetime.fromisoformat(event["time"]).utcoffset().total_seconds() == 0
    assert event == {
        "time": event["time"],
        "level": "INFO",
        "event": "storage_initialized",
        "source_id": "whu-undergrad-student",
        "run_id": "run-1",
        "document_id": 7,
    }
    assert logging.getLogger().handlers == root_handlers


def test_formatter_never_serializes_arbitrary_message_or_extra():
    record = logging.LogRecord(
        "signalnest", logging.ERROR, __file__, 1, "secret=%s", ("TOKEN",), None
    )
    record.body = "<html>private whole page</html>"
    record.password = "PASSWORD"
    record.source_id = "https://user:password@example.org/"
    rendered = JsonFormatter().format(record)
    assert json.loads(rendered)["event"] == "unstructured_log"
    for sensitive in ["TOKEN", "PASSWORD", "private whole page", "password@example", "secret="]:
        assert sensitive not in rendered


def test_event_api_rejects_arbitrary_text():
    with pytest.raises(TypeError, match="Event"):
        log_event(logging.getLogger("signalnest"), "<html>whole page</html>")


def test_processing_context_is_bounded_and_safe():
    output = io.StringIO()
    log_event(
        configure_logging(output),
        Event.PROCESSING_FAILED,
        level=logging.ERROR,
        source_id="whu-undergrad-student",
        document_id=3,
        response_id=7,
        stage="parse",
        error_code="parse_missing_structure",
        run_id="run-1",
    )
    payload = json.loads(output.getvalue())
    assert payload["response_id"] == 7
    assert payload["stage"] == "parse"
    assert payload["error_code"] == "parse_missing_structure"
    output.seek(0)
    output.truncate()
    log_event(
        configure_logging(output),
        Event.PROCESSING_FAILED,
        stage="<html>PRIVATE</html>",
        error_code="SECRET=" * 100,
    )
    assert "stage" not in json.loads(output.getvalue())
    assert "SECRET" not in output.getvalue()
