import json
from datetime import datetime, date
from typing import Any


class JSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        return super().default(obj)


def to_json(data: Any) -> str:
    return json.dumps(data, cls=JSONEncoder, ensure_ascii=False)


def from_json(json_str: str) -> Any:
    return json.loads(json_str)
