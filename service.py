"""兼容入口：支持 `from service import MutualAidService, Service`。"""

from mutual_aid.core import (  # noqa: F401
    ConflictError,
    EligibilityError,
    MutualAidError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
    apportion,
    bp_part,
    check_claim_eligibility,
    eligible_shares,
    loss_share,
    settlement_plan,
    split_compensation_voucher,
    tenure_member,
)
from mutual_aid.service import (  # noqa: F401
    FunderPortal,
    MemberPortal,
    MutualAidService,
)

Service = MutualAidService
PoolService = MutualAidService
Pool = MutualAidService
MutualAidPool = MutualAidService
CooperativeService = MutualAidService
HuzhuService = MutualAidService


def create_service(records_path: str = "data/source_records.json") -> MutualAidService:
    return MutualAidService.from_data_file(records_path)


def load_service(path: str) -> MutualAidService:
    return MutualAidService.load(path)
