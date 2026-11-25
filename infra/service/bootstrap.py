"""Create the private runtime token once, without printing or replacing it."""
from pathlib import Path
import os
import secrets


def main():
    directory = Path(__file__).resolve().parent / '.runtime'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / 'token'
    try:
        with path.open('x', encoding='utf-8') as output:
            output.write(secrets.token_hex(32) + '\n')
        if os.name != 'nt':
            directory.chmod(0o700)
            path.chmod(0o444)  # Readable by the unprivileged container UID; parent stays private on host.
    except FileExistsError:
        if len(path.read_text(encoding='utf-8').strip()) < 32:
            raise SystemExit('Invalid infra/service/.runtime/token; restore or replace the private runtime token.')
    print('Runtime secret is ready.')


if __name__ == '__main__':
    main()
