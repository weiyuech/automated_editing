__all__ = ["create_app"]
__version__ = "0.1.22"


def __getattr__(name: str):
    """Keep the public factory importable without executing the server module eagerly.

    Electron starts the backend with ``python -m automated_video_editing_backend.main``.
    Importing ``main`` from the package initializer first meant ``runpy`` then executed an
    already-imported module and warned that startup behaviour could be unpredictable. A lazy
    attribute preserves ``from automated_video_editing_backend import create_app`` while the
    module entry point is imported exactly once.
    """
    if name == "create_app":
        from .main import create_app

        return create_app
    raise AttributeError(name)
