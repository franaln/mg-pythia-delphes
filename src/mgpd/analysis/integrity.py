"""Stable plan checksums shared without importing the analysis API."""
import hashlib
import json


def checksum(plan):
    body = {key: value for key, value in plan.items() if key != 'plan_sha256'}
    return hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()
