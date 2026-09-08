"""Describe a static graph in Python; let Ray execute its dependencies."""

from maze.graph import DAG, Input, Literal, Node, Output, compile
from maze.runtime import Runtime

__all__ = ["DAG", "Input", "Literal", "Node", "Output", "Runtime", "compile"]
