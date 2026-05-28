import os
import sys

from version import APP_NAME


def _module_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _frozen_resource_root() -> str:
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        return os.path.abspath(meipass)
    return os.path.dirname(os.path.abspath(sys.executable))


def get_resource_dir() -> str:
    if getattr(sys, "frozen", False):
        return _frozen_resource_root()
    return _module_root()


def get_data_dir(app_name: str = APP_NAME) -> str:
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, app_name)
        return os.path.join(os.path.expanduser("~"), app_name)
    return _module_root()


RESOURCE_DIR = get_resource_dir()
DATA_DIR = get_data_dir()
