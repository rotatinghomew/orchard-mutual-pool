"""兼容入口：`import mutual_aid_service` / `from mutual_aid_service import ...`。"""

from mutual_aid import *  # noqa: F401,F403
from mutual_aid.service import MutualAidService  # noqa: F401

Service = MutualAidService
