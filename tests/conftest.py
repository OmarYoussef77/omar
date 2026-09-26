import copy
from pathlib import Path

import pytest

from tracking_agent import config as cfg

EXAMPLE = Path(__file__).resolve().parent.parent / "tracking.example.yaml"


@pytest.fixture
def config():
    return copy.deepcopy(cfg.load_config(EXAMPLE))
