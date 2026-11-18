"""Start the research CLI or import the ResearchAgent facade."""

from agent.cli import main
from agent.service import ResearchAgent

__all__ = ["ResearchAgent", "main"]


if __name__ == "__main__":
    main()
