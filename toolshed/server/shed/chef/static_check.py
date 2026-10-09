"""P4 static checks for agent-written tool code (contract section 8).

check(code, manifest) -> [{"rule", "line", "msg"}]; an empty list = pass. AST only, stdlib only.
The sandbox is the real boundary; this pass rejects the obvious escapes before a reviewer reads the code.
"""
import ast

BANNED_MODULES = {
    "subprocess", "ctypes", "cffi", "multiprocessing", "socket", "socketserver", "http.server",
    "xmlrpc.server", "asyncio.subprocess", "pty", "importlib", "runpy", "code", "codeop", "builtins",
    "posix", "_posixsubprocess", "_thread",
}
BANNED_NAMES = {
    "os.system", "os.popen", "os.environ", "os.environb", "os.getenv", "os.getenvb", "os.putenv",
    "os.unsetenv", "os.fork", "os.forkpty", "os.kill", "os.killpg", "os.setuid", "os.setgid", "os.chroot",
}
BANNED_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn", "asyncio.create_subprocess",
                   "concurrent.futures.ProcessPool")
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
DUNDERS = {"__subclasses__", "__globals__", "__builtins__", "__code__", "__import__", "__loader__",
           "__bases__", "__mro__", "__getattribute__"}
FILE_FUNCS = {"open", "Path", "PurePath", "PosixPath", "PurePosixPath", "listdir", "scandir", "walk", "glob",
              "iglob", "rmtree", "copyfile", "copytree", "remove", "unlink", "rmdir", "mkdir", "makedirs",
              "chmod", "chown", "symlink", "join"}
ALLOWED_ROOTS = ("/work", "/tmp")
SECRETS = ("shed.db", "meter.sock", "rt.sock", "admin.token", "SHED_RUN_TOKEN", "SHED_ADMIN_TOKEN",
           "OAI_COMPATIBLE_KEY", "/proc/self")
NET_MODULES = {"urllib.request", "http.client", "requests", "httpx", "aiohttp", "urllib3", "ftplib", "smtplib",
               "websocket", "websockets"}


def _banned_name(dotted: str) -> bool:
    return any(dotted == b or dotted.startswith(b + ".") for b in BANNED_NAMES) or dotted.startswith(BANNED_PREFIXES)


def _banned_module(mod: str) -> bool:
    return any(mod == b or mod.startswith(b + ".") for b in BANNED_MODULES)


def _lit(node) -> str | None:
    """A string literal, or the literal head of an f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return str(node.values[0].value)
    return None


def _outside(path: str) -> bool:
    return path.startswith("/") and not any(path == r or path.startswith(r + "/") for r in ALLOWED_ROOTS)


def check(code: str, manifest: dict) -> list[dict]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [{"rule": "syntax", "line": e.lineno or 0, "msg": str(e.msg)}]
    uses = set(manifest.get("uses") or [])
    perms = manifest.get("permissions") or {}
    issues: dict[tuple, dict] = {}

    def flag(rule: str, node, msg: str) -> None:
        line = getattr(node, "lineno", 0)
        issues.setdefault((rule, line, msg), {"rule": rule, "line": line, "msg": msg})

    aliases: dict[str, str] = {}
    shed_names = {"shed"}
    call_funcs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                if _banned_module(a.name):
                    flag("import", node, f"import {a.name} is banned")
                if a.name in NET_MODULES and not perms.get("network"):
                    flag("network", node, f"{a.name} needs permissions.network = true")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _banned_module(mod):
                flag("import", node, f"from {mod} import is banned")
            if mod in NET_MODULES and not perms.get("network"):
                flag("network", node, f"{mod} needs permissions.network = true")
            for a in node.names:
                full = f"{mod}.{a.name}"
                aliases[a.asname or a.name] = full
                if _banned_module(full) or _banned_name(full):
                    flag("import", node, f"from {mod} import {a.name} is banned")
                elif full in NET_MODULES and not perms.get("network"):
                    flag("network", node, f"{full} needs permissions.network = true")
        elif isinstance(node, ast.FunctionDef) and node.name == "run" and len(node.args.args) >= 2:
            shed_names.add(node.args.args[1].arg)
        elif isinstance(node, ast.Call):
            call_funcs.add(id(node.func))

    def dotted(node) -> str | None:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(aliases.get(node.id, node.id))
            return ".".join(reversed(parts))
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            d = dotted(node)
            if d and _banned_name(d):
                flag("call", node, f"{d} is banned")
            if node.attr in DUNDERS:
                flag("dunder", node, f"access to {node.attr} is banned")
            if isinstance(node.value, ast.Name) and node.value.id in shed_names:
                if node.attr == "call" and id(node) not in call_funcs:
                    flag("shed_call", node, "use shed.call(...) directly, not as a value")
                if node.attr == "llm" and not perms.get("llm_usd"):
                    flag("llm", node, "shed.llm needs permissions.llm_usd > 0")
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            flag("dunder", node, "access to __builtins__ is banned")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hit = next((s for s in SECRETS if s in node.value), None)
            if hit:
                flag("secret", node, f"reference to {hit} is banned")
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if isinstance(f, ast.Name) and f.id in BANNED_CALLS and f.id not in aliases:
                flag("call", node, f"{f.id}() is banned")
            if name == "getattr" and len(node.args) >= 2:
                target, attr = dotted(node.args[0]), _lit(node.args[1])
                if isinstance(node.args[0], ast.Name) and node.args[0].id in shed_names:
                    flag("shed_call", node, "getattr on shed is banned")
                elif attr is None:
                    if target and target.split(".")[0] in aliases.values():
                        flag("call", node, "getattr with a dynamic name on a module is banned")
                elif attr in DUNDERS or (target and _banned_name(f"{target}.{attr}")):
                    flag("call", node, f"getattr(..., {attr!r}) is banned")
            if name in FILE_FUNCS:
                for arg in node.args[:2]:
                    p = _lit(arg)
                    if p and _outside(p):
                        where = "/data and /run are" if p.startswith(("/data", "/run")) else "paths outside /work and /tmp are"
                        flag("path", node, f"{name}({p!r}): {where} banned")
            if isinstance(f, ast.Attribute) and f.attr == "call" and isinstance(f.value, ast.Name) \
                    and f.value.id in shed_names:
                target = _lit(node.args[0]) if node.args else None
                if target is None or isinstance(node.args[0], ast.JoinedStr):
                    flag("shed_call", node, "shed.call needs a literal tool name")
                elif target not in uses:
                    flag("shed_call", node, f"shed.call({target!r}): not in manifest uses {sorted(uses)}")
    return sorted(issues.values(), key=lambda i: (i["line"], i["rule"]))
