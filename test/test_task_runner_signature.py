import pytest

from maze.core.scheduler.runner import Runner


def test_runner_calls_code_str_task_with_keyword_arguments():
    code_str = """
def process_text(text: str, suffix: str = "!"):
    return {"result": f"{text}{suffix}"}
"""

    runner = Runner(code_str, {"text": "Maze"})

    assert runner.run() == {"result": "Maze!"}


def test_runner_rejects_non_dict_return_value():
    code_str = """
def process_text(text: str):
    return text
"""

    runner = Runner(code_str, {"text": "Maze"})

    with pytest.raises(TypeError, match="must return a dict"):
        runner.run()
