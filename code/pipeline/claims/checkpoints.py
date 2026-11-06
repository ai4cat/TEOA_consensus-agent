"""Resume validated Stage 5 tasks within one run using exact input equality."""

import json
import threading
import uuid
from pathlib import Path


class ValidatedCheckpoints:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.records = {}
        self.reused = 0
        for path in sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime_ns):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                key = tuple(record['input'])
                if record.get('schema') == 'stage5.validated.medium.v1' and len(key) == 4:
                    self.records[key] = record['content']
            except (ValueError, KeyError, TypeError):
                continue

    def get(self, key):
        with self.lock:
            content = self.records.get(key)
            if content is not None:
                self.reused += 1
            return content

    def put(self, key, content):
        # No source/credential hashes; input includes endpoint, model, budget and
        # full prompt. Failed transport/schema responses are never checkpointed.
        record = {'schema': 'stage5.validated.medium.v1', 'input': list(key), 'content': content}
        with self.lock:
            path = self.directory / (uuid.uuid4().hex + '.json')
            pending = path.with_suffix('.tmp')
            pending.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
            pending.replace(path)
            self.records[key] = content
