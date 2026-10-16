"""Process-local retained-evidence limits for a bounded supplement tranche.

A separately reviewed caller must bound its per-admission written-file count and hold
its output lock. This helper does not authorize requests or change the native
queue. It preserves partial files on a size-limit failure and never cleans up.
"""
import os
from pathlib import Path
import resource
import stat

from causes import campaign_supplement as core
from shared.bounded_timing import span


TIMING_PRECHECK = 1
TIMING_INVENTORY = 2
TIMING_VALIDATION = 3


def require(value, reason):
    if not value:
        raise core.IntegrityError(reason)


def install_file_limit(maximum_file_bytes):
    """Constrain this dedicated caller and its children, never its supervisor."""
    require(type(maximum_file_bytes) is int and maximum_file_bytes > 0,
            'retention_file_limit')
    before = resource.getrlimit(resource.RLIMIT_FSIZE)
    soft, hard = before
    require(soft == resource.RLIM_INFINITY or soft >= maximum_file_bytes,
            'retention_existing_soft_limit_too_small')
    require(hard == resource.RLIM_INFINITY or hard >= maximum_file_bytes,
            'retention_existing_hard_limit_too_small')
    resource.setrlimit(resource.RLIMIT_FSIZE,
                       (maximum_file_bytes, maximum_file_bytes))
    after = resource.getrlimit(resource.RLIMIT_FSIZE)
    require(after == (maximum_file_bytes, maximum_file_bytes),
            'retention_file_limit_not_installed')
    return {'before': list(before), 'after': list(after)}


def inventory(root):
    """Count only a new, caller-owned output tree; reject links and devices."""
    root = core.canonical(root)
    require(root.is_dir(), 'retention_output_missing')
    records = {}
    def denied(error):
        raise error
    for directory, folders, files in os.walk(root, followlinks=False, onerror=denied):
        for name in folders:
            path = os.path.join(directory, name)
            require(stat.S_ISDIR(os.lstat(path).st_mode), 'retention_directory_link')
        for name in files:
            path = os.path.join(directory, name)
            value = os.lstat(path)
            require(stat.S_ISREG(value.st_mode) and value.st_nlink == 1,
                    'retention_file_link_or_device')
            records[path] = {'device': value.st_dev, 'inode': value.st_ino,
                                  'bytes': value.st_size}
    return {'root': str(root), 'bytes': sum(x['bytes'] for x in records.values()),
            'files': records}


class AdmissionBudget:
    """Reserve a reviewed worst-case write window before each new claim.

    The per-role allowances plus fixed shared metadata allowance must be within
    the adopted joint budget. Only one in-flight native request may use a role's
    allowance. The native queue supplies the exact cumulative claimed count.
    """
    def __init__(self, root, *, maximum_claims, maximum_retained_bytes,
                 maximum_file_bytes, maximum_written_files, reserve_bytes):
        values = (maximum_claims, maximum_retained_bytes, maximum_file_bytes,
                  maximum_written_files, reserve_bytes)
        require(all(type(value) is int and value > 0 for value in values),
                'retention_positive_limits')
        require(reserve_bytes >= maximum_file_bytes * maximum_written_files and
                maximum_retained_bytes >= reserve_bytes, 'retention_reserve_bound')
        self.root = core.canonical(root)
        self.maximum_claims = maximum_claims
        self.maximum_retained_bytes = maximum_retained_bytes
        self.maximum_file_bytes = maximum_file_bytes
        self.maximum_written_files = maximum_written_files
        self.reserve_bytes = reserve_bytes
        self.previous = None

    def before_admission(self, claimed, *, timing=None):
        with span(timing, TIMING_PRECHECK):
            require(type(claimed) is int and claimed >= 0, 'retention_claim_count')
            require(resource.getrlimit(resource.RLIMIT_FSIZE) ==
                    (self.maximum_file_bytes, self.maximum_file_bytes),
                    'retention_process_file_limit_changed')
        with span(timing, TIMING_INVENTORY):
            current = inventory(self.root)
        with span(timing, TIMING_VALIDATION):
            require(all(row['bytes'] <= self.maximum_file_bytes
                        for row in current['files'].values()), 'retention_oversized_file')
            if self.previous is not None:
                old = self.previous['files']
                require(set(old) <= set(current['files']), 'retention_file_disappeared')
                for path, row in old.items():
                    fresh = current['files'][path]
                    require((row['device'], row['inode']) ==
                            (fresh['device'], fresh['inode']) and fresh['bytes'] >= row['bytes'],
                            'retention_file_replaced_or_shrunk')
                written = len(set(current['files']) - set(old)) + sum(
                    current['files'][path]['bytes'] > row['bytes'] for path, row in old.items())
                require(written <= self.maximum_written_files and
                        current['bytes'] - self.previous['bytes'] <= self.reserve_bytes,
                        'retention_reviewed_write_window_exceeded')
            if claimed >= self.maximum_claims:
                raise core.AdmissionHold('fixed_admission_tranche_complete')
            if current['bytes'] + self.reserve_bytes > self.maximum_retained_bytes:
                raise core.AdmissionHold('retained_evidence_budget_reserved')
            self.previous = current
            return {'claimed': claimed, 'maximum_claims': self.maximum_claims,
                    'retained_bytes': current['bytes'], 'reserve_bytes': self.reserve_bytes,
                    'maximum_retained_bytes': self.maximum_retained_bytes,
                    'maximum_file_bytes': self.maximum_file_bytes,
                    'maximum_written_files': self.maximum_written_files}
