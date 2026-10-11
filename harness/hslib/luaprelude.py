"""The Lua unit runner's XML prelude (openspec/changes/test-pyramid design 5, "XML frames").

Turns our glue XML (client-mods/glue/HSFrontMenu.xml) into a Lua chunk that, given the stub runtime, creates the
named frames with their parents, ids, initial visibility and script handlers, so the real .lua files load unchanged.
The chunk returns {scripts = {<Script file=...> in document order}, build = function(S) ... end}; the runner loads
the scripts first (as the client does: they come before the frames in the file), then calls build, which fires each
frame's OnLoad as it's created.

Only what our files use is understood: named frame-like elements, `parent`, `id`, `hidden`, `inherits` of our own
virtual templates (their scripts are copied), and <Scripts> bodies. Inherited stock templates (GlueButtonTemplate...)
are recorded by name only.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from . import paths as P

GLUE_XML = (P.ROOT / "client-mods" / "glue" / "HSFrontMenu.xml",)
OUT = P.HARNESS / "out" / "unit" / "lua-prelude.lua"

FRAME_TAGS = {"Frame", "Button", "CheckButton", "EditBox", "Model", "StatusBar", "ScrollFrame", "Slider",
              "MessageFrame", "SimpleHTML", "FontString", "Texture", "HighlightTexture", "PushedTexture",
              "NormalTexture", "DisabledTexture"}
# Script handler parameters as the 3.3.5a client passes them (the handler bodies name them)
HANDLER_ARGS = {"OnLoad": "self", "OnShow": "self", "OnHide": "self", "OnUpdate": "self, elapsed",
                "OnEvent": "self, event, ...", "OnClick": "self, button, down", "OnDoubleClick": "self, button",
                "OnEnter": "self, motion", "OnLeave": "self, motion", "OnKeyDown": "self, key", "OnKeyUp": "self, key",
                "OnMouseDown": "self, button", "OnMouseUp": "self, button", "OnValueChanged": "self, value"}


def _tag(e) -> str:
    return e.tag.split("}", 1)[-1]


def _lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _scripts(e) -> dict:
    out = {}
    for s in (c for c in e if _tag(c) == "Scripts"):
        for h in s:
            out[_tag(h)] = (h.text or "").strip()
    return out


def prelude_for(xml_path: Path) -> str:
    root = ET.parse(xml_path).getroot()
    scripts = [c.get("file") for c in root if _tag(c) == "Script" and c.get("file")]
    templates: dict = {}
    lines = ["return { scripts = {" + ", ".join(_lua_str(s) for s in scripts) + "}, build = function(S)"]

    def walk(e, parent_name: str | None):
        t = _tag(e)
        name = e.get("name")
        if t in FRAME_TAGS:
            if name and parent_name and "$parent" in name:
                name = name.replace("$parent", parent_name)
            if e.get("virtual") == "true":
                templates[name] = _scripts(e)
                return
            parent = e.get("parent") or parent_name
            scr = {}
            inh = [x.strip() for x in (e.get("inherits") or "").split(",") if x.strip()]
            for i in inh:
                scr |= templates.get(i, {})
            scr |= _scripts(e)
            fields = [f"type={_lua_str(t)}"]
            if name:
                fields.append(f"name={_lua_str(name)}")
            if parent:
                fields.append(f"parent={_lua_str(parent)}")
            if e.get("id"):
                fields.append(f"id={int(e.get('id'))}")
            if e.get("hidden") == "true":
                fields.append("hidden=true")
            if e.get("text") is not None:
                fields.append(f"text={_lua_str(e.get('text'))}")
            if inh:
                fields.append("inherits={" + ", ".join(_lua_str(i) for i in inh) + "}")
            hs = []
            for h, body in scr.items():
                args = HANDLER_ARGS.get(h)
                if args is None:
                    raise ValueError(f"{xml_path.name}: handler {h} on {name} has no known arguments")
                hs.append(f"{h}=function({args})\n{body}\nend")
            if hs:
                fields.append("scripts={" + ",\n".join(hs) + "}")
            lines.append("S.xmlframe{" + ", ".join(fields) + "}")
            for c in e:
                walk(c, name or parent_name)
            return
        for c in e:
            walk(c, parent_name)

    for c in root:
        walk(c, None)
    lines.append("end }")
    return "\n".join(lines) + "\n"


def write_prelude(xml_path: Path = GLUE_XML[0], out: Path = OUT) -> Path:
    src = prelude_for(xml_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.is_file() or out.read_text() != src:
        out.write_text(src)
    return out
