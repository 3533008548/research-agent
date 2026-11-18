"""Compatibility entry point for the standalone PDF reader."""

from papers.reader import PaperReader, extract_images, main, read_pdf_enhanced

__all__ = ["PaperReader", "extract_images", "read_pdf_enhanced"]


if __name__ == "__main__":
    main()
