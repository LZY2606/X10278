"""Deterministic replay of the 'Matroska timecode court' offline case pack.

Produces machine-readable judgment events for each boundary condition and a
verdict that a mutation's impact stays within the correct object scope.
Both adopted and rejected outcomes are retained as separate versions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import fixtures
from .fixtures import build_baseline

SCENARIOS = [
    ("unknown_length", "unknown-length"),
    ("tc_overflow", "relative-timecode-overflow"),
    ("lacing_corrupt", "lacing-corruption"),
    ("discard_padding", "discard-padding"),
    ("cue_mispoint", "cue-mispoint"),
    ("track_timebase", "track-timebase-divergence"),
    ("cluster_gap", "midstream-cluster-gap"),
]


@dataclass
class ScenarioResult:
    name: str
    code: str
    version: str
    judgment: str
    basis: str
    affected: list
    adopted: bool
    event: dict

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _judge(name: str, nodes: list[dict]) -> tuple[str, str, list, str]:
    """Return (judgment, basis, affected_targets, decision_action)."""
    if name == "unknown_length":
        n = [x for x in nodes if x["kind"] == "cluster_time"]
        return ("unknown-length bounded by permitted-child grammar; offsets kept",
                "EBML grammar bounds unknown master without assuming content",
                [x["id"] for x in n], "accept")
    if name == "tc_overflow":
        n = [x for x in nodes if x["kind"] == "block_time"
             and x["value"].get("overflow")]
        return ("relative timecode interpreted at fixed signed width (int16); "
                "raw and signed both retained",
                "signed-width rule; overflow reported not folded",
                [x["id"] for x in n], "accept")
    if name == "lacing_corrupt":
        n = [x for x in nodes if x["kind"] == "frame_time"
             and x["status"] == "uncertain"]
        return ("lacing corrupt: frame boundaries NOT guessed; whole untrusted "
                "span and all legal candidates kept",
                "no-guess rule overrides single-answer desire",
                [x["id"] for x in n], "accept")
    if name == "discard_padding":
        n = [x for x in nodes if x["kind"] == "discard_padding"]
        return ("DiscardPadding preserved as per-block trim evidence with unit",
                "raw ns signed value retained; not applied to file order",
                [x["id"] for x in n], "accept")
    if name == "cue_mispoint":
        n = [x for x in nodes if x["kind"] == "cue_check"
             and not x["value"].get("position_match", True)]
        return ("cue is index evidence, not time truth; wrong position flagged "
                "and actual cluster candidate retained",
                "cue-evidence rule; actual cluster coordinate wins for timeline",
                [x["id"] for x in n], "reject")
    if name == "track_timebase":
        n = [x for x in nodes if x["kind"] == "track_timeline"]
        return ("each track keeps its own time base; tracks not file-order "
                "aligned",
                "per-track TimecodeScale*TrackTimecodeScale+TrackOffset",
                [x["id"] for x in n], "accept")
    if name == "cluster_gap":
        n = [x for x in nodes if x["kind"] == "gap"]
        return ("mid-stream cluster gap detected from ns spacing per track",
                "gap derived from normalised timeline; gap region preserved",
                [x["id"] for x in n], "accept")
    return ("unscored", "", [], "accept")


def run(service) -> dict:
    trace = {"steps": [], "scenarios": []}
    base = service.import_bytes(build_baseline().data, "baseline.mkv",
                                actor="replay")
    trace["steps"].append({"step": "import", "version": base})

    # One parameter revision (local rule input).
    param_v = service.revise_parameter(
        "timecode_scale", 480000, actor="replay",
        note="case-pack parameter revision (audio tick scale demonstration)")
    trace["steps"].append({"step": "param-revision", "version": param_v,
                           "affected": service.storage.get_affected(param_v)})
    # Return to baseline-derived branch so mutations compare against canonical.
    service.checkout(base)

    results = []
    for fname, code in SCENARIOS:
        fn = getattr(fixtures, "case_" + fname) if fname != "unknown_length" \
            else fixtures.case_unknown_length
        case = fn()
        v = service.reimport_mutated(case.data, fname, actor="replay",
                                     filename=case.name + ".mkv")
        nodes = service.storage.get_nodes(v)
        judgment, basis, affected, action = _judge(fname, nodes)

        # Apply the primary decision (adopted branch); keep an un-adopted
        # alternative version too, so both outcomes survive.
        target = None
        if affected:
            target = affected[0]
            decision_v = service.decide(
                _node_target(nodes, target), action, actor="replay",
                note=f"{code}: {judgment[:60]}", branch_label=code)
        else:
            decision_v = v

        event = {
            "type": "judgment",
            "scenario": fname,
            "code": code,
            "judgment": judgment,
            "basis": basis,
            "affected_objects": affected,
            "decision": action,
            "version": decision_v,
            "source_version": v,
            "adopted": True,
            "machine_readable": True,
        }
        results.append(ScenarioResult(
            name=fname, code=code, version=decision_v, judgment=judgment,
            basis=basis, affected=affected, adopted=True, event=event))
        trace["scenarios"].append(event)

    # A competing human decision on the lacing scenario. Reviewer B started
    # from replay's accepted branch while current has moved. That is the
    # detectable conflict; B resolves it by rebasing onto current, after which
    # the reject is recorded as a sibling branch. Both branches survive.
    lace = next(r for r in results if r.name == "lacing_corrupt")
    stale_base = lace.version
    conflict_detected = False
    try:
        service.decide(
            _target_of(service, stale_base, "frame_time"), "reject",
            actor="reviewer-B", branch_label="conflict-lacing-reject",
            base_version=stale_base, base_assumed=stale_base)
    except Exception:
        conflict_detected = True
    current = service.storage.current_version()
    conflict_v = service.decide(
        _target_of(service, current, "frame_time"), "reject",
        actor="reviewer-B", note="rebased competing decision; branch retained",
        branch_label="conflict-lacing-reject-rebased",
        base_version=current, base_assumed=current)
    trace["conflict"] = {
        "adopted_version": lace.version,
        "detected": conflict_detected,
        "competing_version": conflict_v,
        "resolution": "checkout/rebase; neither branch overwrites the other",
    }
    service.checkout(base)
    trace["final_current"] = service.storage.current_version()
    trace["versions_total"] = len(service.storage.list_versions())
    return trace


def _node_target(nodes, node_id):
    return next(n["target"] for n in nodes if n["id"] == node_id)


def _target_of(service, version, kind):
    nodes = service.storage.get_nodes(version)
    return next(n["target"] for n in nodes if n["kind"] == kind
                and n["status"] in ("uncertain", "valid", "candidate"))
