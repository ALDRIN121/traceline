import pytest

from .driver import WorkflowDriver


@pytest.fixture
def platform(tmp_path):
    driver = WorkflowDriver(tmp_path)
    yield driver
    driver.close()
