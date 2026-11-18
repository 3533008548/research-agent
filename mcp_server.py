"""Start the read-only MCP server over stdio."""

from integrations.mcp_server import main


if __name__ == "__main__":
    raise SystemExit(main())
