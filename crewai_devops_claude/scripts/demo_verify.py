"""Check that a report identifies both demo root causes with evidence.

  python scripts/demo_verify.py runs/argocd-demo/report.json
Exit 0 when obs-demo-imagepull -> image_pull and obs-demo-crashloop -> crash_loop, each with evidence.
"""

from __future__ import annotations

import json
import sys

EXPECT = {
    "obs-demo-imagepull": ({"image_pull"}, ("k8s_event", "resource_tree", "live_resource"),
                           ("pull", "not found", "v0-does-not-exist", "imagepullbackoff", "errimagepull")),
    "obs-demo-crashloop": ({"crash_loop"}, ("container_log", "live_resource", "resource_tree", "k8s_event"),
                           ("redis_url", "crashloopbackoff", "exit", "back-off")),
}


def main(path: str) -> int:
    rep = json.load(open(path))
    apps = {a["app_name"]: a for a in rep["apps"]}
    ok = True
    for name, (cats, sources, needles) in EXPECT.items():
        a = apps.get(name)
        if a is None:
            print(f"FAIL {name}: not in report")
            ok = False
            continue
        ev = [e for e in a["evidence"] if e["source"] in sources]
        hit = [e for e in ev if any(n in e["detail"].lower() for n in needles)]
        good = a["root_cause_category"] in cats and bool(hit)
        ok &= good
        print(f"{'PASS' if good else 'FAIL'} {name}: category={a['root_cause_category']} "
              f"confidence={a['confidence']} evidence={len(a['evidence'])} matching={len(hit)}")
        for e in hit[:3]:
            print(f"     [{e['source']}] {e['reference']}: {e['detail'][:140]}")
    print("limitations:", *rep.get("limitations", []), sep="\n  - ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "runs/argocd-demo/report.json"))
