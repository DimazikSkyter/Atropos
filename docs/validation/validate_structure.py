"""Read-only syntax and shared-service import structure checks; JSON to stdout."""
import ast
import json
from pathlib import Path

root = next(p for p in (Path.cwd(), *Path(__file__).resolve().parents)
            if (p / 'ui/src/atropos_ui').is_dir())
files = [p for base in ('ui/src', 'ui/tests', 'detectors/apps', 'detectors/tests')
         for p in sorted((root / base).rglob('*.py')) if '__pycache__' not in p.parts]
trees = {p: ast.parse(p.read_bytes(), filename=str(p.relative_to(root))) for p in files}
services = root / 'detectors/apps/services'
modules = {p.stem: tree for p, tree in trees.items() if p.parent == services and p.stem != '__init__'}
graph, missing, ui_imports = {}, [], []
for name, tree in modules.items():
    edges = []
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.level == 1 and statement.module:
            target = statement.module.split('.')[0]
            edges.append(target)
            if target not in modules:
                missing.append([name, target])
    graph[name] = sorted(set(edges))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split('.')[0] in ('streamlit', 'atropos_ui'):
                ui_imports.append([name, node.module])
        if isinstance(node, ast.Import):
            ui_imports.extend([name, n.name] for n in node.names
                             if n.name.split('.')[0] in ('streamlit', 'atropos_ui'))

cycles = []


def visit(name, stack):
    if name in stack:
        cycles.append(stack[stack.index(name):] + [name])
        return
    for dependency in graph.get(name, []):
        visit(dependency, stack + [name])


for name in graph:
    visit(name, [])
output = {'python_files_parsed': len(trees), 'syntax': 'PASS', 'shared_service_modules': len(modules),
          'top_level_relative_import_graph': graph, 'missing_local_imports': missing,
          'top_level_import_cycles': cycles, 'ui_imports_in_shared_services': ui_imports,
          'scope_note': 'Top-level relative imports between service modules only; package __init__, dynamic imports and local-function imports are not cycle-checked.'}
print(json.dumps(output, indent=2))
raise SystemExit(bool(missing or cycles or ui_imports))
