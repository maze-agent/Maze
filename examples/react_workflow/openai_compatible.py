import ast
import json
import operator
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from maze import MaClient, create_openai_react_llm_task, task


OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def evaluate_arithmetic(expression: str):
    node = ast.parse(expression, mode="eval").body

    def visit(current):
        if isinstance(current, ast.Constant) and isinstance(current.value, (int, float)):
            return current.value
        if isinstance(current, ast.BinOp) and type(current.op) in OPERATORS:
            return OPERATORS[type(current.op)](visit(current.left), visit(current.right))
        if isinstance(current, ast.UnaryOp) and type(current.op) in OPERATORS:
            return OPERATORS[type(current.op)](visit(current.operand))
        raise ValueError(f"Unsupported arithmetic expression: {expression}")

    return visit(node)


@task(resources={"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0})
def calculator(expression: str):
    result = evaluate_arithmetic(expression)
    return {"result": result}


def main():
    config_path = PROJECT_ROOT / "updates" / "260520" / "api_key.jsonl"
    llm_task = create_openai_react_llm_task(
        config_path=str(config_path),
        task_name="siliconflow_react_decide",
        system_prompt=(
            "You are a ReAct controller for Maze. Return strict JSON only. "
            "Use the calculator tool when arithmetic is needed."
        ),
    )

    client = MaClient("http://localhost:8000")
    react = client.create_react_workflow(
        llm_task=llm_task,
        tools=[calculator],
        max_steps=3,
        timeout_seconds=120,
        task_timeout=120,
    )
    answer = react.run("Use the calculator to compute 18 * 7, then give the final answer.")
    print(json.dumps({
        "run_id": react.run_id,
        "answer": answer,
        "status": react.status()["status"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
