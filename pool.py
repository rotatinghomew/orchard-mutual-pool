"""兼容入口：`from pool import Pool, MutualAidService`。"""

from service import *  # noqa: F401,F403
from service import MutualAidService

Pool = MutualAidService
