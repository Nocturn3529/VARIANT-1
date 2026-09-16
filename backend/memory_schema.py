"""Structured schema for VARIANT-1 long-term memory entries.

Model extraction uses `MemoryRecord` as a dependency-light, typed proposal
value. Approved persistence, revisions, provenance, retrieval, and export live
in `MemoryStore`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import time
import uuid

# Allowed enums. Unknown values are coerced to a safe default rather than rejected,
# so a weak extraction model can't poison the store.
MEM_TYPES = {
    "profile",        # stable identity-level fact in the context-memory ledger
    "preference",     # how the user likes things ("prefers dark mode")
    "fact",           # durable factual info about the user/world
    "project",        # something the user is working on
    "goal",           # an aim/intention
    "skill",          # what the user (or VARIANT-1) can do
    "instruction",    # a standing instruction for how VARIANT-1 should behave
    "relationship",   # people/orgs in the user's life
    "event",          # a notable thing that happened (episodic-ish)
    "screen",         # something observed on screen
    "context",        # misc useful background
}
MEM_SUBJECTS = {"user", "assistant", "world"}
MEM_SOURCES = {"chat", "screen", "tool", "manual", "consolidation", "import", "loop"}
# Lifecycle states. Only active/pinned records are recallable;
# superseded/contradicted/expired records stay stored as history.
MEM_STATES = {"active", "superseded", "contradicted", "expired", "pinned"}

IMPORTANCE_MIN, IMPORTANCE_MAX = 1, 5
MAX_CONTENT = 400
MAX_CONTEXT = 240
MAX_TAGS = 8
MAX_TAG_LEN = 32
MAX_SOURCE_IDS = 8


@dataclass
class MemoryRecord:
    content: str                                   # canonical, self-contained statement (embedded)
    type: str = "fact"
    context: str = ""                              # short why/where it was learned
    importance: int = 3                            # 1 (minor) .. 5 (critical)
    confidence: float = 1.0                        # 0..1
    subject: str = "user"                          # user | assistant | world
    source: str = "chat"                           # how it was captured
    tags: list = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    version: int = 1
    # Provenance + lifecycle (additive and round-trip safe):
    source_ids: list = field(default_factory=list)  # run/episode ids that created it
    state: str = "active"                           # MEM_STATES
    valid_from: float = 0.0                         # 0 = always
    valid_until: float = 0.0                        # 0 = no expiry
    confirmation: int = 0                           # times re-confirmed
    superseded_by: str = ""                         # id of the replacing record

    # ---- validation / cleanup -------------------------------------------
    def normalize(self) -> "MemoryRecord":
        """Clamp/repair fields in place and return self. Always call before storing."""
        self.content = " ".join(str(self.content or "").split())[:MAX_CONTENT]
        self.context = " ".join(str(self.context or "").split())[:MAX_CONTEXT]
        t = str(self.type or "fact").strip().lower()
        self.type = t if t in MEM_TYPES else "fact"
        s = str(self.subject or "user").strip().lower()
        self.subject = s if s in MEM_SUBJECTS else "user"
        src = str(self.source or "chat").strip().lower()
        self.source = src if src in MEM_SOURCES else "chat"
        try:
            self.importance = max(IMPORTANCE_MIN, min(IMPORTANCE_MAX, int(round(float(self.importance)))))
        except (TypeError, ValueError):
            self.importance = 3
        try:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 1.0
        clean = []
        for tag in (self.tags or []):
            tg = " ".join(str(tag).split()).lower()[:MAX_TAG_LEN]
            if tg and tg not in clean:
                clean.append(tg)
        self.tags = clean[:MAX_TAGS]
        st = str(self.state or "active").strip().lower()
        self.state = st if st in MEM_STATES else "active"
        sids = []
        for sid in (self.source_ids or []):
            s2 = " ".join(str(sid).split())[:80]
            if s2 and s2 not in sids:
                sids.append(s2)
        self.source_ids = sids[:MAX_SOURCE_IDS]
        for k in ("valid_from", "valid_until"):
            try:
                setattr(self, k, max(0.0, float(getattr(self, k) or 0.0)))
            except (TypeError, ValueError):
                setattr(self, k, 0.0)
        try:
            self.confirmation = max(0, int(self.confirmation or 0))
        except (TypeError, ValueError):
            self.confirmation = 0
        self.superseded_by = " ".join(str(self.superseded_by or "").split())[:64]
        return self

    @property
    def valid(self) -> bool:
        return bool(self.content.strip())

    def dedup_key(self) -> str:
        """Cheap key for exact/near duplicate detection (no model needed)."""
        return " ".join(self.content.lower().split())

    # ---- serialization --------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryRecord":
        d = dict(d or {})
        allowed = {f for f in cls.__dataclass_fields__}  # noqa: E1101
        rec = cls(**{k: v for k, v in d.items() if k in allowed and v is not None})
        return rec.normalize()
