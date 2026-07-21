"""Shared fixtures for pipeline unit tests.

The loguru capture fixtures (loguru_warnings, loguru_records) live in tests/unit_tests/conftest.py,
since money_pit.email_sender's undelivered-record fallback logs outside the pipeline package.
"""
