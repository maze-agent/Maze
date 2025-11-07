"""
Built-in task library

This package contains predefined task functions marked with the @task decorator
"""

from maze.client.front.builtin import simpleTask
from maze.client.front.builtin import fileTask
from maze.client.front.builtin import healthTask

__all__ = ['simpleTask', 'fileTask', 'healthTask']

