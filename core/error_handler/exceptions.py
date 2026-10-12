class NoBackendError(RuntimeError):
    """Raised when no database backend (ArcticDB or Parquet) is available."""
    pass
