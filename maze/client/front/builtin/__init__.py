"""
Built-in task library

This package contains predefined task functions marked with the @task decorator
"""

from maze.client.front.builtin import simpleTask
from maze.client.front.builtin import fileTask
from maze.client.front.builtin import healthTask
from maze.client.front.builtin import agentTools
from maze.client.front.builtin import distributedSmoke

__all__ = ['simpleTask', 'fileTask', 'healthTask', 'agentTools', 'distributedSmoke']
