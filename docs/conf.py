# Sphinx configuration file

project = 'UrbanLens'
copyright = '2023, Jess Mann'
author = 'Jess Mann'

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.viewcode',
]

templates_path = ['_templates']

exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

html_theme = 'alabaster'
