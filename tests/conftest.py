import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("YIPILOT_RUN_LIVE") == "1" and os.environ.get("YIPILOT_LIVE_PROFILE"):
        return
    skip = pytest.mark.skip(
        reason="set YIPILOT_RUN_LIVE=1 and YIPILOT_LIVE_PROFILE to enable live provider tests"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
