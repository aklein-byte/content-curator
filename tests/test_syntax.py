#!/usr/bin/env python3
"""
Syntax and import verification for all tatami-bot Python files.

Runs py_compile on every .py file and attempts top-level imports
for key modules to catch import errors early.

Usage: python tests/test_syntax.py
"""

import sys
import os
import py_compile
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

# All Python files that should compile cleanly
ALL_PY_FILES = sorted(
    p.relative_to(BASE_DIR)
    for p in BASE_DIR.rglob("*.py")
    if "__pycache__" not in str(p)
    and ".venv" not in str(p)
    and "venv/" not in str(p)
    and "reel-maker" not in str(p)
    and "node_modules" not in str(p)
)

# Key modules that should import without side effects (no API calls at import time)
IMPORTABLE_MODULES = [
    "config.niches",
    "config.categories",
    "tools.common",
    "tools.humanizer",
    "tools.post_queue",
    "tools.museum_apis",
    "agents.fact_checker",
    "tools.ig_api",
]


def test_compile_all():
    """Verify all .py files have valid syntax."""
    errors = []
    for rel_path in ALL_PY_FILES:
        full_path = BASE_DIR / rel_path
        try:
            py_compile.compile(str(full_path), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"  {rel_path}: {e}")

    if errors:
        print(f"FAIL: {len(errors)} files have syntax errors:")
        for e in errors:
            print(e)
        return False

    print(f"OK: {len(ALL_PY_FILES)} files compiled successfully")
    return True


def test_imports():
    """Verify key modules import without errors."""
    errors = []
    for mod in IMPORTABLE_MODULES:
        try:
            __import__(mod)
        except Exception as e:
            errors.append(f"  {mod}: {type(e).__name__}: {e}")

    if errors:
        print(f"FAIL: {len(errors)} modules failed to import:")
        for e in errors:
            print(e)
        return False

    print(f"OK: {len(IMPORTABLE_MODULES)} key modules imported successfully")
    return True


def test_config_consistency():
    """Verify niche configs have required keys."""
    from config.niches import NICHES

    required_keys = ["handle", "description", "engagement"]
    errors = []

    for niche_id, niche in NICHES.items():
        for key in required_keys:
            if key not in niche:
                errors.append(f"  {niche_id}: missing required key '{key}'")

        # Check engage_limits has expected keys
        limits = niche.get("engage_limits", {})
        for lk in ["daily_max_replies", "daily_max_likes", "like_delay", "reply_delay"]:
            if lk not in limits:
                errors.append(f"  {niche_id}: engage_limits missing '{lk}'")

    if errors:
        print(f"FAIL: {len(errors)} config issues:")
        for e in errors:
            print(e)
        return False

    print(f"OK: {len(NICHES)} niche configs validated")
    return True


def test_model_roles():
    """Verify all model roles resolve to valid model IDs."""
    from tools.common import get_model, _MODEL_DEFAULTS

    errors = []
    for role in _MODEL_DEFAULTS:
        model = get_model(role)
        if not model or not model.startswith("claude-"):
            errors.append(f"  {role}: resolved to '{model}' (expected claude-* model)")

    if errors:
        print(f"FAIL: {len(errors)} model role issues:")
        for e in errors:
            print(e)
        return False

    print(f"OK: {len(_MODEL_DEFAULTS)} model roles resolved")
    return True


if __name__ == "__main__":
    os.chdir(str(BASE_DIR))

    results = [
        test_compile_all(),
        test_imports(),
        test_config_consistency(),
        test_model_roles(),
    ]

    print()
    if all(results):
        print("ALL TESTS PASSED")
        sys.exit(0)
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)
