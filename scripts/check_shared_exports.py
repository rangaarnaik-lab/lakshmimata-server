"""Fail if shared.__all__ names something the module never defines.

`live_scan.py` and `fundamentals_worker.py` use `from shared import *`, so a
name in __all__ with no definition crashes the service at import time rather
than where the typo was made. Runs on the AST, so no dependencies needed.
"""
import ast
import pathlib
import sys

SHARED = pathlib.Path(__file__).resolve().parent.parent / 'shared.py'


def defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    names.update(e.id for e in target.elts if isinstance(e, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
    return names


def exported_names(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == '__all__' for t in node.targets
        ):
            return [
                el.value for el in node.value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
    return []


def main() -> int:
    tree = ast.parse(SHARED.read_text(encoding='utf-8'))
    missing = sorted(set(exported_names(tree)) - defined_names(tree))
    if missing:
        print(f'shared.__all__ exports undefined names: {", ".join(missing)}')
        return 1
    print('shared.__all__ OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
