"""Runner entry: python3 -m shed_sdk.main <tool_dir> <args.json> <result.json>"""

import importlib.util
import json
import sys
import traceback
from pathlib import Path

from shed_sdk import Shed


def main(argv: list[str]) -> int:
    tool_dir, args_path, result_path = Path(argv[1]), Path(argv[2]), Path(argv[3])
    try:
        sys.path.insert(0, str(tool_dir))
        spec = importlib.util.spec_from_file_location("tool", tool_dir / "tool.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tool"] = mod
        spec.loader.exec_module(mod)
        result = mod.run(json.loads(args_path.read_text()), Shed())
        out = {"ok": True, "result": result}
        text = json.dumps(out, default=str, ensure_ascii=False)
    except BaseException as e:  # noqa: BLE001 - report every failure as a result
        tb = traceback.format_exc()
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:2000], "traceback": tb[-4000:]}
        text = json.dumps(out, default=str, ensure_ascii=False)
    result_path.write_text(text)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
