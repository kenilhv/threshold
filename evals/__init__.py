"""Measuring the extractor instead of asserting things about it.

Top-level rather than inside ``src/threshold`` on purpose. This package imports
``seeds``, which needs reportlab to render PDFs, and reportlab is a development
dependency the running service does not have. An evaluation harness that shipped
inside the runtime package would either drag a renderer into production or force
the import to be conditional, and both are worse than a directory.
"""
