"""Serialization helpers. DO NOT USE: deliberately vulnerable."""

import pickle


def deserialize(blob: bytes):
    """Restore an object. CWE-502: pickle.loads on untrusted bytes."""
    return pickle.loads(blob)  # B301


def evaluate_expression(expr: str):
    """Evaluate a user-supplied expression. CWE-95: code injection via eval."""
    return eval(expr)  # B307
