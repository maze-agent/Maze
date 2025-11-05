# Maze 测试套件

本目录包含 Maze 项目的所有测试文件。

## 测试文件说明

### 标准 pytest 测试（推荐使用）

1. **`test_simple_api.py`** - 测试使用内置任务的简单工作流
   - 测试内置任务的执行
   - 测试任务输出传递
   
2. **`test_user_upload_task.py`** - 测试用户上传自定义任务的工作流
   - 测试用户自定义任务
   - 测试自定义输入
   - 测试多任务链式执行

3. **`test_http_api.py`** - 测试直接使用HTTP API和WebSocket
   - 测试REST API端点
   - 测试WebSocket连接
   - 测试完整工作流执行

### 旧版测试文件（保留用于参考）

- `simple_api_test.py` - 旧版简单API测试
- `api_test.py` - 旧版HTTP API测试
- `user_upload_task_test.py` - 旧版用户上传任务测试

## 前置条件

### 1. 安装依赖

确保已安装pytest和相关依赖：

```bash
pip install pytest pytest-asyncio websocket-client requests
```

或者使用项目的依赖文件：

```bash
pip install -e .
```

### 2. 启动服务器

在运行测试之前，需要先启动Maze服务器：

```bash
# 在另一个终端窗口中运行
maze server
# 或者
python -m maze.cli.cli server
```

确保服务器在 `http://localhost:8000` 上运行。

## 运行测试

### 运行所有测试

```bash
pytest
```

### 运行特定测试文件

```bash
# 测试简单API
pytest test/test_simple_api.py -v

# 测试用户上传任务
pytest test/test_user_upload_task.py -v

# 测试HTTP API
pytest test/test_http_api.py -v
```

### 运行特定测试类

```bash
pytest test/test_simple_api.py::TestSimpleWorkflow -v
```

### 运行特定测试方法

```bash
pytest test/test_simple_api.py::TestSimpleWorkflow::test_builtin_task_workflow -v
```

### 显示打印输出

```bash
pytest -v -s
```

### 运行测试并显示详细信息

```bash
pytest -vv
```

### 只运行失败的测试

```bash
pytest --lf  # last failed
```

### 生成测试覆盖率报告

```bash
pip install pytest-cov
pytest --cov=maze --cov-report=html
```

## 测试标记

测试使用以下标记进行分类：

- `@pytest.mark.integration` - 集成测试（需要服务器运行）
- `@pytest.mark.slow` - 慢速测试
- `@pytest.mark.unit` - 单元测试

### 运行特定标记的测试

```bash
# 只运行集成测试
pytest -m integration

# 排除慢速测试
pytest -m "not slow"
```

## 测试输出示例

```
test/test_simple_api.py::TestSimpleWorkflow::test_builtin_task_workflow PASSED
test/test_simple_api.py::TestSimpleWorkflow::test_task_output_propagation PASSED
test/test_user_upload_task.py::TestUserUploadTask::test_user_defined_task_workflow PASSED

========================= 3 passed in 5.23s =========================
```

## 常见问题

### 1. 服务器未运行

**错误信息**: `Connection refused` 或 `Failed to establish connection`

**解决方案**: 确保Maze服务器正在运行在 `http://localhost:8000`

```bash
maze server
```

### 2. 导入错误

**错误信息**: `ModuleNotFoundError: No module named 'maze'`

**解决方案**: 确保已安装项目

```bash
pip install -e .
```

### 3. WebSocket连接失败

**错误信息**: `WebSocket connection failed`

**解决方案**: 
- 确保服务器正在运行
- 检查防火墙设置
- 验证端口8000未被占用

### 4. Ray相关错误

**错误信息**: `Ray cluster not initialized`

**解决方案**: 服务器启动时会自动初始化Ray集群。如果出现问题，可以手动停止并重启：

```bash
ray stop
maze server
```

## 调试技巧

### 1. 使用pytest的调试模式

```bash
pytest --pdb  # 失败时进入调试器
```

### 2. 打印详细日志

```bash
pytest -v -s --log-cli-level=DEBUG
```

### 3. 只运行一个测试并查看详细输出

```bash
pytest test/test_simple_api.py::TestSimpleWorkflow::test_builtin_task_workflow -v -s
```

## 编写新测试

参考现有测试文件的结构：

```python
import pytest
from maze import MaClient

@pytest.fixture
def client():
    """创建客户端fixture"""
    return MaClient()

class TestMyFeature:
    """测试我的功能"""
    
    def test_something(self, client):
        """测试某个功能"""
        # 准备
        workflow = client.create_workflow()
        
        # 执行
        # ... 执行测试逻辑 ...
        
        # 断言
        assert result == expected
```

## 持续集成

这些测试可以集成到CI/CD流程中：

```yaml
# GitHub Actions 示例
- name: Run tests
  run: |
    maze server &
    sleep 5  # 等待服务器启动
    pytest -v
```

## 贡献指南

添加新测试时：

1. 遵循现有的命名约定（`test_*.py`）
2. 使用描述性的测试函数名
3. 添加适当的文档字符串
4. 使用fixtures来共享设置代码
5. 包含断言来验证结果
6. 考虑边界情况和错误情况

## 参考资源

- [pytest文档](https://docs.pytest.org/)
- [Maze项目文档](../README.md)
- [Python测试最佳实践](https://docs.python-guide.org/writing/tests/)


