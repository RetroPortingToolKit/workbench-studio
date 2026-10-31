import os

def resolve_build_jobs(override=None):
    for candidate in (override, os.environ.get('WORKBENCH_BUILD_JOBS')):
        if candidate in (None, ''):
            continue
        try:
            value = int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    cpus = os.cpu_count() or 4
    return max(2, cpus - 2)
