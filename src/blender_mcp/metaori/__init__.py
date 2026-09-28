"""MetaOri 3D: local ComfyUI asset generation and model normalization for Blender MCP.

See docs/metaori.md. The MCP tools are registered on the server by register_tools().
"""


def register_tools(mcp, get_blender_connection, service=None):
    """Register the metaori_* MCP tools on a FastMCP server.

    Imported lazily so `import blender_mcp.metaori` stays cheap.
    """
    from .tools import register_tools as _register

    return _register(mcp, get_blender_connection, service)
