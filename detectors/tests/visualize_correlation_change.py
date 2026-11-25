"""Build the continuous online correlation report directly from InfluxDB."""
import argparse
from pathlib import Path
from tests.visualize_correlation_online import create_report

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('artifacts/correlation-change'))
    create_report(parser.parse_args().output)
