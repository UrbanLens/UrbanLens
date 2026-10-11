UrbanLens Documentation
=======================

.. warning::

   With few exceptions everything under ``docs/`` was written by a Claude agent,
   not by a person, and records what one automated session measured or believed
   on a given date. Treat it as evidence, not authority - see ``docs/README.md``.

.. Every toctree here is a ``present-toctree`` (docs/conf.py): a page this checkout
   lacks is left out rather than warned about, so the public mirror, which ships
   none of these pages, still builds the API reference.

.. present-toctree::
   :maxdepth: 1
   :caption: Orientation

   README
   FEATURES

.. present-toctree::
   :maxdepth: 1
   :caption: Subsystems

   MEDIA_PIPELINE
   DATA_ENCRYPTION
   PRIVACY_MODEL
   AI_PIPELINE
   EXTERNAL_API
   METRICS

.. present-toctree::
   :maxdepth: 1
   :caption: Working on it

   TOOLING
   CONTRACT_TESTS
   INTEGRATION_TESTS
   MAP_PERFORMANCE
   BACKUPS

API reference
-------------

Generated from the source by ``autoapi`` - every module under
``src/urbanlens`` except migrations and tests.

.. toctree::
   :maxdepth: 2

   api/index

.. present-toctree::
   :hidden:
   :glob:

   DEMO
   EXTERNAL_API_SPOTGUESSR
   adr/*

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
