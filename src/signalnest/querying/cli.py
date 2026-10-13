"""Thin explicit CLI boundary; imports, help and validation perform no model calls."""

import json
import logging
import sys
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from signalnest.config import ConfigurationError
from signalnest.eventlog import Event, configure_logging, log_event
from signalnest.querying.agent import ask, render_report
from signalnest.querying.config import load_query_config, load_query_inputs
from signalnest.querying.contracts import Question


def query_command(args) -> int:
    try:
        settings = load_query_config(args.config)
        if args.command == "ask-config-check":
            instances, _ = load_query_inputs(settings)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "instances": list(instances),
                        "model_configured": settings.model is not None,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        question = Question(
            text=args.question,
            at=datetime.fromisoformat(args.at) if args.at else datetime.now(UTC),
        )
    except (ConfigurationError, ValidationError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ConfigurationError)
            else "问题或时间参数无效；时间须含 UTC offset"
        )
        print(f"查询配置/参数错误：{message}", file=sys.stderr)
        return 2
    logger = configure_logging()
    query_id = uuid4().hex
    log_event(logger, Event.ASK_STARTED, stage="query", run_id=query_id)
    try:
        report = ask(settings, question, local=args.local)
    except ConfigurationError:
        print("查询实例配置或画像不可用；请先运行 ask-config-check", file=sys.stderr)
        log_event(
            logger,
            Event.ASK_FINISHED,
            level=logging.ERROR,
            error_code="query_configuration_invalid",
            run_id=query_id,
        )
        return 2
    print(report.model_dump_json(indent=2) if args.json else render_report(report))
    log_event(
        logger,
        Event.ASK_FINISHED,
        stage="query",
        error_code=report.error_code
        or ("query_tools_incomplete" if "tool_results_incomplete" in report.warnings else None),
        run_id=query_id,
        attempted=report.model_usage["http_requests"],
        succeeded=len(report.notices),
    )
    # A degraded but useful retrieval report remains visible; automation can distinguish it.
    return 1 if report.error_code or "tool_results_incomplete" in report.warnings else 0
