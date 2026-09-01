try:
    from ._version import __version__
except ImportError:
    # Fallback when using the package in dev mode without installing
    # in editable mode with pip. It is highly recommended to install
    # the package from a stable release or in editable mode: https://pip.pypa.io/en/stable/topics/local-project-installs/#editable-installs
    import warnings
    warnings.warn("Importing 'bifrost_jupyter' outside a proper installation.")
    __version__ = "dev"
from .config import BifrostConfig
from .connect import connect
from .handlers import setup_handlers

__all__ = ["connect"]


def _jupyter_labextension_paths():
    return [{
        "src": "labextension",
        "dest": "bifrost-jupyter"
    }]


def _jupyter_server_extension_points():
    return [{
        "module": "bifrost_jupyter"
    }]


def _load_jupyter_server_extension(server_app):
    """Registers the API handler to receive HTTP requests from the frontend extension.

    Parameters
    ----------
    server_app: jupyterlab.labapp.LabApp
        JupyterLab application instance
    """
    from . import _profiles

    config = BifrostConfig(config=server_app.config)
    profiles = _profiles.resolve_profiles(config.profiles)
    setup_handlers(
        server_app.web_app,
        namespace=config.cluster_namespace,
        profiles=profiles,
    )
    name = "bifrost_jupyter"
    server_app.log.info(
        f"Registered {name} server extension "
        f"({len(profiles)} profile(s): {', '.join(sorted(profiles))})"
    )
