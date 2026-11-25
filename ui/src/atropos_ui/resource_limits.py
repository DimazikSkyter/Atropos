"""Positive, configurable process budgets. Limits fail explicitly, never truncate imports."""
import os


def limit(name, default):
    try:
        value = int(os.getenv('ATROPOS_' + name, str(default)))
    except ValueError:
        raise ValueError('ATROPOS_' + name + ' должен быть положительным целым числом.') from None
    if value < 1:
        raise ValueError('ATROPOS_' + name + ' должен быть положительным целым числом.')
    return value
