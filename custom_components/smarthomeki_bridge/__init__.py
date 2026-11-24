"""SmartHomeKI MCP bridge: read-only diagnostics and explicitly approved changes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone

from aiohttp import web
from . import extensions as ext
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.core import callback
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)
DOMAIN = "smarthomeki_bridge"
DASHBOARD = "smarthomeki"
BUTTON = "input_button.smarthomeki_approve"
READ = "get_smarthomeki_dashboard"
PREVIEW = "preview_smarthomeki_card"
PROPOSE = "propose_smarthomeki_card"
STATUS = "get_smarthomeki_proposal_status"
SIDEBAR = "propose_smarthomeki_sidebar_visible"
VIEW_PREVIEW = "preview_smarthomeki_view"
VIEW_PROPOSE = "propose_smarthomeki_view"
EDIT_PREVIEW = "preview_smarthomeki_card_edit"
EDIT_PROPOSE = "propose_smarthomeki_card_edit"
ENTITIES = "get_smarthomeki_entities"
DIAGNOSTICS = "get_smarthomeki_diagnostics"
CARD_CHANGE_PREVIEW = "preview_smarthomeki_card_change"
CARD_CHANGE_PROPOSE = "propose_smarthomeki_card_change"
ACTION_PREVIEW = "preview_smarthomeki_action"
ACTION_PROPOSE = "propose_smarthomeki_action"
SAFE_ACTIONS = {
    "light": {"turn_on", "turn_off"},
    "switch": {"turn_on", "turn_off"},
    "input_boolean": {"turn_on", "turn_off"},
    "automation": {"turn_on", "turn_off"},
}
EXCLUDED_READ_DOMAINS = {"person", "device_tracker", "camera", "lock", "alarm_control_panel", "zone", "conversation"}
MAX_RESULTS = 250
MAX_VIEW_BYTES = 150000
TTL = 120
MAX_CARD_BYTES = 50000


def digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def content(value, error=False):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": error}


def valid_card(card):
    return (isinstance(card, dict) and isinstance(card.get("type"), str)
            and bool(card["type"]) and len(json.dumps(card, ensure_ascii=False).encode()) <= MAX_CARD_BYTES)


def validate_args(args):
    if not isinstance(args, dict) or set(args) != {"dashboard", "view_path", "card"} or args.get("dashboard") != DASHBOARD:
        return "Only smarthomeki with dashboard, view_path, card is allowed"
    if not isinstance(args.get("view_path"), str) or not 0 < len(args["view_path"]) <= 100:
        return "Invalid view_path"
    if not valid_card(args.get("card")):
        return "Invalid or oversized card"
    return None


def find_exact_card(config, path, old_card):
    view = find_view(config, path)
    if view is None:
        raise ValueError("View not found")
    containers = []
    if isinstance(view.get("cards"), list):
        containers.append(view["cards"])
    for section in view.get("sections", []):
        if isinstance(section, dict) and isinstance(section.get("cards"), list):
            containers.append(section["cards"])
    matches = [(cards, index) for cards in containers for index, card in enumerate(cards)
               if card == old_card]
    if len(matches) != 1:
        raise ValueError("Expected exactly one matching original card; nothing changed")
    return matches[0]


def validate_edit_args(args):
    if not isinstance(args, dict) or set(args) != {"dashboard", "view_path", "old_card", "new_card"}:
        return "Expected dashboard, view_path, old_card, new_card"
    if args["dashboard"] != DASHBOARD or args["view_path"] not in ("home", "calendar", "tablet"):
        return "Only SmartHomeKI Home, Calendar or Tablet calendar cards may be edited"
    old, new = args["old_card"], args["new_card"]
    if not valid_card(old) or not valid_card(new):
        return "Both cards must be valid"
    if args["view_path"] == "tablet":
        if old.get("type") != "custom:week-planner-card" or new.get("type") != old["type"]:
            return "Tablet edit requires the existing week-planner-card"
        # Only presentation settings may change. Protect calendar sources and identity.
        allowed = {"columns", "texts", "dayFormat", "card_mod", "grid_options"}
        if {k: v for k, v in old.items() if k not in allowed} != {k: v for k, v in new.items() if k not in allowed}:
            return "Tablet edit may only change week-planner presentation settings"
        columns = new.get("columns")
        if not isinstance(columns, dict) or set(columns) != {"extraLarge", "large", "medium", "small", "extraSmall"}:
            return "Tablet requires all five responsive column settings"
        if any(type(value) is not int or not 1 <= value <= 7 for value in columns.values()):
            return "Tablet column counts must be integers from 1 to 7"
        if new.get("grid_options") != old.get("grid_options"):
            return "Tablet grid options must remain unchanged"
        if "texts" in new and (not isinstance(new["texts"], dict) or
                any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 100
                    for k, v in new["texts"].items())):
            return "Invalid Tablet calendar translations"
        if "card_mod" in new and (not isinstance(new["card_mod"], dict) or
                not isinstance(new["card_mod"].get("style"), str) or
                len(new["card_mod"]["style"]) > 5000):
            return "Invalid Tablet calendar styling"
        return None
    if old.get("type") != "calendar":
        return "Original must be a valid built-in calendar card"
    if new.get("type") == "calendar":
        if not isinstance(new.get("entities"), list) or not new["entities"] or not all(
                isinstance(e, str) and e.startswith("calendar.") for e in new["entities"]):
            return "Calendar entities must be a nonempty list of calendar.* IDs"
        return None
    # Only permit the exact pre-agreed migration of the Home weekly calendar.
    # Other custom cards, other views, and arbitrary settings are not permitted.
    expected_old = {
        "type": "calendar", "title": "Next 7 days",
        "entities": ["calendar.smarthomeki_events", "calendar.smarthomeki_birthdays", "calendar.smarthomeki_holidays"],
        "initial_view": "listWeek", "show_add_event": False,
        "grid_options": {"columns": 24},
    }
    expected_new = {
        "type": "custom:atomic-calendar-revive", "name": "Next 7 days",
        "entities": [
            {"entity": "calendar.smarthomeki_events"},
            {"entity": "calendar.smarthomeki_birthdays"},
            {"entity": "calendar.smarthomeki_holidays"},
        ],
        "maxDaysToShow": 7, "startDaysAhead": 0,
        "defaultMode": "Event", "showNoEventDays": True,
        "showEventDate": True, "sortBy": "start",
        "grid_options": {"columns": 24},
    }
    if args["view_path"] != "home" or old != expected_old or new != expected_new:
        return "Custom card migration only allows the approved Home 7-day calendar configuration"
    return None



def safe_entity_summary(state):
    """Return minimal status; do not expose full attributes or precise location."""
    return {"entity_id": state.entity_id, "state": state.state,
            "name": str(state.attributes.get("friendly_name", state.entity_id))[:120],
            "unit": state.attributes.get("unit_of_measurement") if isinstance(state.attributes.get("unit_of_measurement"), str) else None,
            "last_changed": state.last_changed.isoformat()}


def list_safe_entities(hass, args):
    if not isinstance(args, dict) or set(args) - {"domain", "limit"}:
        raise ValueError("Only optional domain and limit accepted")
    domain = args.get("domain")
    limit = args.get("limit", 100)
    if domain is not None and (not isinstance(domain, str) or not domain.isidentifier() or domain in EXCLUDED_READ_DOMAINS):
        raise ValueError("Invalid or excluded domain")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValueError("limit must be 1..250")
    items = [x for x in sorted(hass.states.async_all(), key=lambda x: x.entity_id)
             if x.entity_id.split(".", 1)[0] not in EXCLUDED_READ_DOMAINS
             and (domain is None or x.entity_id.startswith(domain + "."))]
    return {"total": len(items), "returned": min(limit, len(items)),
            "entities": [safe_entity_summary(x) for x in items[:limit]]}


def get_diagnostics(hass, args):
    if args != {}:
        raise ValueError("Diagnostics accepts no arguments")
    states = [x for x in hass.states.async_all() if x.entity_id.split(".", 1)[0] not in EXCLUDED_READ_DOMAINS]
    unavailable = [x for x in states if x.state in ("unavailable", "unknown")]
    counts = {}
    for x in states:
        domain = x.entity_id.split(".", 1)[0]
        counts[domain] = counts.get(domain, 0) + 1
    return {"total": len(states), "domains": counts, "unavailable_total": len(unavailable),
            "unavailable": [safe_entity_summary(x) for x in unavailable[:MAX_RESULTS]],
            "truncated": len(unavailable) > MAX_RESULTS}


def validate_card_change(args):
    if not isinstance(args, dict) or set(args) != {"dashboard", "view_path", "operation", "old_card", "new_card"}:
        return "Expected dashboard, view_path, operation, old_card, new_card"
    if args["dashboard"] != DASHBOARD or not isinstance(args["view_path"], str) or len(args["view_path"]) > 100:
        return "Invalid dashboard or view"
    if args["operation"] not in ("edit", "remove") or not valid_card(args["old_card"]):
        return "Invalid operation or original card"
    if args["operation"] == "edit" and not valid_card(args["new_card"]):
        return "Invalid replacement card"
    if args["operation"] == "remove" and args["new_card"] is not None:
        return "Removal requires new_card=null"
    return None


def validate_action(hass, args):
    if not isinstance(args, dict) or set(args) != {"entity_id", "service"}:
        return "Expected entity_id and service"
    entity_id, service = args["entity_id"], args["service"]
    if not isinstance(entity_id, str) or entity_id.count(".") != 1 or not isinstance(service, str):
        return "Invalid entity or service"
    domain = entity_id.split(".", 1)[0]
    if service not in SAFE_ACTIONS.get(domain, set()):
        return "Action not allowed: only light, switch, input_boolean and automation on/off"
    if hass.states.get(entity_id) is None:
        return "Entity does not exist"
    if not hass.services.has_service(domain, service):
        return "Service not available"
    return None


def stage(state, hass, proposal):
    # Caller must hold state lock. No device or dashboard action occurs here.
    if state["pending"] is not None and time.monotonic() <= state["pending"]["expires"]:
        raise ValueError("Another proposal is awaiting approval")
    if hass.states.get(BUTTON) is None:
        raise ValueError("Approval button helper not found")
    pid = secrets.token_hex(8)
    state["pending"] = {"id": pid, "expires": time.monotonic() + TTL, **proposal}
    state["results"][pid] = {"status": "pending", "saved": False}
    return pid


def validate_view_args(args):
    """Allow layout/content replacement only for an existing SmartHomeKI tab."""
    if not isinstance(args, dict) or set(args) != {"dashboard", "view_path", "view"}:
        return "Expected dashboard, view_path, view"
    if args["dashboard"] != DASHBOARD or not isinstance(args["view_path"], str) or not 0 < len(args["view_path"]) <= 100:
        return "Invalid SmartHomeKI dashboard or view path"
    view = args["view"]
    if not isinstance(view, dict) or view.get("type") not in ("sections", "masonry"):
        return "Only sections and masonry layouts are supported"
    if len(json.dumps(view, ensure_ascii=False).encode()) > MAX_VIEW_BYTES:
        return "View too large"
    if view["type"] == "masonry":
        if set(view) != {"type", "cards"} or not isinstance(view["cards"], list):
            return "Masonry requires only type and cards"
        if len(view["cards"]) > 100 or not all(valid_card(c) for c in view["cards"]):
            return "Invalid masonry cards"
    else:
        if set(view) != {"type", "max_columns", "sections"}:
            return "Sections requires type, max_columns and sections"
        if type(view["max_columns"]) is not int or not 1 <= view["max_columns"] <= 4:
            return "Invalid max_columns"
        sections = view["sections"]
        if not isinstance(sections, list) or not 1 <= len(sections) <= 24:
            return "Invalid sections"
        for section in sections:
            if not isinstance(section, dict) or section.get("type") != "grid":
                return "Only grid sections allowed"
            if set(section) - {"type", "cards", "column_span"}:
                return "Unsupported section property"
            cards = section.get("cards")
            if not isinstance(cards, list) or len(cards) > 100 or not all(valid_card(c) for c in cards):
                return "Invalid section cards"
            if "column_span" in section and (type(section["column_span"]) is not int or not 1 <= section["column_span"] <= view["max_columns"]):
                return "Invalid column span"
    return None


def build_replacement(current, requested):
    # Preserve identity and navigation metadata; replace only layout and content.
    result = {k: copy.deepcopy(v) for k, v in current.items()
              if k not in ("type", "max_columns", "sections", "cards")}
    result.update(copy.deepcopy(requested))
    return result


async def get_config(hass):
    lovelace = hass.data.get(LOVELACE_DATA)
    if not lovelace:
        raise ValueError("Lovelace not loaded")

    # HA indexes dashboards by their URL path, not the storage filename.
    # Never select the default/production dashboard as a fallback.
    dashboard = lovelace.dashboards.get("smarthome-ki")
    if dashboard is None:
        for candidate in lovelace.dashboards.values():
            if getattr(candidate, "url_path", None) == "smarthome-ki":
                dashboard = candidate
                break
    if dashboard is None or dashboard.mode != MODE_STORAGE:
        raise ValueError("SmartHomeKI storage dashboard not found (URL path smarthome-ki)")

    config = await dashboard.async_load(False)
    if not isinstance(config, dict) or not isinstance(config.get("views"), list):
        raise ValueError("Invalid dashboard configuration")
    return dashboard, config


def find_view(config, path):
    for view in config["views"]:
        if isinstance(view, dict) and view.get("path") == path:
            return view
    return None


async def async_setup(hass, config):
    state = {"pending": None, "results": {}, "lock": asyncio.Lock()}
    state["settings_store"] = Store(hass, 1, "smarthomeki_bridge.settings", private=True)
    state["settings"] = await state["settings_store"].async_load() or ext.DEFAULTS.copy()
    hass.data[DOMAIN] = state
    hass.http.register_view(DashboardView())
    hass.http.register_view(MCPView())

    @callback
    def on_button(event):
        if event.data.get("entity_id") != BUTTON:
            return
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if new is None or (old is not None and old.state == new.state):
            return
        # An input_button's state is the timestamp of its latest press.
        if state["pending"] is not None:
            hass.async_create_task(apply_approved(hass, state))

    hass.bus.async_listen("state_changed", on_button)
    ext.setup_reports(hass, state)
    return True


async def apply_approved(hass, state):
    async with state["lock"]:
        proposal = state["pending"]
        if proposal is None:
            return
        state["pending"] = None  # One press can only consume one proposal.
        pid = proposal["id"]
        if time.monotonic() > proposal["expires"]:
            state["results"][pid] = {"status": "expired", "saved": False}
            return
        try:
            if proposal.get("kind") == "extension":
                await ext.apply(hass, state, proposal)
                state["results"][pid] = {"status": "executed", "saved": True}
            elif proposal.get("kind") == "safe_action":
                action = {"entity_id": proposal["entity_id"], "service": proposal["service"]}
                error = validate_action(hass, action)
                if error:
                    raise ValueError(error)
                domain = action["entity_id"].split(".", 1)[0]
                await hass.services.async_call(domain, action["service"],
                    {"entity_id": action["entity_id"]}, blocking=True)
                state["results"][pid] = {"status": "executed", "saved": True,
                    "entity_id": action["entity_id"], "service": action["service"]}
            else:
                await apply_dashboard_approved(hass, state, proposal, pid)
        except Exception as exc:
            _LOGGER.exception("SmartHomeKI proposal %s failed", pid)
            state["results"][pid] = {"status": "failed", "saved": False, "reason": str(exc)}
        await hass.services.async_call("persistent_notification", "create", {
            "title": "SmartHomeKI Änderung", "message": f"Vorgang {pid}: {state['results'][pid]['status']}"
        }, blocking=False)


async def apply_dashboard_approved(hass, state, proposal, pid):
            dashboard, current = await get_config(hass)
            if digest(current) != proposal["base_hash"]:
                raise ValueError("Dashboard changed since proposal; nothing saved")
            new_config = copy.deepcopy(current)
            if proposal.get("kind") == "sidebar_visible":
                # Allow exactly one existing kiosk-mode setting to change.
                kiosk = new_config.get("kiosk_mode")
                if not isinstance(kiosk, dict) or kiosk.get("hide_sidebar") is not True:
                    raise ValueError("Sidebar setting has changed; nothing saved")
                kiosk["hide_sidebar"] = False
            elif proposal.get("kind", "add_card") == "add_card":
                view = find_view(new_config, proposal["view_path"])
                if view is None:
                    raise ValueError("View no longer exists")
                if isinstance(view.get("cards"), list):
                    target = view["cards"]
                elif isinstance(view.get("sections"), list):
                    grids = [s for s in view["sections"] if isinstance(s, dict) and isinstance(s.get("cards"), list)]
                    if not grids:
                        raise ValueError("No section grid available")
                    target = grids[0]["cards"]
                else:
                    raise ValueError("Unsupported view layout")
                target.append(copy.deepcopy(proposal["card"]))
            elif proposal.get("kind") == "edit_calendar_card":
                cards, index = find_exact_card(new_config, proposal["view_path"], proposal["old_card"])
                cards[index] = copy.deepcopy(proposal["new_card"])
            elif proposal.get("kind") == "change_card":
                cards, index = find_exact_card(new_config, proposal["view_path"], proposal["old_card"])
                if proposal["operation"] == "remove":
                    cards.pop(index)
                else:
                    cards[index] = copy.deepcopy(proposal["new_card"])
            elif proposal.get("kind") == "replace_view":
                view = find_view(new_config, proposal["view_path"])
                if view is None:
                    raise ValueError("View no longer exists")
                new_view = build_replacement(view, proposal["view"])
                index = next(i for i, v in enumerate(new_config["views"]) if v is view)
                new_config["views"][index] = new_view
            else:
                raise ValueError("Unknown proposal type")
            # Separate durable backup; if backup fails, never save.
            backup = Store(hass, 1, "smarthomeki_bridge.backup_" + pid, private=True)
            await backup.async_save({"dashboard": DASHBOARD, "created_at": datetime.now(timezone.utc).isoformat(), "config": current})
            await dashboard.async_save(new_config)  # HA Lovelace API: updates cache and fires lovelace_updated.
            _, verified = await get_config(hass)
            if digest(verified) != digest(new_config):
                raise ValueError("Post-save verification failed; inspect dashboard and backup")
            state["results"][pid] = {"status": "saved", "saved": True, "backup": "smarthomeki_bridge.backup_" + pid}



class DashboardView(HomeAssistantView):
    url = "/api/smarthomeki_bridge/dashboard"
    name = "api:smarthomeki_bridge:dashboard"
    requires_auth = True

    async def get(self, request):
        try:
            _, config = await get_config(request.app["hass"])
            return self.json({"dashboard": DASHBOARD, "config": config})
        except ValueError as exc:
            return self.json_message(str(exc), status_code=404)


class MCPView(HomeAssistantView):
    url = "/api/smarthomeki_bridge/mcp"
    name = "api:smarthomeki_bridge:mcp"
    requires_auth = True

    async def post(self, request):
        if request.content_length and request.content_length > 200000:
            return self.json({"error": "Too large"}, status_code=413)
        try:
            raw = await request.read()
            if len(raw) > 200000:
                return self.json({"error": "Too large"}, status_code=413)
            msg = json.loads(raw)
        except (ValueError, UnicodeDecodeError, web.HTTPBadRequest):
            return self.json({"error": "Invalid JSON"}, status_code=400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self.json({"error": "Expected JSON-RPC 2.0"}, status_code=400)
        if "id" not in msg:
            return web.Response(status=202)
        rid = msg["id"]
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return self.error(rid, -32602, "Invalid params")
        hass = request.app["hass"]
        state = hass.data[DOMAIN]
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "smarthomeki-bridge", "version": "2.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            args = {"type": "object", "properties": {"dashboard": {"type": "string", "enum": [DASHBOARD]}, "view_path": {"type": "string"}, "card": {"type": "object"}}, "required": ["dashboard", "view_path", "card"], "additionalProperties": False}
            view_args = {"type": "object", "properties": {"dashboard": {"type": "string", "enum": [DASHBOARD]}, "view_path": {"type": "string"}, "view": {"type": "object"}}, "required": ["dashboard", "view_path", "view"], "additionalProperties": False}
            edit_args = {"type": "object", "properties": {
                "dashboard": {"type": "string", "enum": [DASHBOARD]},
                "view_path": {"type": "string", "enum": ["home", "calendar", "tablet"]},
                "old_card": {"type": "object"}, "new_card": {"type": "object"}},
                "required": ["dashboard", "view_path", "old_card", "new_card"], "additionalProperties": False}
            result = {"tools": [
                {"name": READ, "description": "Read only the SmartHomeKI Lovelace dashboard.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
                {"name": PREVIEW, "description": "Validate card proposal without saving.", "inputSchema": args, "annotations": {"readOnlyHint": True}},
                {"name": PROPOSE, "description": "Stage one card addition to SmartHomeKI. Does not save. Requires a separate physical/UI press of the configured Home Assistant approval input_button within 120 seconds; pressing the button commits the staged change. Always ask the user for explicit permission BEFORE calling this tool.", "inputSchema": args, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
                {"name": VIEW_PREVIEW, "description": "Read-only validation and summary of a complete replacement for any existing SmartHomeKI view. Does not save.", "inputSchema": view_args, "annotations": {"readOnlyHint": True}},
                {"name": VIEW_PROPOSE, "description": "Stage a complete replacement of any existing SmartHomeKI view. Requires explicit user permission before calling and a separate Home Assistant approval button press within 120 seconds. Replaces existing cards; creates backup before save.", "inputSchema": view_args, "annotations": {"readOnlyHint": False, "destructiveHint": True}},
                {"name": SIDEBAR, "description": "Stage ONLY setting kiosk_mode.hide_sidebar=false on SmartHomeKI. No save until the user presses the Home Assistant approval input_button within 120 seconds. Requires explicit user permission BEFORE invoking this tool. This makes the sidebar visible on ALL devices including tablets.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
                {"name": EDIT_PREVIEW, "description": "Read-only validation of exact calendar card replacement, including Tablet week-planner presentation; never saves.", "inputSchema": edit_args, "annotations": {"readOnlyHint": True}},
                {"name": EDIT_PROPOSE, "description": "Stage exact calendar card edit on SmartHomeKI Home, Calendar or Tablet. Requires explicit user permission BEFORE invoking, then a separate Home Assistant approval-button press within 120 seconds. Creates backup before saving.", "inputSchema": edit_args, "annotations": {"readOnlyHint": False, "destructiveHint": True}},
                {"name": ENTITIES, "description": "Read minimal device and sensor states; excludes location, camera, locks and alarm domains. No changes.",
                 "inputSchema": {"type": "object", "properties": {"domain": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 250}}, "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
                {"name": DIAGNOSTICS, "description": "Read-only count of device domains and unavailable/unknown entities.",
                 "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
                {"name": CARD_CHANGE_PREVIEW, "description": "Validate exact edit or removal of one SmartHomeKI card; no save.",
                 "inputSchema": {"type": "object", "properties": {"dashboard": {"type": "string", "enum": [DASHBOARD]}, "view_path": {"type": "string"}, "operation": {"type": "string", "enum": ["edit", "remove"]}, "old_card": {"type": "object"}, "new_card": {"type": ["object", "null"]}}, "required": ["dashboard", "view_path", "operation", "old_card", "new_card"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
                {"name": CARD_CHANGE_PROPOSE, "description": "Stage one exact SmartHomeKI card edit/removal. Requires explicit chat permission BEFORE staging and separate Home Assistant button approval within 120 seconds. Backs up dashboard.",
                 "inputSchema": {"type": "object", "properties": {"dashboard": {"type": "string", "enum": [DASHBOARD]}, "view_path": {"type": "string"}, "operation": {"type": "string", "enum": ["edit", "remove"]}, "old_card": {"type": "object"}, "new_card": {"type": ["object", "null"]}}, "required": ["dashboard", "view_path", "operation", "old_card", "new_card"], "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": True}},
                {"name": ACTION_PREVIEW, "description": "Read-only validation of a safe on/off action; never executes.",
                 "inputSchema": {"type": "object", "properties": {"entity_id": {"type": "string"}, "service": {"type": "string", "enum": ["turn_on", "turn_off"]}}, "required": ["entity_id", "service"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
                {"name": ACTION_PROPOSE, "description": "Stage on/off for light, switch, input_boolean or automation ONLY. Requires explicit chat approval BEFORE staging and Home Assistant approval button within 120 seconds. No locks, covers, climate, alarm or scripts.",
                 "inputSchema": {"type": "object", "properties": {"entity_id": {"type": "string"}, "service": {"type": "string", "enum": ["turn_on", "turn_off"]}}, "required": ["entity_id", "service"], "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": True}},
                {"name": STATUS, "description": "Check the result of a staged SmartHomeKI change. Read only.", "inputSchema": {"type": "object", "properties": {"proposal_id": {"type": "string"}}, "required": ["proposal_id"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
            ] + ext.TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name in {t["name"] for t in ext.TOOLS}:
                try:
                    result = content(await ext.dispatch(hass, state, name, args, stage))
                except Exception as exc:
                    _LOGGER.warning("SmartHomeKI extension failed: %s", type(exc).__name__)
                    result = content("Extension failed: " + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), True)
            elif name == READ:
                try:
                    _, config = await get_config(hass)
                    result = content({"dashboard": DASHBOARD, "config": config})
                except ValueError as exc:
                    result = content(str(exc), True)
            elif name == ENTITIES:
                try:
                    result = content(list_safe_entities(hass, args))
                except ValueError as exc:
                    result = content(str(exc), True)
            elif name == DIAGNOSTICS:
                try:
                    result = content(get_diagnostics(hass, args))
                except ValueError as exc:
                    result = content(str(exc), True)
            elif name in (CARD_CHANGE_PREVIEW, CARD_CHANGE_PROPOSE):
                err = validate_card_change(args)
                if err:
                    result = content(err, True)
                else:
                    try:
                        _, config = await get_config(hass)
                        find_exact_card(config, args["view_path"], args["old_card"])
                        if name == CARD_CHANGE_PREVIEW:
                            result = content({"valid": True, "saved": False, "view_path": args["view_path"], "operation": args["operation"]})
                        else:
                            async with state["lock"]:
                                pid = stage(state, hass, {"kind": "change_card", "view_path": args["view_path"],
                                    "operation": args["operation"], "old_card": copy.deepcopy(args["old_card"]),
                                    "new_card": copy.deepcopy(args["new_card"]), "base_hash": digest(config)})
                            await hass.services.async_call("persistent_notification", "create", {
                                "title": "SmartHomeKI – Kartenänderung freigeben",
                                "message": f"{args['operation']} auf {args['view_path']}: Vorschlag {pid}. Nur bei Zustimmung innerhalb von 120 Sekunden {BUTTON} drücken."}, blocking=False)
                            result = content({"proposal_id": pid, "status": "pending", "saved": False,
                                "expires_in_seconds": TTL, "approval_button": BUTTON})
                    except (ValueError, TypeError) as exc:
                        result = content(str(exc), True)
            elif name in (ACTION_PREVIEW, ACTION_PROPOSE):
                err = validate_action(hass, args)
                if err:
                    result = content(err, True)
                elif name == ACTION_PREVIEW:
                    result = content({"valid": True, "executed": False, **args})
                else:
                    try:
                        async with state["lock"]:
                            pid = stage(state, hass, {"kind": "safe_action", **args})
                        await hass.services.async_call("persistent_notification", "create", {
                            "title": "SmartHomeKI – Geräteaktion freigeben",
                            "message": f"{args['entity_id']}: {args['service']} (Vorschlag {pid}). Nur bei Zustimmung innerhalb von 120 Sekunden {BUTTON} drücken."}, blocking=False)
                        result = content({"proposal_id": pid, "status": "pending", "saved": False,
                            "expires_in_seconds": TTL, "approval_button": BUTTON, **args})
                    except ValueError as exc:
                        result = content(str(exc), True)
            elif name in (PREVIEW, PROPOSE):
                err = validate_args(args)
                if err:
                    result = content(err, True)
                else:
                    try:
                        _, config = await get_config(hass)
                        view = find_view(config, args["view_path"])
                        if view is None:
                            raise ValueError("View not found")
                        if not isinstance(view.get("cards"), list) and not any(isinstance(s, dict) and isinstance(s.get("cards"), list) for s in view.get("sections", []) if isinstance(view.get("sections"), list)):
                            raise ValueError("View has no supported card container")
                        if name == PREVIEW:
                            result = content({"valid": True, "saved": False, "dashboard": DASHBOARD, "view_path": args["view_path"], "card": args["card"]})
                        else:
                            async with state["lock"]:
                                if state["pending"] is not None and time.monotonic() <= state["pending"]["expires"]:
                                    raise ValueError("Another proposal is awaiting approval")
                                if hass.states.get(BUTTON) is None:
                                    raise ValueError("Approval button helper not found")
                                pid = secrets.token_hex(8)
                                state["pending"] = {"id": pid, "view_path": args["view_path"], "card": copy.deepcopy(args["card"]), "base_hash": digest(config), "expires": time.monotonic() + TTL}
                                state["results"][pid] = {"status": "pending", "saved": False}
                            await hass.services.async_call("persistent_notification", "create", {"title": "SmartHomeKI – Freigabe erforderlich", "message": f"Vorschlag {pid}: Karte '{args['card']['type']}' auf Seite '{args['view_path']}'. Nur wenn du diese Änderung möchtest, drücke innerhalb von 2 Minuten den Helfer-Taster '{BUTTON}'."}, blocking=False)
                            result = content({"proposal_id": pid, "status": "pending", "saved": False, "expires_in_seconds": TTL, "approval_button": BUTTON, "view_path": args["view_path"], "card": args["card"]})
                    except (ValueError, TypeError) as exc:
                        result = content(str(exc), True)
            elif name in (EDIT_PREVIEW, EDIT_PROPOSE):
                err = validate_edit_args(args)
                if err:
                    result = content(err, True)
                else:
                    try:
                        _, config = await get_config(hass)
                        find_exact_card(config, args["view_path"], args["old_card"])
                        if name == EDIT_PREVIEW:
                            result = content({"valid": True, "saved": False, "view_path": args["view_path"],
                                              "old_card": args["old_card"], "new_card": args["new_card"]})
                        else:
                            async with state["lock"]:
                                if state["pending"] is not None and time.monotonic() <= state["pending"]["expires"]:
                                    raise ValueError("Another proposal is awaiting approval")
                                if hass.states.get(BUTTON) is None:
                                    raise ValueError("Approval button helper not found")
                                pid = secrets.token_hex(8)
                                state["pending"] = {"id": pid, "kind": "edit_calendar_card", "view_path": args["view_path"],
                                                    "old_card": copy.deepcopy(args["old_card"]),
                                                    "new_card": copy.deepcopy(args["new_card"]),
                                                    "base_hash": digest(config), "expires": time.monotonic() + TTL}
                                state["results"][pid] = {"status": "pending", "saved": False}
                            await hass.services.async_call("persistent_notification", "create", {
                                "title": "SmartHomeKI – Kalenderkarte bearbeiten",
                                "message": f"Vorschlag {pid}: Kalenderkarte auf '{args['view_path']}' ersetzen. Nur wenn gewünscht innerhalb von 2 Minuten '{BUTTON}' drücken."
                            }, blocking=False)
                            result = content({"proposal_id": pid, "status": "pending", "saved": False,
                                              "expires_in_seconds": TTL, "approval_button": BUTTON})
                    except (ValueError, TypeError) as exc:
                        result = content(str(exc), True)
            elif name in (VIEW_PREVIEW, VIEW_PROPOSE):
                err = validate_view_args(args)
                if err:
                    result = content(err, True)
                else:
                    try:
                        _, config = await get_config(hass)
                        old_view = find_view(config, args["view_path"])
                        if old_view is None:
                            raise ValueError("Requested SmartHomeKI view not found")
                        new_view = build_replacement(old_view, args["view"])
                        summary = {"view_path": args["view_path"], "old_layout": old_view.get("type"),
                                   "new_layout": new_view["type"], "old_top_level_cards": len(old_view.get("cards", [])),
                                   "new_sections": len(new_view.get("sections", [])),
                                   "new_cards": len(new_view.get("cards", [])) + sum(len(sec["cards"]) for sec in new_view.get("sections", []))}
                        if name == VIEW_PREVIEW:
                            result = content({"valid": True, "saved": False, "summary": summary})
                        else:
                            async with state["lock"]:
                                if state["pending"] is not None and time.monotonic() <= state["pending"]["expires"]:
                                    raise ValueError("Another proposal is awaiting approval")
                                if hass.states.get(BUTTON) is None:
                                    raise ValueError("Approval button helper not found")
                                pid = secrets.token_hex(8)
                                state["pending"] = {"id": pid, "kind": "replace_view", "view_path": args["view_path"],
                                                    "view": copy.deepcopy(args["view"]), "base_hash": digest(config),
                                                    "expires": time.monotonic() + TTL}
                                state["results"][pid] = {"status": "pending", "saved": False}
                            await hass.services.async_call("persistent_notification", "create", {
                                "title": "SmartHomeKI – Ansicht ersetzen",
                                "message": f"Vorschlag {pid}: {summary}. Bestehende Karten werden ersetzt. Nur wenn gewünscht innerhalb von 2 Minuten '{BUTTON}' drücken."
                            }, blocking=False)
                            result = content({"proposal_id": pid, "status": "pending", "saved": False,
                                              "expires_in_seconds": TTL, "approval_button": BUTTON, "summary": summary})
                    except (ValueError, TypeError) as exc:
                        result = content(str(exc), True)
            elif name == SIDEBAR:
                try:
                    if args != {}:
                        raise ValueError("This operation accepts no arguments")
                    _, config = await get_config(hass)
                    kiosk = config.get("kiosk_mode")
                    if not isinstance(kiosk, dict) or kiosk.get("hide_sidebar") is not True:
                        raise ValueError("Sidebar is not currently configured as hidden")
                    async with state["lock"]:
                        if state["pending"] is not None and time.monotonic() <= state["pending"]["expires"]:
                            raise ValueError("Another proposal is awaiting approval")
                        if hass.states.get(BUTTON) is None:
                            raise ValueError("Approval button helper not found")
                        pid = secrets.token_hex(8)
                        state["pending"] = {"id": pid, "kind": "sidebar_visible", "base_hash": digest(config), "expires": time.monotonic() + TTL}
                        state["results"][pid] = {"status": "pending", "saved": False}
                    await hass.services.async_call("persistent_notification", "create", {
                        "title": "SmartHomeKI – Seitenleiste freigeben",
                        "message": f"Vorschlag {pid}: kiosk_mode.hide_sidebar wird auf false gesetzt (auch auf Tablets sichtbar). Nur wenn gewünscht, drücke innerhalb von 2 Minuten '{BUTTON}'."
                    }, blocking=False)
                    result = content({"proposal_id": pid, "status": "pending", "saved": False, "expires_in_seconds": TTL, "approval_button": BUTTON, "change": "kiosk_mode.hide_sidebar = false"})
                except ValueError as exc:
                    result = content(str(exc), True)
            elif name == STATUS:
                pid = args.get("proposal_id") if isinstance(args, dict) else None
                if not isinstance(pid, str):
                    result = content("proposal_id required", True)
                else:
                    r = state["results"].get(pid)
                    if r is None:
                        result = content("Unknown proposal", True)
                    elif r["status"] == "pending" and (state["pending"] is None or state["pending"]["id"] != pid or time.monotonic() > state["pending"]["expires"]):
                        result = content({"status": "expired", "saved": False})
                    else:
                        result = content(r)
            else:
                return self.error(rid, -32602, "Unknown tool")
        else:
            return self.error(rid, -32601, "Method not found")
        return self.json({"jsonrpc": "2.0", "id": rid, "result": result})

    def error(self, rid, code, message):
        return self.json({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})
