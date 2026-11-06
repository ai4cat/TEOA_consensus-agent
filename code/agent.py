import sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / 'pipeline'))
from runtime.agent import main
if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'Agent stopped: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
