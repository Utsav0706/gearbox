import io
import json
import logging

from gearbox.log import setup_logging


def test_json_lines_with_extra_fields():
    stream = io.StringIO()
    setup_logging("INFO", stream)
    logging.getLogger("gearbox.test").info("routed", extra={"tier": "M", "cost_gbp": 0.0001})
    logging.getLogger("gearbox.test").debug("hidden")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1  # DEBUG is below the level
    record = json.loads(lines[0])
    assert record["msg"] == "routed"
    assert record["tier"] == "M"
    assert record["cost_gbp"] == 0.0001
    assert record["level"] == "INFO"
