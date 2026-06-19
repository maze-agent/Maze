"""Legacy standalone agent package.

This package is not part of the Maze Core Runtime public boundary. It remains
temporarily for compatibility and should move to an extension or be removed in
a later purification phase.
"""

from maze.agent.react_agent.react_agent import ReActAgent

from maze.agent.memory.short_term_memory import ShortTermMemory

from maze.agent.tool.toolkit import Toolkit
from maze.agent.tool.calculator import calculator
from maze.agent.tool.weather import get_current_weather

from maze.agent.model.openai_model import OpenAIModel
from maze.agent.model.dashscope_model import DashScopeModel

__all__ = ['ReActAgent', 'ShortTermMemory', 'Toolkit', 'calculator', 'get_current_weather', 'OpenAIModel', 'DashScopeModel']
