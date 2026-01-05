"""Native Plugins registry, connectors, and permission boundary."""

from .mcp import McpHttpClient, McpProtocolError, McpStdioClient
from .registry import PluginRegistry, ToolRegistry
from .secrets import DpapiSecretStore, SecretStoreUnavailable
from .web_search import WebSearchClient, should_search_web, web_results_prompt

__all__ = [
    "McpHttpClient",
    "McpProtocolError",
    "McpStdioClient",
    "DpapiSecretStore",
    "PluginRegistry",
    "SecretStoreUnavailable",
    "ToolRegistry",
    "WebSearchClient",
    "should_search_web",
    "web_results_prompt",
]
