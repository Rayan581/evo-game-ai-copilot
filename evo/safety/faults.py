"""Deliberate fault injection for the retry / duplicate-prevention tests.

Set EVO_FAULTS="run_experiment:after:1,search_knowledge:before:2" (or pass
--inject-fault on the CLI). Each spec makes the named tool raise a transient
error `count` times, either before the tool acts or after its side effect has
happened but before the result is recorded (the nasty case for duplicates).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class TransientToolError(RuntimeError):
    pass


@dataclass
class FaultInjector:
    specs: dict[tuple[str, str], int] = field(default_factory=dict)
    fired: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str | None) -> "FaultInjector":
        specs: dict[tuple[str, str], int] = {}
        for part in (text or "").split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split(":")
            tool = bits[0]
            phase = bits[1] if len(bits) > 1 else "before"
            count = int(bits[2]) if len(bits) > 2 else 1
            if phase not in ("before", "after"):
                raise ValueError(f"bad fault phase '{phase}' (use before|after)")
            specs[(tool, phase)] = count
        return cls(specs)

    @classmethod
    def from_env(cls) -> "FaultInjector":
        return cls.parse(os.environ.get("EVO_FAULTS"))

    def maybe_fail(self, tool: str, phase: str) -> None:
        left = self.specs.get((tool, phase), 0)
        if left > 0:
            self.specs[(tool, phase)] = left - 1
            self.fired.append(f"{tool}:{phase}")
            raise TransientToolError(f"injected fault in {tool} ({phase} side effect)")
