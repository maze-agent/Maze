"""User-facing metrics reporting API for Maze tasks.

Usage inside a @task function:

    from maze import metrics

    @task
    def call_llm(prompt: str):
        response = openai_client.chat.completions.create(...)
        metrics.report(
            tokens_in=response.usage.prompt_tokens,
            tokens_out=response.usage.completion_tokens,
            model=response.model,
        )
        return {"answer": ...}

Multiple ``report()`` calls within the same task accumulate. The framework
collects them automatically when the task returns.
"""

from maze.metrics.collector import report

__all__ = ["report"]
