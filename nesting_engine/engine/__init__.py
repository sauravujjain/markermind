"""
Nesting engine implementations.

Available engines:
- SpyrrowEngine: State-of-the-art nesting using Sparrow algorithm

Usage:
    >>> from nesting_engine.engine import SpyrrowEngine, SpyrrowConfig
    >>> engine = SpyrrowEngine()
    >>> solution = engine.solve(instance, config=SpyrrowConfig(time_limit=60))
"""

from nesting_engine.engine.spyrrow_engine import (
    NestingEngine,
    SpyrrowEngine,
    SpyrrowConfig,
    check_spyrrow_available,
    get_spyrrow_version,
)

__all__ = [
    "NestingEngine",
    "SpyrrowEngine",
    "SpyrrowConfig",
    "check_spyrrow_available",
    "get_spyrrow_version",
]
