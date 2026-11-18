"""Model-facing tools. Load execution handlers only when requested."""


def __getattr__(name: str):
    if name == "TOOL_NAMES":
        from tools.catalog import TOOL_NAMES

        return TOOL_NAMES
    if name == "execute_tool" or name.startswith("handle_"):
        from tools import dispatch

        return getattr(dispatch, name)
    raise AttributeError(name)
