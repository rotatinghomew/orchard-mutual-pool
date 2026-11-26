"""产季互助与退回清算服务。

最常用入口：
    from mutual_aid import MutualAidService
    svc = MutualAidService.from_data_file("data/source_records.json")
"""

from .core import (
    ConflictError,
    EligibilityError,
    MutualAidError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
    apportion,
    bp_part,
    check_claim_eligibility,
    loss_share,
    split_compensation_voucher,
    tenure_member,
)
from .service import FunderPortal, MemberPortal, MutualAidService

# 常见命名别名，方便不同调用习惯。
Service = MutualAidService
PoolService = MutualAidService
Pool = MutualAidService
MutualAidPool = MutualAidService
CooperativeService = MutualAidService

__all__ = [
    "MutualAidService", "Service", "PoolService", "Pool", "MutualAidPool",
    "CooperativeService",
    "MemberPortal", "FunderPortal",
    "MutualAidError", "ValidationError", "NotFoundError",
    "PermissionDeniedError", "ConflictError", "EligibilityError",
    "apportion", "bp_part", "check_claim_eligibility", "loss_share",
    "split_compensation_voucher", "tenure_member",
]
