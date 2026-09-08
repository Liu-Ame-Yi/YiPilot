import os

import pytest

from yipilot.models.service import ProviderService


@pytest.mark.live
def test_explicit_provider_probe() -> None:
    result = ProviderService().test(os.environ["YIPILOT_LIVE_PROFILE"], request=True)
    assert result.status == "request_passed"
