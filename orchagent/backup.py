from __future__ import annotations

import os
import time
from pathlib import Path


def create_backup_dir(root: Path, kind: str) -> tuple[Path, int, int]:
    created_at_ns = time.time_ns()
    base_name = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    sequence = 0
    while True:
        name = base_name if sequence == 0 else f"{base_name}-{sequence}"
        backup_dir = root / name
        try:
            backup_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        except FileExistsError:
            # 同秒同进程重复备份时递增后缀，mkdir 保证并发争用下仍唯一。
            sequence += 1
            continue
        os.chmod(backup_dir, 0o700)
        return backup_dir, created_at_ns, sequence
