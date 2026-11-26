# 让 pytest 也能从仓库根导入 mutual_aid / service（unittest 下无副作用）。
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
