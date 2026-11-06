"""Current-run bibliographic HTTP records; never import historical lookup results."""

from datetime import datetime, timezone
import uuid, requests
from runtime.artifacts import save


class ReferenceHTTP:
    RequestException = requests.RequestException

    def __init__(self, folder, controller):
        self.folder = folder
        self.controller = controller
        self.successes = 0
        self.events = []
        self.session = requests.Session()

    def get(self, url, **kwargs):
        self.controller.check()
        request = {
            "method": "GET",
            "url": url,
            "params": kwargs.get("params"),
            "headers": kwargs.get("headers", {}),
        }
        key = uuid.uuid4().hex
        root = self.folder / key
        index = root / "completed.json"
        attempt = root / uuid.uuid4().hex
        attempt.mkdir(parents=True)
        self.events.append(
            {
                "request_identity": key,
                "attempt": attempt.relative_to(self.folder).as_posix(),
                "source": "new_request",
            }
        )
        save(attempt / "request.json", request)
        started = datetime.now(timezone.utc).isoformat()
        try:
            response = self.session.get(url, **kwargs)
        except requests.RequestException as exc:
            save(
                attempt / "failure.json",
                {
                    "started_utc": started,
                    "finished_utc": datetime.now(timezone.utc).isoformat(),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        body = attempt / "response.body"
        body.write_bytes(response.content)
        record = {
            "started_utc": started,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "request_record_scope": "explicit URL, query and headers; HTTP-library-added request headers are not captured",
            "status": response.status_code,
            "headers": dict(response.headers),
            "url": response.url,
            "encoding": response.encoding,
            "body": body.relative_to(root).as_posix(),
        }
        save(attempt / "transport.json", record)
        if 200 <= response.status_code < 300:
            try:
                response.json()
            except ValueError:
                pass
            else:
                save(index, record)
                self.successes += 1
        return response

    def close(self):
        self.session.close()
