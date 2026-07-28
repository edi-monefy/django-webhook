# Configuration file for the Sphinx documentation builder.

# -- Project information

import tomllib
from pathlib import Path

project = "django-webhook2"
copyright = "2023, Dani Hodovic; 2026, Eduard Luca"  # pylint: disable=redefined-builtin
author = "Eduard Luca"

# Single source of truth: read the version from pyproject.toml.
_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
release = tomllib.loads(_pyproject.read_text())["project"]["version"]
version = release

# -- General configuration

extensions = [
    "sphinx.ext.duration",
    "sphinx.ext.doctest",
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.intersphinx",
    "myst_parser",
]

# Generate anchors for h1-h3 so pages can link to a specific section of another
# page (e.g. configuration.md#populating-topics). Off by default in MyST.
myst_heading_anchors = 3

intersphinx_mapping = {
    "python": ("https://docs.python.org/3/", None),
    "sphinx": ("https://www.sphinx-doc.org/en/master/", None),
}
intersphinx_disabled_domains = ["std"]

templates_path = ["_templates"]

# -- Options for HTML output

html_theme = "sphinx_rtd_theme"

# -- Options for EPUB output
epub_show_urls = "footnote"
