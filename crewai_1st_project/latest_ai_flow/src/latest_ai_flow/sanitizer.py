import hashlib
import hmac
import os


def pseudonymize(value: str, kind: str) -> str:
    key = os.environ["REDACTION_KEY"].encode()

    digest = hmac.new(
        key,
        f"{kind}:{value}".encode(),
        hashlib.sha256,
    ).hexdigest()[:10]

    return f"{kind}-{digest}"

