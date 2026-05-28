#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional


VALID_FAMILIES = {"dimmer", "color", "position", "other"}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _slugify(text: Any, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
    return slug or fallback


def _humanize(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = raw.replace(".", " ").replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in raw.split())


def _unique_group_id(base_id: str, seen: set[str]) -> str:
    base = _slugify(base_id, "group").replace("_", ".")
    if base not in seen:
        seen.add(base)
        return base
    idx = 2
    while f"{base}.{idx}" in seen:
        idx += 1
    final = f"{base}.{idx}"
    seen.add(final)
    return final


def _normalize_presets(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        preset = {
            "label": label,
            "min": max(0, min(255, _safe_int(item.get("min"), 0))),
            "max": max(0, min(255, _safe_int(item.get("max"), 255))),
        }
        if preset["max"] < preset["min"]:
            preset["min"], preset["max"] = preset["max"], preset["min"]
        out.append(preset)
    return out


def _normalize_channel(raw: Any, default_role: str = "value") -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    offset_raw = raw.get("offset", raw.get("channel", raw.get("dmxchannel")))
    if offset_raw is None:
        return None
    channel = {
        "role": _slugify(raw.get("role") or default_role, default_role),
        "offset": max(0, _safe_int(offset_raw, 0)),
        "default": max(0, min(255, _safe_int(raw.get("default"), 0))),
    }
    ui = str(raw.get("ui") or "").strip().lower()
    if ui:
        channel["ui"] = ui
    if raw.get("range_deg") is not None:
        channel["range_deg"] = _safe_int(raw.get("range_deg"), 0)
    elif raw.get("range") is not None:
        channel["range_deg"] = _safe_int(raw.get("range"), 0)
    presets = _normalize_presets(raw.get("presets"))
    if presets:
        channel["presets"] = presets
    return channel


def _normalize_group(raw: Any, index: int, seen_ids: set[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    family = str(raw.get("family") or "").strip().lower()
    if family not in VALID_FAMILIES:
        return None

    raw_channels = raw.get("channels")
    if not isinstance(raw_channels, list):
        raw_channels = []
    channels: List[Dict[str, Any]] = []
    for item in raw_channels:
        normalized = _normalize_channel(item)
        if normalized:
            channels.append(normalized)
    if not channels:
        return None

    kind = str(raw.get("kind") or "").strip().lower()
    group_id = _unique_group_id(
        str(raw.get("id") or f"{family}.{kind or index + 1}"),
        seen_ids,
    )
    label = str(raw.get("label") or "").strip() or _humanize(group_id)
    group: Dict[str, Any] = {
        "id": group_id,
        "family": family,
        "label": label,
        "channels": channels,
    }
    if kind:
        group["kind"] = kind
    return group


def _pick_group(groups: List[Dict[str, Any]], group_id: str) -> Optional[Dict[str, Any]]:
    wanted = str(group_id or "").strip().lower()
    for group in groups:
        if str(group.get("id") or "").strip().lower() == wanted:
            return group
    return None


def _derive_primary(groups: List[Dict[str, Any]], raw_primary: Any) -> Dict[str, str]:
    primary: Dict[str, str] = {}
    if isinstance(raw_primary, dict):
        for family in ("dimmer", "color", "position"):
            chosen = raw_primary.get(family)
            match = _pick_group(groups, str(chosen or ""))
            if match and str(match.get("family")) == family:
                primary[family] = str(match["id"])
    for family in ("dimmer", "color", "position"):
        if family in primary:
            continue
        for group in groups:
            if str(group.get("family")) == family:
                primary[family] = str(group.get("id") or "")
                break
    return primary


def _group_channel_by_role(group: Optional[Dict[str, Any]], role: str) -> Optional[Dict[str, Any]]:
    if not isinstance(group, dict):
        return None
    wanted = str(role or "").strip().lower()
    for channel in group.get("channels") or []:
        if str(channel.get("role") or "").strip().lower() == wanted:
            return channel
    return None


def _first_channel(group: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(group, dict):
        return None
    channels = group.get("channels") or []
    return channels[0] if channels else None


def _build_legacy_functions(groups: List[Dict[str, Any]], primary: Dict[str, str]) -> Dict[str, Any]:
    functions: Dict[str, Any] = {}

    dimmer_group = _pick_group(groups, primary.get("dimmer", ""))
    dimmer_channel = _group_channel_by_role(dimmer_group, "level") or _first_channel(dimmer_group)
    if dimmer_channel:
        functions["dimmer"] = {"channel": int(dimmer_channel["offset"])}

    color_group = _pick_group(groups, primary.get("color", ""))
    if color_group:
        rgb_map: Dict[str, int] = {}
        for role in ("red", "green", "blue"):
            ch = _group_channel_by_role(color_group, role)
            if ch:
                rgb_map[role] = int(ch["offset"])
        if rgb_map:
            functions["rgb"] = rgb_map

    position_group = _pick_group(groups, primary.get("position", ""))
    if position_group:
        pos_map: Dict[str, Any] = {}
        for role in ("pan", "tilt"):
            ch = _group_channel_by_role(position_group, role)
            if ch:
                pos_entry = {"channel": int(ch["offset"])}
                if ch.get("range_deg") is not None:
                    pos_entry["range"] = int(ch["range_deg"])
                pos_map[role] = pos_entry
        if pos_map:
            functions["position"] = pos_map

    for group in groups:
        family = str(group.get("family") or "")
        if family != "other":
            continue
        kind = str(group.get("kind") or group.get("id") or "").strip().lower()
        channel = _group_channel_by_role(group, "value") or _group_channel_by_role(group, "level") or _first_channel(group)
        if not channel:
            continue
        entry = {"channel": int(channel["offset"])}
        if kind == "focus":
            functions["focus"] = entry
        else:
            functions.setdefault("extra", {})[kind or str(group.get("id") or "other")] = entry

    return functions


def _build_runtime_fixture(
    *,
    meta: Dict[str, Any],
    footprint: int,
    groups: List[Dict[str, Any]],
    raw_primary: Any,
    source_file: str,
    source_format: str,
) -> Dict[str, Any]:
    normalized_meta = {
        "model": str(meta.get("model") or "").strip(),
        "vendor": str(meta.get("vendor") or "").strip(),
        "mode": str(meta.get("mode") or "").strip(),
        "author": str(meta.get("author") or "").strip(),
    }
    primary = _derive_primary(groups, raw_primary)
    runtime = {
        "schema": "ddmx.fixture/v1",
        "meta": normalized_meta,
        "info": {
            "model": normalized_meta["model"],
            "vendor": normalized_meta["vendor"],
            "mode": normalized_meta["mode"],
            "author": normalized_meta["author"],
        },
        "footprint": max(1, int(footprint or 1)),
        "addr_count": max(1, int(footprint or 1)),
        "primary": primary,
        "groups": groups,
        "source_file": source_file,
        "source_format": source_format,
    }
    runtime["functions"] = _build_legacy_functions(groups, primary)
    return runtime


def load_fixture_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("fixture json root must be an object")
    schema = str(raw.get("schema") or "").strip()
    if schema and schema != "ddmx.fixture/v1":
        raise ValueError(f"unsupported fixture schema: {schema}")
    raw_groups = raw.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("fixture json must define a non-empty groups array")
    seen_ids: set[str] = set()
    groups: List[Dict[str, Any]] = []
    for idx, group in enumerate(raw_groups):
        normalized = _normalize_group(group, idx, seen_ids)
        if normalized:
            groups.append(normalized)
    if not groups:
        raise ValueError("fixture json does not contain any valid groups")
    meta = raw.get("meta")
    if not isinstance(meta, dict):
        meta = raw.get("information") if isinstance(raw.get("information"), dict) else {}
    footprint = raw.get("footprint", raw.get("addr_count", 1))
    return _build_runtime_fixture(
        meta=meta if isinstance(meta, dict) else {},
        footprint=_safe_int(footprint, 1),
        groups=groups,
        raw_primary=raw.get("primary"),
        source_file=os.path.basename(path),
        source_format="json",
    )


def load_fixture_xml(path: str) -> Dict[str, Any]:
    tree = ET.parse(path)
    root = tree.getroot()

    info_node = root.find("information")
    meta = {
        "model": info_node.findtext("model") if info_node is not None else "",
        "vendor": info_node.findtext("vendor") if info_node is not None else "",
        "mode": info_node.findtext("mode") if info_node is not None else "",
        "author": info_node.findtext("author") if info_node is not None else "",
    }

    funcs = root.find("functions")
    groups: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    family_counts: Dict[str, int] = {"dimmer": 0, "color": 0, "position": 0, "focus": 0, "other": 0}

    if funcs is not None:
        for child in funcs:
            tag = str(child.tag or "").strip().lower()
            if not tag:
                continue
            if tag == "dimmer" and "dmxchannel" in child.attrib:
                family_counts["dimmer"] += 1
                idx = family_counts["dimmer"]
                group_id = _unique_group_id("dimmer.main" if idx == 1 else f"dimmer.{idx}", seen_ids)
                groups.append({
                    "id": group_id,
                    "family": "dimmer",
                    "label": "Main Dimmer" if idx == 1 else f"Dimmer {idx}",
                    "channels": [
                        {
                            "role": "level",
                            "offset": _safe_int(child.attrib.get("dmxchannel"), 0),
                            "default": 0,
                            "ui": "slider",
                        }
                    ],
                })
                continue

            if tag == "rgb":
                family_counts["color"] += 1
                idx = family_counts["color"]
                channels: List[Dict[str, Any]] = []
                defaults = {"red": 255, "green": 255, "blue": 255}
                for role in ("red", "green", "blue"):
                    node = child.find(role)
                    if node is not None and "dmxchannel" in node.attrib:
                        channels.append({
                            "role": role,
                            "offset": _safe_int(node.attrib.get("dmxchannel"), 0),
                            "default": defaults[role],
                        })
                if channels:
                    group_id = _unique_group_id("color.main" if idx == 1 else f"color.{idx}", seen_ids)
                    groups.append({
                        "id": group_id,
                        "family": "color",
                        "label": "Color" if idx == 1 else f"Color {idx}",
                        "channels": channels,
                    })
                continue

            if tag == "position":
                family_counts["position"] += 1
                idx = family_counts["position"]
                channels = []
                for role in ("pan", "tilt"):
                    node = child.find(role)
                    if node is None or "dmxchannel" not in node.attrib:
                        continue
                    range_node = node.find("range")
                    channel = {
                        "role": role,
                        "offset": _safe_int(node.attrib.get("dmxchannel"), 0),
                        "default": 128,
                    }
                    if range_node is not None and "range" in range_node.attrib:
                        channel["range_deg"] = _safe_int(range_node.attrib.get("range"), 0)
                    channels.append(channel)
                if channels:
                    group_id = _unique_group_id("position.main" if idx == 1 else f"position.{idx}", seen_ids)
                    groups.append({
                        "id": group_id,
                        "family": "position",
                        "label": "Pan / Tilt" if idx == 1 else f"Position {idx}",
                        "channels": channels,
                    })
                continue

            if "dmxchannel" not in child.attrib:
                continue

            kind = "focus" if tag == "focus" else _slugify(tag, "other")
            family_counts["focus" if kind == "focus" else "other"] += 1
            count = family_counts["focus" if kind == "focus" else "other"]
            base_id = f"other.{kind}" if count == 1 else f"other.{kind}.{count}"
            label = _humanize(kind) if count == 1 else f"{_humanize(kind)} {count}"
            groups.append({
                "id": _unique_group_id(base_id, seen_ids),
                "family": "other",
                "kind": kind,
                "label": label,
                "channels": [
                    {
                        "role": "value",
                        "offset": _safe_int(child.attrib.get("dmxchannel"), 0),
                        "default": 0 if kind not in ("focus",) else 128,
                        "ui": "slider",
                    }
                ],
            })

    return _build_runtime_fixture(
        meta=meta,
        footprint=_safe_int(root.attrib.get("dmxaddresscount"), 1),
        groups=groups or [{
            "id": "other.channel",
            "family": "other",
            "kind": "generic",
            "label": "Channel",
            "channels": [{"role": "value", "offset": 0, "default": 0, "ui": "slider"}],
        }],
        raw_primary=None,
        source_file=os.path.basename(path),
        source_format="xml",
    )


def load_fixture_file(path: str) -> Dict[str, Any]:
    lower = path.lower()
    if lower.endswith(".fixture.json"):
        return load_fixture_json(path)
    if lower.endswith(".xml"):
        return load_fixture_xml(path)
    raise ValueError(f"unsupported fixture file: {path}")
