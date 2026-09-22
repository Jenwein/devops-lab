"""Scripted stand-ins for the HTTP transports used by the platform scripts."""


class FakeTransport:
    def __init__(self, responses=None, default=(200, {})):
        self.responses = {
            key: list(value) if isinstance(value, list) else [value]
            for key, value in (responses or {}).items()
        }
        self.default = default
        self.calls = []
        self.headers = {}

    def request(self, method, path, fields=None, expected=(200,), *, body=None, content_type=None):
        key = (method, path.split("?", 1)[0])
        self.calls.append((method, path, fields if fields is not None else body))
        queue = self.responses.get(key)
        if queue:
            status, payload = queue[0] if len(queue) == 1 else queue.pop(0)
        else:
            status, payload = self.default
        if status not in expected:
            raise RuntimeError(f"{method} {path} returned HTTP {status}")
        return status, payload, {}

    def posts(self):
        return [call for call in self.calls if call[0] != "GET"]
