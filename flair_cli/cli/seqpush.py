"""
Sequential Push command (seqpush) re-exported from push module.
Provides backward compatibility and diagnostic fallback for the 5-step commit creation loop.
"""
from .push import seqpush, app

__all__ = ["seqpush", "app"]
