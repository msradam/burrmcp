# Deliberately vulnerable demo target

This directory contains a deliberately-vulnerable Python micro-app used
as the target for `examples/codebase_security.py`. Every file
demonstrates a common security anti-pattern mapped to a CWE
classification. Do not run this code or model your own after it.

Vulnerability map:

| File              | CWE     | Title                          | Detected by                  |
|-------------------|---------|--------------------------------|------------------------------|
| `app/db.py`       | CWE-89  | SQL Injection                  | bandit (B608) + hardcoded pw |
| `app/admin.py`    | CWE-78  | OS Command Injection           | bandit (B602, B605)          |
| `app/serde.py`    | CWE-502 | Insecure Deserialization       | bandit (B301)                |
|                   | CWE-95  | eval Code Injection            | bandit (B307)                |
| `app/crypto.py`   | CWE-327 | Weak Cryptographic Hash (MD5)  | bandit (B303, B324)          |
| `config.py`       | CWE-798 | Hardcoded Credentials          | detect-secrets               |
