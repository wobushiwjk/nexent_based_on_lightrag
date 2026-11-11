"""Scripts that package and import the repository into Nexent.

Every module here is import-safe without network access, so the offline test
suite under ``tests/nexent`` can exercise the request-building and packaging
logic against fakes. Live access is isolated behind ``nexent_client``.
"""
