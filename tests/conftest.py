import pytest

from src.config import Settings
from src.store import Store
from src.tools import ToolBox


@pytest.fixture
def make_box(tmp_path):
    """每个测试一个独立的临时状态库；同一测试内多次调用共享同一个库（模拟多次运行）。"""
    db = tmp_path / "state.db"

    def _make(request, data_dir=None):
        return ToolBox(request, data_dir or Settings().data_dir, Store(db))

    return _make
