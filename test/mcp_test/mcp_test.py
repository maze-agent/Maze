# test_react_agent_demo.py
import os
import pytest
import asyncio
from maze.agent import ReActAgent, ShortTermMemory, Toolkit, DashScopeModel
from maze.mcp import StdIOClient


class TestReActAgentDemo:
    def test_weather_and_browser_query(self):
        """Test ReActAgent with weather query and browser opening via MCP tool."""

        async def _run_test():
            model = DashScopeModel(
                api_key=os.getenv("DASHSCOPE_API_KEY"),
                model_name="qwen-max"
            )
            memory = ShortTermMemory()
            toolkit = Toolkit()

            mcp_client = StdIOClient(
                name='mcp-tools',
                command="uvx",
                args=["--from", "browser-use[cli]", "browser-use", "--mcp"],
                env={"BROWSER_USE_HEADLESS": "false"}
            )
            await mcp_client.connect()
            await toolkit.add_mcp_tool(mcp_client)

            agent = ReActAgent(model=model, memory=memory, toolkit=toolkit, max_iterations=5)
            response = await agent.run("What's the weather like in Beijing? Open the browser")

            # Basic assertions
            assert response is not None
            assert isinstance(response, str)
            assert len(response.strip()) > 0

            await mcp_client.close()

        # Run the async test inside a new event loop (since pytest doesn't auto-handle top-level async in non-asyncio-marked functions)
        asyncio.run(_run_test())


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])