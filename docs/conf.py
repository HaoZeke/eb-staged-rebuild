"""Sphinx configuration for the eb-staged-rebuild documentation."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eb_staged_rebuild import __version__

project = "eb-staged-rebuild"
author = "eb-staged-rebuild developers"
copyright = "Copyright (c) eb-staged-rebuild developers"
version = __version__
release = __version__

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.graphviz",
    "sphinx_copybutton",
    "sphinx_design",
]

myst_enable_extensions = ["colon_fence", "deflist", "fieldlist"]
myst_heading_anchors = 3

templates_path = ["_templates"]
exclude_patterns = ["_build", "examples", "orgmode", "Thumbs.db", ".DS_Store"]

html_theme = "pydata_sphinx_theme"
html_title = "eb-staged-rebuild"
html_theme_options = {
    "navbar_align": "left",
    "show_nav_level": 2,
    "show_toc_level": 2,
    "navigation_depth": 3,
    "search_bar_text": "Search the documentation",
    "primary_sidebar_end": [],
    "footer_start": ["copyright"],
    "footer_end": ["sphinx-version"],
}
html_show_copyright = True
html_show_sourcelink = True

graphviz_output_format = "svg"

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

copybutton_prompt_text = r"\$ |eb-staged-rebuild: "
copybutton_prompt_is_regexp = True
copybutton_only_copy_prompt_lines = False
