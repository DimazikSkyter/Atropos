"""Runtime trend report: uv run python -m apps.demo --output artifacts/runtime-trend."""


def cases():
    """Retain the original twelve demo fixtures and parameter settings."""
    from tests.runtime_trend_scenarios import legacy_scenarios
    for scenario in legacy_scenarios():
        yield scenario.name, list(scenario.points), scenario.config


def main():
    from tests.visualize_runtime_trend import main as report_main
    report_main()


if __name__ == '__main__':
    main()
