"""Small persistent JSON store shared by Glue / SageMaker / Step Functions job histories."""
import json
import threading

from src import config


class JobStore:
    def __init__(self, service, filename):
        self.path = config.CLOUD_ROOT / service / filename
        self.lock = threading.RLock()
        self._data = None

    def _load(self):
        if self._data is None:
            self._data = json.loads(self.path.read_text()) if self.path.exists() else {}
        return self._data

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, default=str))
        tmp.replace(self.path)

    def put(self, key, value):
        with self.lock:
            self._load()[key] = value
            self._save()

    def update(self, key, **fields):
        with self.lock:
            self._load()[key].update(fields)
            self._save()
            return self._data[key]

    def get(self, key):
        with self.lock:
            return self._load().get(key)

    def all(self):
        with self.lock:
            return list(self._load().values())

    def delete(self, key):
        with self.lock:
            self._load().pop(key, None)
            self._save()
