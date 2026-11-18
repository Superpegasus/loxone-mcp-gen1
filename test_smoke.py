"""Smoke test (read only): python test_smoke.py <path-to-.Loxone-file>

Parses the project, renders every page and block, the text view, the analysis and a trace
of the first logic block found. Exits with an error if anything fails.
"""
import json
import sys

from analyze import analyze, lint_layout
from loxproject import Project
import textview


def show(title, obj, limit=1500):
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)
    print(f"\n===== {title} ({len(s)} chars)\n{s[:limit]}")


pr = Project(sys.argv[1])
show("Summary", pr.summary(), 1000)

# every page and every block must render without errors
sizes = {p.title: len(json.dumps(pr.page_view(p), ensure_ascii=False)) for p in pr.pages}
for b in pr.blocks.values():
    pr.block_view(b)
print("\npage sizes (chars):", sizes)

refs = sum(1 for b in pr.blocks.values() if pr.is_ref(b))
print(f"placeholders: {refs}, connections: {len(pr.edges)}")

text = textview.render(pr)
show("Text view (start)", text, 1200)
show("Analysis", {"findings": len(analyze(pr)), "layout": len(lint_layout(pr))})

logic = next((b for b in pr.blocks.values() if b.page is not None and not pr.is_ref(b) and pr.out_edges.get(b.uuid)), None)
if logic is not None:
    show(f"Trace forward from {logic.title}", pr.trace(logic, None, "forward", 3), 1200)
print("\nOK")
