"""Shared in-memory store for Music Assistant and Alexa routes.

Allows the Lambda skill code and the Flask API routes to share the last pushed 
media metadata and version directly in-memory to reduce loopback HTTP requests.
"""

_store = None
_version = 0
