"""
pytest配置文件
包含共享的fixtures和配置
"""

import pytest
import sys
from pathlib import Path

# 将项目根目录添加到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture(scope="session")
def server_url():
    """服务器URL配置"""
    return "http://localhost:8000"


@pytest.fixture(scope="session")
def ws_url():
    """WebSocket URL配置"""
    return "ws://localhost:8000"


def pytest_configure(config):
    """pytest配置钩子"""
    # 添加自定义标记
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "unit: marks tests as unit tests"
    )


def pytest_collection_modifyitems(config, items):
    """修改测试收集，添加默认标记"""
    for item in items:
        # 为所有测试添加integration标记（因为它们都需要服务器运行）
        if "test_" in item.nodeid:
            item.add_marker(pytest.mark.integration)


