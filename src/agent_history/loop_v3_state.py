"""Second frozen loop-state body schema for pi v3, still framed with integer v=1.

Producer bin/loop-state SHA-256:
052d7fd5702b849572c826906d0fe48c505eb5c2af8972ebb64477f85a6c440f.
Selected by open.charter_sha256, never a date or a relaxed legacy validator.
No runtime dependency on the producer or its checkout.
"""

BY_VALUES = ("root", "ext", "daemon")
TIER = ("routine", "guarded")
SCHEMA = {
    "open": (
        {"charter_sha256": "str", "root": ("llm",), "root_model": "str", "first_wave": "strlist"},
        {"tier": TIER},
    ),
    "admit": (
        {"task": "str", "source": ("first-wave", "loop-created"), "owned": "owned", "accept": "str"},
        {"tier": TIER, "surfaces": "strlist"},
    ),
    "dispatch": (
        {"lane": "str", "task": "str", "agent": "str", "run": "str", "base": "str"},
        {
            "deadline": "str",
            "model": "str",
            "effort": "str",
            "surface": "str",
            "tier": TIER,
            "tasks": "strlist",
            "kind": ("work", "review"),
            "recovery_of": "str",
        },
    ),
    "return": (
        {"lane": "str", "run": "str", "status": ("complete", "partial", "blocked", "failed")},
        {
            "sha": "str?",
            "landed": "bool",
            "check": "str",
            "exit": "int?",
            "ci": "str?",
            "coderabbit": "coderabbit",
            "base": "str",
            "questions": "strlist",
            "artifact": "artifact",
        },
    ),
    "accept": ({"task": "str", "accepted": "bool", "reason": "str"}, {"lane": "str"}),
    "park": (
        {
            "task": "str",
            "reason": "str",
            "needs": ("owner", "authority", "evidence-later", "dependency", "defect", "budget"),
        },
        {"lane": "str", "until": "str"},
    ),
    "land": (
        {"task": "str", "sha": "str", "gate": "str", "mode": ("after-green", "pre-green")},
        {"lane": "str", "ci": "str"},
    ),
    "revert": (
        {"sha": "str", "reverted_by": "str", "reason": ("ci-red", "ci-timeout", "composed-red", "root-decision")},
        {"task": "str"},
    ),
    "watch": ({"op": ("start", "stop"), "what": "str", "deadline": "str"}, {}),
    "heartbeat": ({"at": "str"}, {}),
    "judgement": ({"text": "str"}, {"task": "str", "lane": "str"}),
    "close": ({"reason": ("nothing-admissible", "owner-stop", "blocked", "budget")}, {}),
}
