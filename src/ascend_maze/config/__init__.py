"""Strict, frozen Ascend-Maze configuration loading."""

from ascend_maze.config.loader import (
    DEFAULT_CONFIG_NAME,
    LoadedConfig,
    load_config,
    resolve_config_path,
)
from ascend_maze.config.schema import MainConfig
from ascend_maze.config.model_catalog import ModelCatalogDocument, load_model_catalog
from ascend_maze.config.node import NodeBootstrapConfig, load_node_bootstrap
from ascend_maze.config.override_document import (
    ConfigOverrideDocument,
    load_config_override_document,
)

__all__ = [
    "DEFAULT_CONFIG_NAME",
    "ConfigOverrideDocument",
    "LoadedConfig",
    "MainConfig",
    "ModelCatalogDocument",
    "NodeBootstrapConfig",
    "load_config",
    "load_model_catalog",
    "load_node_bootstrap",
    "load_config_override_document",
    "resolve_config_path",
]
