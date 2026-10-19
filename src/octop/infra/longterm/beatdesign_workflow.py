"""Explicit executable bindings for BeatDesign's real Canvas document format.

BeatDesign edges/referenceCardIds are visual or media lineage, not execution
dependencies. Octop instructions can be stored in an existing card's ``prompt``:

    ```octop-execution
    {"id":"research","skill":"agent.analyze","risk":"R0","depends_on":[]}
    ```

For generation prompts that must stay untouched, keep a separate manifest in
the exported envelope's ``octop_execution`` field. This is an Octop sidecar,
not a new field claimed to be accepted by BeatDesign's Command Kernel.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .execution import WorkflowValidationError, validate_workflow

_BLOCK = re.compile(r"```octop-execution\s*\n(.*?)\n```", re.DOTALL)


def import_beatdesign_workflow(document: dict[str, Any]) -> dict[str, Any]:
    """Import only explicitly declared card bindings and execution dependencies."""
    canvas = document.get("document", document)
    if not isinstance(canvas, dict) or not isinstance(canvas.get("cards"), list):
        raise WorkflowValidationError("BeatDesign canvas document.cards is required")
    cards = canvas["cards"]
    by_id: dict[str, dict[str, Any]] = {}
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("id"), str):
            raise WorkflowValidationError("BeatDesign cards need string IDs")
        if card["id"] in by_id:
            raise WorkflowValidationError("duplicate BeatDesign card ID")
        by_id[card["id"]] = card
    sidecar = document.get("octop_execution")
    raw_nodes: list[dict[str, Any]] = []
    options: dict[str, Any] = {}
    if sidecar is not None:
        if not isinstance(sidecar, dict) or sidecar.get("schema_version") != 1:
            raise WorkflowValidationError("octop_execution sidecar schema_version must be 1")
        if not isinstance(sidecar.get("nodes"), list):
            raise WorkflowValidationError("octop_execution.nodes must be a list")
        raw_nodes = sidecar["nodes"]
        options = {
            key: sidecar[key]
            for key in (
                "max_cycles",
                "max_actions",
                "max_parallel",
                "max_tokens",
                "timeout_seconds",
            )
            if key in sidecar
        }
    else:
        for card in cards:
            prompt = card.get("prompt", "")
            blocks = _BLOCK.findall(prompt) if isinstance(prompt, str) else []
            if len(blocks) > 1:
                raise WorkflowValidationError("one octop-execution block per card is allowed")
            if blocks:
                try:
                    binding = json.loads(blocks[0])
                except json.JSONDecodeError as exc:
                    raise WorkflowValidationError("invalid octop-execution JSON") from exc
                if not isinstance(binding, dict):
                    raise WorkflowValidationError("octop-execution binding must be an object")
                raw_nodes.append({**binding, "card_id": card["id"]})
    nodes: list[dict[str, Any]] = []
    used_cards: set[str] = set()
    for binding in raw_nodes:
        if not isinstance(binding, dict):
            raise WorkflowValidationError("execution binding must be an object")
        card_id = binding.get("card_id")
        if not isinstance(card_id, str) or card_id not in by_id or card_id in used_cards:
            raise WorkflowValidationError("execution binding needs one unique existing card_id")
        if "depends_on" not in binding:
            raise WorkflowValidationError(
                "depends_on must be explicitly declared; Canvas edges are visual"
            )
        if not isinstance(binding.get("skill"), str) or not binding["skill"]:
            raise WorkflowValidationError("execution binding needs an explicit skill")
        used_cards.add(card_id)
        node = {key: value for key, value in binding.items() if key != "card_id"}
        node["beatdesign_card_id"] = card_id
        nodes.append(node)
    workflow = {"nodes": nodes, **options}
    workflow["nodes"] = validate_workflow(workflow)
    return workflow
