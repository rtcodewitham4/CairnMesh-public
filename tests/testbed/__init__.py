"""TEST-ONLY scaffolding for the bench. Nothing under ``cairnmesh/`` may import this package.

It lives under ``tests/`` on purpose: ``tests/`` is not a package of the wheel and not in the sdist
(``tests/unit/test_testbed_isolation.py`` builds both and checks), and a production module that
imported it would fail the import-graph scan in the same file.

``FilteredPort`` (``rxfilter.py``) is the gateway-side twin of the node firmware's test receive
filter (firmware/rxfilter); ``topo.py`` parses the shared topology text; ``m5_driver`` fans one
physical radio out to several independent stacks and runs the host M5 scenarios. See README.md here.
"""
