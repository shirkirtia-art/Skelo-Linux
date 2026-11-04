#!/usr/bin/env python3
"""
Skelo MCP Server CLI Entrypoint.
Enables AI agents to interact with Skelo via standard I/O (JSON-RPC 2.0).
"""

import sys
from skelo.mcp import SkeloMCPServer

if __name__ == "__main__":
    server = SkeloMCPServer()
    server.run()
