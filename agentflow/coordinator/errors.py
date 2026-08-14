"""Coordinator refusal vocabulary shared without importing the Store implementation."""


class StoreUnavailable(RuntimeError):
    """The continuation store cannot be safely read or written."""
