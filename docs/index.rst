UrbanLens Documentation
=======================

.. warning::

   With few exceptions everything under ``docs/`` was written by a Claude agent,
   not by a person, and records what one automated session measured or believed
   on a given date. Treat it as evidence, not authority - see :doc:`README`.

Start here
----------

.. toctree::
   :maxdepth: 1
   :caption: Orientation

   README
   INDEX
   FEATURES
   GOALS
   ROADMAP

.. toctree::
   :maxdepth: 1
   :caption: Subsystems

   MEDIA_PIPELINE
   DATA_ENCRYPTION
   PRIVACY_MODEL
   AI_PIPELINE
   EXTERNAL_API
   METRICS
   NOTES

.. toctree::
   :maxdepth: 1
   :caption: Working on it

   TOOLING
   CONTRACT_TESTS
   INTEGRATION_TESTS
   LOCATION_DATA_TESTS
   PROBLEMS

API reference
-------------

Generated from the source by ``autoapi`` - every module under
``src/urbanlens`` except migrations and tests.

.. toctree::
   :maxdepth: 2

   api/index

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
