from src.bridge._recording import RecordingMixin
from src.bridge._processing import ProcessingMixin
from src.bridge._settings import SettingsMixin
from src.bridge._environment import EnvironmentMixin
from src.bridge._worker import WorkerMixin
from src.bridge._services import ServicesMixin
from src.bridge._base import BridgeBase


class Bridge(
    RecordingMixin,
    ProcessingMixin,
    SettingsMixin,
    EnvironmentMixin,
    WorkerMixin,
    ServicesMixin,
    BridgeBase,
):
    """PyWebView Bridge — 所有暴露給 JS 的 API 方法集合（純 Windows 版）。"""
