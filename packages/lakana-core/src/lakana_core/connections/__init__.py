"""Connections: where a Python asset reads a source system from, per environment.

Lakana runs no connector. Assets declare the connections they read from, checked at deploy;
a running task fetches each declared one with its own token (`fetch`).
"""
