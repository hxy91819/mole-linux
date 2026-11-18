#!/usr/bin/env python3
"""Retire abandoned BB environments before the linked and clone sweeps."""
import importlib.util
from pathlib import Path


if __name__ == '__main__':
    path = Path(__file__).with_name('git_tree_sweep.py')
    spec = importlib.util.spec_from_file_location('git_tree_sweep', path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    raise SystemExit(module.bb_environment_main())
