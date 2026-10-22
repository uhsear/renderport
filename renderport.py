#!/usr/bin/env python
"""Convert an ArcGIS layer renderer to MapLibre style layers, and refuse every approximation.

A converter that meets a symbol it cannot carry has two choices: draw
something close and say nothing, or stop. This tool stops. Each place where
the Esri reference and the MapLibre style specification disagree, or where the
Esri reference does not say what a value means, is a named rule. A conversion
that hits a rule is REFUSED, the rule id and the JSON path are printed, and no
style is produced. Five rules can be accepted one at a time with --accept. An
accepted approximation is printed and recorded in each layer's metadata.

Input is the JSON of one layer: the answer of <layer url>?f=json, or a saved
copy of it. Output is a JSON array of MapLibre layer objects. The tool does not
build tiles and does not read data. The attribute names and types in the
layer's "fields" list are what the emitted filters assume the tiles carry.

    python renderport.py --self-test
    python renderport.py --layer-json layer.json
    python renderport.py --layer-json layer.json --out layers.json --apply
    python renderport.py --url https://host/arcgis/rest/services/S/FeatureServer/0
    python renderport.py --layer-json layer.json --accept R-LINE-CAP-JOIN,R-SCALE-RANGE

Exit codes: 0 converted, 1 refused, 2 input or output failure, 64 usage error.
Only --apply writes a file, and it never overwrites one.
"""

import argparse
import contextlib
import hashlib
import http.server
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Constants with a source. Change them here, not at a call site.
# =============================================================================

# CSS Values and Units 3, section 5.2: 1pt = 1/72 in and 1px = 1/96 in. Esri
# sizes and widths are points, MapLibre sizes are pixels.
PT_TO_PX = 96.0 / 72.0

# Scale denominator of web mercator level 0 in the 256 pixel, 96 dpi tile
# scheme (591657527.591555) halved, because a MapLibre zoom level is one level
# lower than the matching 256 pixel level.
ZOOM0_SCALE = 591657527.591555 / 2.0

MAX_ZOOM = 24.0  # MapLibre minzoom and maxzoom range 0 to 24
FETCH_LIMIT = 10 * 1024 * 1024  # bytes read from one layer answer
FETCH_TIMEOUT = 30  # seconds

# rule id -> (can be accepted with --accept, what is refused and why)
RULES = {
    "R-INPUT": (False, "the input is not a layer definition this tool can read"),
    "R-GEOMETRY": (False, "the layer geometry type is not point, multipoint, polyline or polygon"),
    "R-RENDERER": (False, "the renderer is missing or is not simple, uniqueValue or classBreaks"),
    "R-SYMBOL-TYPE": (False, "the symbol type is not the simple marker, line or fill that matches the layer geometry"),
    "R-PICTURE": (False, "picture symbols need a sprite image this tool does not build"),
    "R-MARKER-SHAPE": (False, "a MapLibre circle layer draws only circles"),
    "R-MARKER-SIZE": (True, "the Esri reference does not say whether a marker size is a diameter or includes the outline, and MapLibre draws a circle stroke outside circle-radius"),
    "R-OFFSET": (False, "the Esri reference does not say which direction a positive marker offset moves"),
    "R-LINE-DASH": (False, "the Esri reference does not define the dash lengths of its named line styles"),
    "R-LINE-CAP-JOIN": (True, "the REST line symbol has no cap or join, the ArcGIS JavaScript symbol defaults both to round, and MapLibre defaults to butt and miter"),
    "R-FILL-HATCH": (False, "hatch fills need a sprite pattern this tool does not build"),
    "R-COLOR": (False, "the color is null or is not [r, g, b, a] with each value 0 to 255"),
    "R-VALUE": (False, "a value is missing, malformed or ambiguous"),
    "R-UNREAD-KEY": (False, "the object has a property this tool does not read"),
    "R-VISUAL-VARIABLE": (False, "visual variables and rotation change the symbol per feature and are not carried"),
    "R-UV-ARCADE": (False, "an Arcade value expression cannot be carried"),
    "R-UV-GROUPS": (False, "uniqueValueGroups are not read"),
    "R-UV-MULTIFIELD": (False, "matching on more than one field is not carried"),
    "R-FIELD-TYPE": (False, "the renderer field is not in the layer fields, or its type cannot be matched safely"),
    "R-CB-NORMALIZE": (False, "normalized class breaks are not carried"),
    "R-CB-BOUNDARY": (True, "the Esri reference does not say whether a class bound is inclusive, so a value on a bound may take the wrong class"),
    "R-CB-ORDER": (False, "class bounds must rise strictly from minValue"),
    "R-LABELS": (False, "label placement and label expressions are not carried"),
    "R-SCALE-SYMBOLS": (False, "symbols that scale with the map are not carried"),
    "R-SCALE-RANGE": (True, "the Esri reference does not define the direction of minScale and maxScale or the meaning of 0, and a scale-to-zoom conversion assumes 96 dpi"),
    "R-LAYER-TRANSPARENCY": (True, "MapLibre alpha is applied per feature and accumulates where features overlap, and the Esri reference does not say how layer transparency composites"),
}

_GEOMETRY = {
    "esriGeometryPoint": "point",
    "esriGeometryMultipoint": "point",
    "esriGeometryPolyline": "line",
    "esriGeometryPolygon": "polygon",
}
_SYMBOL_GEOM = {"esriSMS": "point", "esriSLS": "line", "esriSFS": "polygon"}
_PICTURE = ("esriPMS", "esriPFS")
_SMS_KEYS = ("type", "style", "color", "size", "angle", "xoffset", "yoffset", "outline")
_SLS_KEYS = ("type", "style", "color", "width")
_SFS_KEYS = ("type", "style", "color", "outline")
_HATCH = ("esriSFSBackwardDiagonal", "esriSFSCross", "esriSFSDiagonalCross",
          "esriSFSForwardDiagonal", "esriSFSHorizontal", "esriSFSVertical")
_DI_KEYS = ("renderer", "transparency", "labelingInfo", "scaleSymbols", "showLabels", "fixedSymbols")
_COMMON_KEYS = ("type", "rotationType", "rotationExpression", "visualVariables", "authoringInfo")
_SIMPLE_KEYS = _COMMON_KEYS + ("symbol", "label", "description")
_UV_KEYS = _COMMON_KEYS + ("field1", "field2", "field3", "fieldDelimiter", "defaultSymbol",
                           "defaultLabel", "uniqueValueInfos", "uniqueValueGroups", "valueExpression")
_CB_KEYS = _COMMON_KEYS + ("field", "classificationMethod", "normalizationType", "normalizationField",
                           "normalizationTotal", "defaultSymbol", "defaultLabel", "minValue",
                           "classBreakInfos")
_KINDS = {
    "esriFieldTypeString": "string",
    "esriFieldTypeInteger": "int", "esriFieldTypeSmallInteger": "int",
    "esriFieldTypeBigInteger": "int", "esriFieldTypeOID": "int",
    "esriFieldTypeDouble": "float", "esriFieldTypeSingle": "float",
}
_INT_RE = re.compile(r"^-?[0-9]+$")
_ROUND = {"line-cap": "round", "line-join": "round"}


# =============================================================================
# The decision core. Pure: no file, no network, no clock.
# =============================================================================

class _Ctx(object):
    """Collects refusals and the approximations that --accept allowed."""

    def __init__(self, accepted):
        self.accepted = frozenset(accepted)
        self.refusals = []
        self.used = set()

    def refuse(self, rule, where, extra=""):
        detail = RULES[rule][1] + (" -- " + extra if extra else "")
        self.refusals.append((rule, where, detail))

    def approx(self, rule, where, extra=""):
        """True when the rule was accepted. Otherwise record the refusal."""
        if rule in self.accepted:
            self.used.add(rule)
            return True
        self.refuse(rule, where, extra)
        return False


def _s(value):
    """The value when it is a string, else ''. Keeps a table lookup safe on any JSON."""
    return value if isinstance(value, str) else ""


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _fmt(x):
    return ("%.4f" % x).rstrip("0").rstrip(".")


def _rgba(color, factor, ctx, where):
    ok = isinstance(color, list) and len(color) == 4 and all(_num(c) and 0 <= c <= 255 for c in color)
    if not ok:
        ctx.refuse("R-COLOR", where, "got %s" % json.dumps(color))
        return None
    return "rgba(%d,%d,%d,%s)" % (round(color[0]), round(color[1]), round(color[2]),
                                  _fmt(color[3] / 255.0 * factor))


def _zoom(scale):
    return round(math.log(ZOOM0_SCALE / scale, 2), 4)


def _check_keys(sym, allowed, ctx, where):
    extra = sorted(k for k in sym if k not in allowed)
    if extra:
        ctx.refuse("R-UNREAD-KEY", where, "unread: %s" % ", ".join(extra))


def _cap_join(ctx, where):
    return ctx.approx("R-LINE-CAP-JOIN", where, "emitting round cap and round join if accepted")


def _line_spec(sym, factor, ctx, where):
    """None when refused, False when the style draws nothing, else the line paint values."""
    if not isinstance(sym, dict) or sym.get("type") != "esriSLS":
        ctx.refuse("R-VALUE", where, "need a simple line symbol (esriSLS)")
        return None
    _check_keys(sym, _SLS_KEYS, ctx, where)
    style = _s(sym.get("style"))
    if style == "esriSLSNull":
        return False
    if style != "esriSLSSolid":
        ctx.refuse("R-LINE-DASH", where, "style %s" % json.dumps(sym.get("style")))
        return None
    width = sym.get("width")
    if not _num(width) or width <= 0:
        ctx.refuse("R-VALUE", where, "a solid line needs a width above 0, got %s" % json.dumps(width))
        return None
    color = _rgba(sym.get("color"), factor, ctx, where + ".color")
    if color is None:
        return None
    return {"color": color, "width": round(width * PT_TO_PX, 4)}


def _marker(sym, factor, ctx, where):
    bad = not ctx.approx("R-MARKER-SIZE", where, "accepting reads size as the circle diameter, with any stroke outside it")
    if sym.get("style") != "esriSMSCircle":
        ctx.refuse("R-MARKER-SHAPE", where, "style %s" % json.dumps(sym.get("style")))
        bad = True
    _check_keys(sym, _SMS_KEYS, ctx, where)
    size = sym.get("size")
    if not _num(size) or size <= 0:
        ctx.refuse("R-VALUE", where, "size must be above 0, got %s" % json.dumps(size))
        bad = True
    if sym.get("xoffset", 0) != 0 or sym.get("yoffset", 0) != 0:
        ctx.refuse("R-OFFSET", where, "xoffset %s, yoffset %s" % (
            json.dumps(sym.get("xoffset")), json.dumps(sym.get("yoffset"))))
        bad = True
    color = _rgba(sym.get("color"), factor, ctx, where + ".color")
    outline = _line_spec(sym.get("outline"), factor, ctx, where + ".outline")
    if color is None or outline is None:
        bad = True
    if bad:
        return None
    paint = {"circle-radius": round(size * PT_TO_PX / 2.0, 4), "circle-color": color}
    if outline is not False:
        paint["circle-stroke-width"] = outline["width"]
        paint["circle-stroke-color"] = outline["color"]
    return [("circle", paint, {})]


def _line_symbol(sym, factor, ctx, where):
    spec = _line_spec(sym, factor, ctx, where)
    if spec is None:
        return None
    if spec is False:
        return []
    if not _cap_join(ctx, where):
        return None
    return [("line", {"line-color": spec["color"], "line-width": spec["width"]}, dict(_ROUND))]


def _fill(sym, factor, ctx, where):
    _check_keys(sym, _SFS_KEYS, ctx, where)
    style = _s(sym.get("style"))
    parts = []
    bad = False
    if style == "esriSFSSolid":
        color = _rgba(sym.get("color"), factor, ctx, where + ".color")
        if color is None:
            bad = True
        else:
            parts.append(("fill", {"fill-color": color}, {}))
    elif style in _HATCH:
        ctx.refuse("R-FILL-HATCH", where, "style %s" % style)
        bad = True
    elif style != "esriSFSNull":
        ctx.refuse("R-VALUE", where, "unknown fill style %s" % json.dumps(sym.get("style")))
        bad = True
    spec = _line_spec(sym.get("outline"), factor, ctx, where + ".outline")
    if spec is None:
        bad = True
    elif spec is not False:
        if _cap_join(ctx, where + ".outline"):
            parts.append(("line", {"line-color": spec["color"], "line-width": spec["width"]}, dict(_ROUND)))
        else:
            bad = True
    return None if bad else parts


_CONVERTERS = {"esriSMS": _marker, "esriSLS": _line_symbol, "esriSFS": _fill}


def _symbol_layers(sym, geom, factor, ctx, where):
    """A list of (layer type, paint, layout), or None when the symbol was refused."""
    if not isinstance(sym, dict):
        ctx.refuse("R-VALUE", where, "the symbol is missing or is not an object")
        return None
    stype = _s(sym.get("type"))
    if stype in _PICTURE:
        ctx.refuse("R-PICTURE", where, "type %s" % stype)
        return None
    if _SYMBOL_GEOM.get(stype) != geom:
        ctx.refuse("R-SYMBOL-TYPE", where, "type %s on a %s layer" % (json.dumps(sym.get("type")), geom))
        return None
    return _CONVERTERS[stype](sym, factor, ctx, where)


def _field_types(info):
    fields = info.get("fields")
    if not isinstance(fields, list):
        return {}
    return dict((f["name"], f["type"]) for f in fields
                if isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("type"), str))


def _field_kind(name, info, allowed, ctx, where):
    types = _field_types(info)
    kind = _KINDS.get(types.get(_s(name)))
    if kind is None or kind not in allowed:
        ctx.refuse("R-FIELD-TYPE", where, "field %s has type %s" % (json.dumps(name), json.dumps(types.get(_s(name)))))
        return None
    return kind


def _typed(value, kind):
    """The class value as the tile attribute type, or None when it cannot be."""
    if not isinstance(value, str):
        return None
    if kind == "string":
        return value
    if kind == "int":
        return int(value) if _INT_RE.match(value) else None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _renderer_common(r, allowed, ctx):
    _check_keys(r, allowed, ctx, "renderer")
    if r.get("visualVariables") or r.get("rotationExpression"):
        ctx.refuse("R-VISUAL-VARIABLE", "renderer", "visualVariables or rotationExpression is set")


def _simple(r, geom, info, factor, ctx):
    _renderer_common(r, _SIMPLE_KEYS, ctx)
    return [(r.get("label"), None, r.get("symbol"), "renderer.symbol")]


def _unique(r, geom, info, factor, ctx):
    _renderer_common(r, _UV_KEYS, ctx)
    stop = False
    if r.get("valueExpression"):
        ctx.refuse("R-UV-ARCADE", "renderer.valueExpression")
        stop = True
    if r.get("uniqueValueGroups"):
        ctx.refuse("R-UV-GROUPS", "renderer.uniqueValueGroups")
        stop = True
    if r.get("field2") or r.get("field3"):
        ctx.refuse("R-UV-MULTIFIELD", "renderer.field2")
        stop = True
    if stop:
        return []
    kind = _field_kind(r.get("field1"), info, ("string", "int", "float"), ctx, "renderer.field1")
    if kind is None:
        return []
    infos = r.get("uniqueValueInfos") or []
    if not isinstance(infos, list):
        ctx.refuse("R-VALUE", "renderer.uniqueValueInfos", "not a list")
        return []
    field = r["field1"]
    specs = []
    seen = []
    for i, item in enumerate(infos):
        where = "renderer.uniqueValueInfos[%d]" % i
        if not isinstance(item, dict):
            ctx.refuse("R-VALUE", where, "not an object")
            continue
        typed = _typed(item.get("value"), kind)
        if typed is None:
            ctx.refuse("R-VALUE", where, "value %s does not parse as %s" % (json.dumps(item.get("value")), kind))
            continue
        if typed in seen:
            ctx.refuse("R-VALUE", where, "value %s repeats an earlier class" % json.dumps(item.get("value")))
            continue
        seen.append(typed)
        specs.append((item.get("label"), ["==", ["get", field], typed], item.get("symbol"), where + ".symbol"))
    default = r.get("defaultSymbol")
    if default is not None:
        filt = ["!", ["match", ["get", field], list(seen), True, False]] if seen else None
        specs.append((r.get("defaultLabel"), filt, default, "renderer.defaultSymbol"))
    if not specs and not ctx.refusals:
        ctx.refuse("R-VALUE", "renderer", "no classes and no defaultSymbol, so nothing would draw")
    return specs


def _breaks(r, geom, info, factor, ctx):
    _renderer_common(r, _CB_KEYS, ctx)
    if any(r.get(k) for k in ("normalizationType", "normalizationField", "normalizationTotal")):
        ctx.refuse("R-CB-NORMALIZE", "renderer.normalizationType")
        return []
    kind = _field_kind(r.get("field"), info, ("int", "float"), ctx, "renderer.field")
    if kind is None:
        return []
    ctx.approx("R-CB-BOUNDARY", "renderer.classBreakInfos", "taking each class as (previous max, max], the first as [minValue, max]")
    low = r.get("minValue")
    infos = r.get("classBreakInfos")
    if not _num(low) or not isinstance(infos, list) or not infos:
        ctx.refuse("R-VALUE", "renderer", "need a numeric minValue and a non-empty classBreakInfos list")
        return []
    get = ["get", r["field"]]
    specs = []
    prev = low
    for i, item in enumerate(infos):
        where = "renderer.classBreakInfos[%d]" % i
        if not isinstance(item, dict) or not _num(item.get("classMaxValue")):
            ctx.refuse("R-VALUE", where, "classMaxValue must be a number")
            return []
        top = item["classMaxValue"]
        mine = item.get("classMinValue")
        if mine is not None and (not _num(mine) or mine > top):
            ctx.refuse("R-VALUE", where, "classMinValue %s is not a number at or below classMaxValue" % json.dumps(mine))
            return []
        if (top < prev) if i == 0 else (top <= prev):
            ctx.refuse("R-CB-ORDER", where, "classMaxValue %s after %s" % (json.dumps(top), json.dumps(prev)))
            return []
        lower = [">=", get, low] if i == 0 else [">", get, prev]
        specs.append((item.get("label"), ["all", ["!=", get, None], lower, ["<=", get, top]],
                      item.get("symbol"), where + ".symbol"))
        prev = top
    default = r.get("defaultSymbol")
    if default is not None:
        specs.append((r.get("defaultLabel"),
                      ["any", ["==", get, None], ["<", get, low], [">", get, prev]],
                      default, "renderer.defaultSymbol"))
    return specs


_BUILDERS = {"simple": _simple, "uniqueValue": _unique, "classBreaks": _breaks}


def _layer_checks(info, di, ctx):
    """Layer-wide settings. Returns (alpha factor, minzoom, maxzoom)."""
    factor = 1.0
    t = di.get("transparency", 0)
    if not _num(t) or not 0 <= t <= 100:
        ctx.refuse("R-VALUE", "drawingInfo.transparency", "got %s" % json.dumps(t))
    elif t > 0 and ctx.approx("R-LAYER-TRANSPARENCY", "drawingInfo.transparency", "multiplying every alpha by %s if accepted" % _fmt(1 - t / 100.0)):
        factor = 1 - t / 100.0
    if di.get("labelingInfo"):
        ctx.refuse("R-LABELS", "drawingInfo.labelingInfo")
    if di.get("scaleSymbols") is True:
        ctx.refuse("R-SCALE-SYMBOLS", "drawingInfo.scaleSymbols")
    extra = sorted(k for k in di if k not in _DI_KEYS)
    if extra:
        ctx.refuse("R-UNREAD-KEY", "drawingInfo", "unread: %s" % ", ".join(extra))
    lo = info.get("minScale", 0)
    hi = info.get("maxScale", 0)
    minzoom = maxzoom = None
    if not (_num(lo) and _num(hi) and lo >= 0 and hi >= 0 and (lo == 0 or hi == 0 or lo >= hi)):
        ctx.refuse("R-VALUE", "minScale", "minScale %s, maxScale %s" % (json.dumps(lo), json.dumps(hi)))
    elif (lo or hi) and ctx.approx("R-SCALE-RANGE", "minScale", "minScale %s, maxScale %s; reading minScale as the zoomed-out limit and 0 as no limit" % (json.dumps(lo), json.dumps(hi))):
        if lo:
            minzoom = max(0.0, _zoom(lo))
        if hi:
            maxzoom = min(MAX_ZOOM, _zoom(hi))
    return factor, minzoom, maxzoom


def _plan(info, ctx):
    """-> (classes, minzoom, maxzoom). Each class is (label, filter, parts)."""
    if not isinstance(info, dict):
        ctx.refuse("R-INPUT", "input", "not a JSON object")
        return [], None, None
    if "error" in info:
        ctx.refuse("R-INPUT", "input", "the service answered with an error body: %s" % json.dumps(info["error"], sort_keys=True)[:200])
        return [], None, None
    geom = _GEOMETRY.get(_s(info.get("geometryType")))
    if geom is None:
        ctx.refuse("R-GEOMETRY", "geometryType", "got %s" % json.dumps(info.get("geometryType")))
        return [], None, None
    di = info.get("drawingInfo")
    if not isinstance(di, dict):
        ctx.refuse("R-INPUT", "drawingInfo", "missing or not an object")
        return [], None, None
    factor, minzoom, maxzoom = _layer_checks(info, di, ctx)
    r = di.get("renderer")
    builder = _BUILDERS.get(_s(r.get("type"))) if isinstance(r, dict) else None
    if builder is None:
        ctx.refuse("R-RENDERER", "drawingInfo.renderer", "type %s" % json.dumps(r.get("type") if isinstance(r, dict) else None))
        return [], minzoom, maxzoom
    classes = []
    for label, filt, sym, where in builder(r, geom, info, factor, ctx):
        classes.append((label, filt, _symbol_layers(sym, geom, factor, ctx, where)))
    return classes, minzoom, maxzoom


Result = type("Result", (object,), {})


def convert(info, source="features", source_layer="features", prefix="layer", accepted=()):
    """Convert one layer definition. The result has layers, refusals and accepted."""
    ctx = _Ctx(accepted)
    classes, minzoom, maxzoom = _plan(info, ctx)
    result = Result()
    result.refusals = list(ctx.refusals)
    result.accepted = sorted(ctx.used)
    result.layers = []
    if result.refusals:
        return result
    for idx, (label, filt, parts) in enumerate(classes):
        for kind, paint, layout in parts:
            layer = {"id": "%s-%d-%s" % (prefix, idx, kind), "type": kind, "source": source,
                     "source-layer": source_layer, "paint": paint}
            if layout:
                layer["layout"] = layout
            if filt is not None:
                layer["filter"] = filt
            if minzoom is not None:
                layer["minzoom"] = minzoom
            if maxzoom is not None:
                layer["maxzoom"] = maxzoom
            layer["metadata"] = {"renderport": {"label": label, "accepted": list(result.accepted)}}
            result.layers.append(layer)
    return result


def describe(result):
    """The report lines for a Result."""
    lines = []
    for rule, where, detail in result.refusals:
        lines.append("REFUSED  %s  %s" % (rule, where))
        lines.append("    %s" % detail)
        if RULES[rule][0]:
            lines.append("    to accept: --accept %s" % rule)
    for rule in result.accepted:
        lines.append("ACCEPTED %s: %s" % (rule, RULES[rule][1]))
    if result.refusals:
        lines.append("%d refusal(s). No style was produced." % len(result.refusals))
    else:
        lines.append("%d layer(s) converted." % len(result.layers))
    return lines


# =============================================================================
# I/O. Reads a file or one URL. Writes one new file, only under --apply.
# =============================================================================

def _fetch(url, limit=FETCH_LIMIT):
    """GET <url>?f=json and parse it. Raises ValueError or OSError."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are read, got %r" % parts.scheme)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "f"] + [("f", "json")]
    target = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    with contextlib.closing(urllib.request.urlopen(target, timeout=FETCH_TIMEOUT)) as resp:
        body = resp.read(limit + 1)
    if len(body) > limit:
        raise ValueError("the answer is larger than %d bytes" % limit)
    return json.loads(body.decode("utf-8"))


def _load(args):
    if args.url:
        return _fetch(args.url)
    with io.open(args.layer_json, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_new(path, layers):
    """Create path and write the layers. Never overwrites: mode x fails if it exists."""
    with io.open(path, "x", encoding="ascii") as handle:
        handle.write(json.dumps(layers, indent=2, sort_keys=True) + "\n")


# =============================================================================
# Self-test support: a small model of the MapLibre expressions this tool emits.
# It is this tool's reading of the style specification, not MapLibre itself.
# =============================================================================

def _tkind(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    return "string" if isinstance(v, str) else "other"


def _eval(expr, props):
    """Evaluate an emitted filter against one feature's properties."""
    if not isinstance(expr, list):
        return expr
    op, args = expr[0], expr[1:]
    if op == "get":
        return props.get(args[0])
    if op == "all":
        return all(_eval(a, props) for a in args)  # short-circuits, as the specification says
    if op == "any":
        return any(_eval(a, props) for a in args)
    if op == "!":
        return not _eval(args[0], props)
    if op == "match":
        value = _eval(args[0], props)
        pairs = args[1:-1]
        for i in range(0, len(pairs), 2):
            for label in pairs[i]:
                if _tkind(value) == _tkind(label) and value == label:
                    return pairs[i + 1]
        return args[-1]
    a, b = _eval(args[0], props), _eval(args[1], props)
    if op in ("==", "!="):
        same = _tkind(a) == _tkind(b) and a == b  # strictly typed
        return same if op == "==" else not same
    if _tkind(a) != "number" or _tkind(b) != "number":
        raise ValueError("expression error: %r needs two numbers" % op)
    return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]


def _drawn(layers, props):
    """The layer ids a feature with these properties draws in."""
    return [l["id"] for l in layers if "filter" not in l or _eval(l["filter"], props) is True]


# ----- fixtures ---------------------------------------------------------------

_FIELDS = [
    {"name": "KIND", "type": "esriFieldTypeString"},
    {"name": "CODE", "type": "esriFieldTypeInteger"},
    {"name": "AREA", "type": "esriFieldTypeDouble"},
    {"name": "MADE", "type": "esriFieldTypeDate"},
]


def _sls(style="esriSLSNull", width=1.5, color=None):
    return {"type": "esriSLS", "style": style, "color": color or [0, 0, 0, 255], "width": width}


def _sms(**kw):
    sym = {"type": "esriSMS", "style": "esriSMSCircle", "color": [255, 0, 0, 255], "size": 9.0,
           "angle": 0, "xoffset": 0, "yoffset": 0, "outline": _sls()}
    sym.update(kw)
    return sym


def _sfs(**kw):
    sym = {"type": "esriSFS", "style": "esriSFSSolid", "color": [10, 120, 200, 128], "outline": _sls()}
    sym.update(kw)
    return sym


def _lyr(geom, renderer, **kw):
    info = {"geometryType": geom, "fields": [dict(f) for f in _FIELDS],
            "drawingInfo": {"renderer": renderer, "transparency": 0, "labelingInfo": None},
            "minScale": 0, "maxScale": 0}
    info.update(kw)
    return info


def _simple_pt(sym=None):
    return _lyr("esriGeometryPoint", {"type": "simple", "label": "all", "symbol": sym or _sms()})


def _simple_fill():
    return _lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs()})


def _uv(infos, field="KIND", default=None, **kw):
    r = {"type": "uniqueValue", "field1": field, "uniqueValueInfos": infos}
    if default is not None:
        r["defaultSymbol"] = default
    r.update(kw)
    return r


def _cb(infos, field="AREA", **kw):
    r = {"type": "classBreaks", "field": field, "minValue": 0, "classBreakInfos": infos}
    r.update(kw)
    return r


class _Harness(object):
    """The check()/raises() recorder, a class so the self-test can test it."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label):
        try:
            fn()
        except ValueError:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def footer(self):
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


PIN = "  <-- pinned defect"


class _Stub(http.server.BaseHTTPRequestHandler):
    """A local layer service. It records each request path."""
    seen = []
    routes = {}

    def do_GET(self):
        self.seen.append(self.path)
        status, body = self.routes.get(self.path.split("?")[0], (404, b"not found"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _run(argv):
    """Run main() and capture (status, stdout, stderr). A parser exit becomes a status."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = main(argv)
        except SystemExit as exc:
            status = exc.code
    return status, out.getvalue(), err.getvalue()


def self_test():
    """Assertions over the decision core, the parser and the file and network paths."""
    h = _Harness()
    check, raises = h.check, h.raises
    ms = ["R-MARKER-SIZE"]

    def conv(info, accepted=(), **kw):
        """convert() with the marker size rule accepted, so marker fixtures reach the rule under test."""
        return convert(info, accepted=list(accepted) + ms, **kw)

    def rules(info, accept=()):
        return sorted(set(r[0] for r in conv(info, accepted=accept).refusals))

    # ---- the harness itself
    probe = _Harness()
    with contextlib.redirect_stdout(io.StringIO()) as sink:
        probe.check(True, "a")
        probe.check(False, "b")
        probe.raises(lambda: 1, "c")
        probe.raises(lambda: (_ for _ in ()).throw(KeyError("x")), "d")
        probe.raises(lambda: (_ for _ in ()).throw(ValueError("x")), "e")
        status = probe.footer()
    shown = sink.getvalue()
    check(status == 1 and "2 assertions" not in shown and "5 assertions, 3 failed" in shown and "FAILED: b" in shown
          and "FAIL  c (no error raised)" in shown and "wrong exception" in shown and "PASS  e" in shown,
          "the harness counts passes and failures, names each failure and exits 1")

    # ---- helpers
    check(_fmt(1) == "1" and _fmt(0) == "0" and _fmt(0.5019607) == "0.502", "_fmt trims zeros and keeps 4 places at most")
    check(_num(1) and _num(1.5) and not _num(True) and not _num("1") and not _num(float("nan"))
          and not _num(float("inf")), "_num rejects bool, str, nan and inf" + PIN)
    c = _Ctx(())
    check(_rgba([10, 120, 200, 128], 1.0, c, "w") == "rgba(10,120,200,0.502)", "alpha 128 of 255 becomes 0.502")
    check(_rgba([10, 120, 200, 255], 1.0, c, "w") == "rgba(10,120,200,1)", "alpha 255 becomes 1")
    check(_rgba([10, 120, 200, 0], 1.0, c, "w") == "rgba(10,120,200,0)", "alpha 0 becomes 0")
    check(_rgba([0, 0, 0, 255], 0.5, c, "w") == "rgba(0,0,0,0.5)", "the layer factor scales alpha")
    check(not c.refusals, "valid colors record no refusal")
    for bad in (None, [1, 2, 3], [1, 2, 3, 4, 5], [256, 0, 0, 255], [-1, 0, 0, 255],
                [True, 0, 0, 255], [float("nan"), 0, 0, 255], "red"):
        c = _Ctx(())
        check(_rgba(bad, 1.0, c, "w") is None and c.refusals[0][0] == "R-COLOR",
              "color %s is refused as R-COLOR" % json.dumps(bad))
    check(_typed("A", "string") == "A" and _typed("", "string") == "", "a string class value is kept, even when empty" + PIN)
    check(_typed("7", "int") == 7 and _typed("-7", "int") == -7 and _typed("7.0", "int") is None
          and _typed("x", "int") is None, "an integer class value must be written as an integer")
    check(_typed("1.5", "float") == 1.5 and _typed("x", "float") is None and _typed("nan", "float") is None
          and _typed("inf", "float") is None and _typed(7, "float") is None, "a float class value must be finite text")
    check(abs(_zoom(295828763.795777)) < 1e-4 and abs(_zoom(295828763.795777 / 4) - 2.0) < 1e-4
          and abs(_zoom(1128.497176) - 18.0) < 1e-4,
          "scale 295828763.8 (Esri level 1) is MapLibre zoom 0, and Esri level 19 (1128.5) is zoom 18" + PIN)
    check(_s(["x"]) == "" and _s("x") == "x", "_s keeps strings and blanks everything else")

    # ---- exact conversions, no approximation involved
    res = conv(_simple_pt(), prefix="p")
    check(not res.refusals and len(res.layers) == 1, "a plain circle with no outline converts")
    lay = res.layers[0]
    check(lay["id"] == "p-0-circle" and lay["type"] == "circle" and lay["source"] == "features"
          and lay["source-layer"] == "features", "the layer id, type, source and source-layer are set")
    check(lay["paint"] == {"circle-radius": 6.0, "circle-color": "rgba(255,0,0,1)"},
          "a 9 point marker is a 6 pixel radius (9 pt = 12 px diameter)")
    check("filter" not in lay and "layout" not in lay and "minzoom" not in lay and "maxzoom" not in lay,
          "a simple renderer sets no filter, layout or zoom range")
    check(lay["metadata"] == {"renderport": {"label": "all", "accepted": ["R-MARKER-SIZE"]}}, "the class label and the accepted rules ride in metadata")
    check(conv(_simple_pt(_sms(size=0.75))).layers[0]["paint"]["circle-radius"] == 0.5, "0.75 pt is a 0.5 pixel radius")
    check(conv(_simple_pt(_sms(angle=45))).layers[0]["paint"]["circle-radius"] == 6.0,
          "a circle's angle changes nothing, so it converts")
    check(conv(_simple_pt(), source="s", source_layer="sl").layers[0]["source-layer"] == "sl",
          "--source and --source-layer reach the layer")
    mp = _lyr("esriGeometryMultipoint", {"type": "simple", "symbol": _sms()})
    check(len(conv(mp).layers) == 1, "a multipoint layer draws as circles")
    fill = conv(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs()})).layers
    check(len(fill) == 1 and fill[0]["type"] == "fill" and fill[0]["paint"] == {"fill-color": "rgba(10,120,200,0.502)"},
          "a solid fill with a null outline is one fill layer")
    nofill = conv(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs(style="esriSFSNull")}))
    check(not nofill.refusals and nofill.layers == [], "a null fill with a null outline draws nothing, as Esri does")
    nulline = conv(_lyr("esriGeometryPolyline", {"type": "simple", "symbol": _sls()}))
    check(not nulline.refusals and nulline.layers == [], "a null line draws nothing")
    check(describe(nulline) == ["0 layer(s) converted."], "an empty conversion reports 0 layers")

    # ---- the five accepted approximations
    line = {"type": "simple", "symbol": _sls("esriSLSSolid", 3.0, [1, 2, 3, 255])}
    lyr_line = _lyr("esriGeometryPolyline", line)
    check(rules(lyr_line) == ["R-LINE-CAP-JOIN"], "a solid line is refused until R-LINE-CAP-JOIN is accepted")
    ok = conv(lyr_line, accepted=["R-LINE-CAP-JOIN"])
    check(not ok.refusals and ok.layers[0]["paint"] == {"line-color": "rgba(1,2,3,1)", "line-width": 4.0}
          and ok.layers[0]["layout"] == {"line-cap": "round", "line-join": "round"},
          "an accepted line is 3 pt = 4 px wide with round cap and join")
    check(ok.layers[0]["metadata"]["renderport"]["accepted"] == ["R-LINE-CAP-JOIN"],
          "the accepted rule is recorded in the layer metadata")
    check("ACCEPTED R-LINE-CAP-JOIN" in "\n".join(describe(ok)), "the report lists what was accepted")
    outl = _lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs(outline=_sls("esriSLSSolid", 0.75))})
    check(rules(outl) == ["R-LINE-CAP-JOIN"], "a polygon outline needs R-LINE-CAP-JOIN too")
    both = conv(outl, accepted=["R-LINE-CAP-JOIN"]).layers
    check([l["type"] for l in both] == ["fill", "line"] and both[1]["paint"]["line-width"] == 1.0,
          "an accepted polygon is a fill layer under a line layer, 0.75 pt = 1 px")
    check(both[0]["id"] == "layer-0-fill" and both[1]["id"] == "layer-0-line", "each part of a class has its own id")
    ring = conv(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs(style="esriSFSNull", outline=_sls("esriSLSSolid"))}),
                   accepted=["R-LINE-CAP-JOIN"]).layers
    check([l["type"] for l in ring] == ["line"], "a null fill with a solid outline is only the line")

    mo = _simple_pt(_sms(outline=_sls("esriSLSSolid", 1.5, [9, 9, 9, 255])))
    mok = conv(mo).layers[0]["paint"]
    check(mok["circle-stroke-width"] == 2.0 and mok["circle-stroke-color"] == "rgba(9,9,9,1)" and mok["circle-radius"] == 6.0,
          "a marker outline is a 2 px stroke and the radius stays size / 2")
    bare = convert(_simple_pt())
    check([r[0] for r in bare.refusals] == ["R-MARKER-SIZE"] and bare.layers == [],
          "a marker is refused until R-MARKER-SIZE is accepted" + PIN)
    check("    to accept: --accept R-MARKER-SIZE" in describe(bare), "the refusal says how to accept the marker size rule")
    check(not convert(_simple_pt(), accepted=ms).refusals and convert(_simple_pt(), accepted=ms).accepted == ms,
          "an accepted marker size is listed as accepted")

    cbs = _lyr("esriGeometryPolygon", _cb([{"classMaxValue": 10, "symbol": _sfs()}]))
    check(rules(cbs) == ["R-CB-BOUNDARY"], "class breaks are refused until R-CB-BOUNDARY is accepted")
    sc = _simple_pt()
    sc["minScale"], sc["maxScale"] = ZOOM0_SCALE / 4, ZOOM0_SCALE / 64
    check(rules(sc) == ["R-SCALE-RANGE"], "a scale range is refused until R-SCALE-RANGE is accepted")
    z = conv(sc, accepted=["R-SCALE-RANGE"]).layers[0]
    check(z["minzoom"] == 2.0 and z["maxzoom"] == 6.0, "an accepted scale range becomes minzoom 2 and maxzoom 6")
    sc["minScale"], sc["maxScale"] = 0, ZOOM0_SCALE / 64
    z = conv(sc, accepted=["R-SCALE-RANGE"]).layers[0]
    check("minzoom" not in z and z["maxzoom"] == 6.0, "minScale 0 sets no minzoom")
    sc["minScale"], sc["maxScale"] = ZOOM0_SCALE / 4, 0
    z = conv(sc, accepted=["R-SCALE-RANGE"]).layers[0]
    check(z["minzoom"] == 2.0 and "maxzoom" not in z, "maxScale 0 sets no maxzoom")
    sc["minScale"], sc["maxScale"] = ZOOM0_SCALE * 8, ZOOM0_SCALE / 2 ** 40
    z = conv(sc, accepted=["R-SCALE-RANGE"]).layers[0]
    check(z["minzoom"] == 0.0 and z["maxzoom"] == 24.0, "zoom is clamped to MapLibre's 0 to 24")
    sc["minScale"], sc["maxScale"] = 1000, 5000
    check(rules(sc, ["R-SCALE-RANGE"]) == ["R-VALUE"], "minScale below maxScale is refused even when accepted")
    sc["minScale"], sc["maxScale"] = -1, 0
    check(rules(sc, ["R-SCALE-RANGE"]) == ["R-VALUE"], "a negative scale is refused")
    sc["minScale"], sc["maxScale"] = "1000", 0
    check(rules(sc, ["R-SCALE-RANGE"]) == ["R-VALUE"], "a text scale is refused")

    tr = _simple_pt()
    tr["drawingInfo"]["transparency"] = 60
    check(rules(tr) == ["R-LAYER-TRANSPARENCY"], "layer transparency is refused until accepted")
    tl = conv(tr, accepted=["R-LAYER-TRANSPARENCY"]).layers[0]
    check(tl["paint"]["circle-color"] == "rgba(255,0,0,0.4)", "60 percent transparency multiplies alpha by 0.4")
    for badt in (-1, 101, "5", True):
        tr["drawingInfo"]["transparency"] = badt
        check(rules(tr, ["R-LAYER-TRANSPARENCY"]) == ["R-VALUE"], "transparency %s is refused as R-VALUE" % json.dumps(badt))

    # ---- every refusal rule fires, and a refusal produces no layers
    def refused(info, rule, label, accept=()):
        res = conv(info, accepted=accept)
        check(rule in [r[0] for r in res.refusals] and res.layers == [], "%s is refused as %s" % (label, rule))

    refused(5, "R-INPUT", "a non-object input")
    refused({"error": {"code": 499, "message": "Token Required"}}, "R-INPUT", "a service error body" + PIN)
    refused({"geometryType": "esriGeometryEnvelope"}, "R-GEOMETRY", "an envelope layer")
    refused({"geometryType": ["x"]}, "R-GEOMETRY", "a list geometry type")
    refused({"geometryType": "esriGeometryPoint"}, "R-INPUT", "a layer with no drawingInfo")
    refused(_lyr("esriGeometryPoint", None), "R-RENDERER", "a null renderer")
    refused(_lyr("esriGeometryPoint", {"type": "heatmap"}), "R-RENDERER", "a heatmap renderer")
    refused(_lyr("esriGeometryPoint", {"type": ["x"]}), "R-RENDERER", "a list renderer type")
    refused(_simple_pt({"type": "esriPMS", "url": "x.png"}), "R-PICTURE", "a picture marker")
    refused(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": {"type": "esriPFS", "url": "x"}}), "R-PICTURE", "a picture fill")
    refused(_simple_pt({"type": "esriTS", "text": "a"}), "R-SYMBOL-TYPE", "a text symbol")
    refused(_simple_pt(_sfs()), "R-SYMBOL-TYPE", "a fill symbol on a point layer")
    refused(_simple_pt({"type": {"a": 1}}), "R-SYMBOL-TYPE", "a dict symbol type")
    refused(_simple_pt("circle"), "R-VALUE", "a string symbol")
    refused(_lyr("esriGeometryPoint", {"type": "simple"}), "R-VALUE", "a renderer with no symbol")
    for style in ("esriSMSCross", "esriSMSDiamond", "esriSMSSquare", "esriSMSTriangle", "esriSMSX", "esriSMSPath"):
        refused(_simple_pt(_sms(style=style)), "R-MARKER-SHAPE", "marker style %s" % style)
    refused(_simple_pt(_sms(style=["x"])), "R-MARKER-SHAPE", "a list marker style")
    refused(_simple_pt(_sms(xoffset=3)), "R-OFFSET", "a marker x offset")
    refused(_simple_pt(_sms(yoffset=-3)), "R-OFFSET", "a marker y offset")
    refused(_simple_pt(_sms(xoffset="a")), "R-OFFSET", "a text offset")
    for size in (0, -1, "9", None, True):
        refused(_simple_pt(_sms(size=size)), "R-VALUE", "marker size %s" % json.dumps(size))
    refused(_simple_pt(_sms(outline=None)), "R-VALUE", "a marker with no outline")
    refused(_simple_pt(_sms(outline=_sfs())), "R-VALUE", "a marker whose outline is a fill")
    refused(_simple_pt(_sms(color=None)), "R-COLOR", "a marker with a null color")
    for style in ("esriSLSDash", "esriSLSDashDot", "esriSLSDashDotDot", "esriSLSDot", "esriSLSLongDash",
                  "esriSLSShortDash", "esriSLSShortDot"):
        refused(_lyr("esriGeometryPolyline", {"type": "simple", "symbol": _sls(style)}), "R-LINE-DASH",
                "line style %s" % style, ["R-LINE-CAP-JOIN"])
    refused(_simple_pt(_sms(outline=_sls("esriSLSDash"))), "R-LINE-DASH", "a dashed marker outline")
    refused(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs(outline=_sls("esriSLSDot"))}), "R-LINE-DASH", "a dotted polygon outline")
    for width in (0, -2, "2", None):
        refused(_lyr("esriGeometryPolyline", {"type": "simple", "symbol": _sls("esriSLSSolid", width)}),
                "R-VALUE", "solid line width %s" % json.dumps(width), ["R-LINE-CAP-JOIN"])
    refused(_lyr("esriGeometryPolyline", {"type": "simple", "symbol": _sls("esriSLSSolid", 1, [1, 2])}), "R-COLOR",
            "a solid line with a short color", ["R-LINE-CAP-JOIN"])
    refused(_lyr("esriGeometryPolyline", {"type": "simple", "symbol": _sls("bogus")}), "R-LINE-DASH", "an unknown line style")
    for style in _HATCH:
        refused(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs(style=style)}), "R-FILL-HATCH", "fill style %s" % style)
    refused(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs(style="esriSFSBogus")}), "R-VALUE", "an unknown fill style")
    refused(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs(color=None)}), "R-COLOR", "a solid fill with a null color")
    refused(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs(outline=None)}), "R-VALUE", "a fill with no outline")
    refused(_simple_pt(_sms(contentType="x")), "R-UNREAD-KEY", "a marker with an unread key")
    refused(_lyr("esriGeometryPolygon", {"type": "simple", "symbol": _sfs(xscale=1)}), "R-UNREAD-KEY", "a fill with an unread key")
    refused(_lyr("esriGeometryPolyline", {"type": "simple", "symbol": dict(_sls("esriSLSNull"), cap="round")}), "R-UNREAD-KEY", "a line with an unread key")
    refused(_lyr("esriGeometryPoint", {"type": "simple", "symbol": _sms(), "backgroundFillSymbol": {}}), "R-UNREAD-KEY", "a renderer with an unread key")
    unread = _simple_pt()
    unread["drawingInfo"]["filter"] = "x"
    refused(unread, "R-UNREAD-KEY", "a drawingInfo with an unread key")
    vv = _lyr("esriGeometryPoint", {"type": "simple", "symbol": _sms(), "visualVariables": [{"type": "sizeInfo"}]})
    refused(vv, "R-VISUAL-VARIABLE", "a size visual variable")
    refused(_lyr("esriGeometryPoint", {"type": "simple", "symbol": _sms(), "rotationExpression": "[ANGLE]"}),
            "R-VISUAL-VARIABLE", "a rotation expression")
    lab = _simple_pt()
    lab["drawingInfo"]["labelingInfo"] = [{"labelExpression": "[KIND]"}]
    refused(lab, "R-LABELS", "a labelled layer" + PIN)
    ss = _simple_pt()
    ss["drawingInfo"]["scaleSymbols"] = True
    refused(ss, "R-SCALE-SYMBOLS", "symbols that scale with the map")
    ss["drawingInfo"]["scaleSymbols"] = False
    check(not conv(ss).refusals, "scaleSymbols false is fixed screen size and converts")
    check(rules(_simple_pt()) == [], "the baseline fixture raises no refusal")

    # ---- unique value renderers
    uv = _lyr("esriGeometryPoint", _uv([
        {"value": "A", "label": "a", "symbol": _sms(size=6)},
        {"value": "B", "label": "b", "symbol": _sms(size=12)}], default=_sms(size=3)))
    res = conv(uv)
    check(not res.refusals and [l["id"] for l in res.layers] == ["layer-0-circle", "layer-1-circle", "layer-2-circle"],
          "a unique value renderer is one layer per class plus one for the default")
    check([l["paint"]["circle-radius"] for l in res.layers] == [4.0, 8.0, 2.0], "each class keeps its own size")
    check(_drawn(res.layers, {"KIND": "A"}) == ["layer-0-circle"], "a feature with KIND A draws in class A only")
    check(_drawn(res.layers, {"KIND": "Z"}) == ["layer-2-circle"], "an unmatched string draws the default only")
    check(_drawn(res.layers, {}) == ["layer-2-circle"], "a feature with no KIND draws the default only")
    check(_drawn(res.layers, {"KIND": None}) == ["layer-2-circle"], "a null KIND draws the default only")
    check(res.layers[0]["metadata"]["renderport"]["label"] == "a", "the class label is recorded")
    nod = conv(_lyr("esriGeometryPoint", _uv([{"value": "A", "symbol": _sms()}])))
    check(_drawn(nod.layers, {"KIND": "Z"}) == [], "with no defaultSymbol an unmatched feature draws nothing")
    onlyd = conv(_lyr("esriGeometryPoint", _uv([], default=_sms())))
    check(len(onlyd.layers) == 1 and "filter" not in onlyd.layers[0], "a default with no classes has no filter")
    refused(_lyr("esriGeometryPoint", _uv([])), "R-VALUE", "no classes and no default")
    num = conv(_lyr("esriGeometryPoint", _uv([{"value": "1", "symbol": _sms()}, {"value": "2", "symbol": _sms()}], field="CODE", default=_sms())))
    check(num.layers[0]["filter"] == ["==", ["get", "CODE"], 1], "an integer field is matched against the integer 1, not the text '1'" + PIN)
    check(_drawn(num.layers, {"CODE": 1}) == ["layer-0-circle"] and _drawn(num.layers, {"CODE": 3}) == ["layer-2-circle"],
          "an integer attribute 1 draws class '1' and 3 draws the default" + PIN)
    check(_drawn(num.layers, {"CODE": "1"}) == ["layer-2-circle"], "a text attribute '1' does not match an integer class: types are strict")
    flo = conv(_lyr("esriGeometryPoint", _uv([{"value": "1.5", "symbol": _sms()}], field="AREA")))
    check(_drawn(flo.layers, {"AREA": 1.5}) == ["layer-0-circle"], "a double field matches the number 1.5")
    refused(_lyr("esriGeometryPoint", _uv([{"value": "1", "symbol": _sms()}, {"value": "01", "symbol": _sms()}], field="CODE")),
            "R-VALUE", "two class values that are the same integer" + PIN)
    refused(_lyr("esriGeometryPoint", _uv([{"value": "A", "symbol": _sms()}, {"value": "A", "symbol": _sms()}])), "R-VALUE", "a repeated class value")
    refused(_lyr("esriGeometryPoint", _uv([{"value": "x", "symbol": _sms()}], field="CODE")), "R-VALUE", "a non-integer value on an integer field")
    refused(_lyr("esriGeometryPoint", _uv([{"value": 1, "symbol": _sms()}])), "R-VALUE", "a numeric JSON class value")
    refused(_lyr("esriGeometryPoint", _uv([5])), "R-VALUE", "a class that is not an object")
    refused(_lyr("esriGeometryPoint", _uv("x")), "R-VALUE", "uniqueValueInfos that is not a list")
    refused(_lyr("esriGeometryPoint", _uv([{"value": "A", "symbol": _sms()}], field="MADE")), "R-FIELD-TYPE", "a date field")
    refused(_lyr("esriGeometryPoint", _uv([{"value": "A", "symbol": _sms()}], field="NOPE")), "R-FIELD-TYPE", "a field that is not listed")
    refused(_lyr("esriGeometryPoint", _uv([{"value": "A", "symbol": _sms()}], field=["x"])), "R-FIELD-TYPE", "a list field name")
    nof = _lyr("esriGeometryPoint", _uv([{"value": "A", "symbol": _sms()}]))
    nof["fields"] = None
    refused(nof, "R-FIELD-TYPE", "a layer with no fields list")
    nof["fields"] = [5, {"name": 1, "type": "x"}, {"name": "KIND"}, {"name": "KIND", "type": ["x"]}]
    refused(nof, "R-FIELD-TYPE", "a fields list of malformed entries")
    refused(_lyr("esriGeometryPoint", _uv([{"value": "A", "symbol": _sms()}], valueExpression="$feature.A")), "R-UV-ARCADE", "an Arcade value expression")
    refused(_lyr("esriGeometryPoint", _uv([], uniqueValueGroups=[{"classes": []}])), "R-UV-GROUPS", "uniqueValueGroups")
    refused(_lyr("esriGeometryPoint", _uv([{"value": "A, 1", "symbol": _sms()}], field2="CODE", fieldDelimiter=", ")), "R-UV-MULTIFIELD", "a second field")
    refused(_lyr("esriGeometryPoint", _uv([{"value": "A", "symbol": _sms(style="esriSMSX")}])), "R-MARKER-SHAPE", "a refused symbol inside a class")
    both_bad = rules(_lyr("esriGeometryPoint", _uv([{"value": "A", "symbol": _sms(style="esriSMSX")}, {"value": "B", "symbol": _sms(size=0)}])))
    check(both_bad == ["R-MARKER-SHAPE", "R-VALUE"], "every refusal is reported in one run, not only the first")

    # ---- class breaks
    acc = ["R-CB-BOUNDARY"]
    brk = _lyr("esriGeometryPolygon", _cb([
        {"classMaxValue": 10, "classMinValue": 0, "label": "low", "symbol": _sfs()},
        {"classMaxValue": 20, "label": "mid", "symbol": _sfs()}], defaultSymbol=_sfs()))
    res = conv(brk, accepted=acc)
    check(not res.refusals and len(res.layers) == 3, "two accepted breaks and a default are three layers")
    ids = res.layers
    check(_drawn(ids, {"AREA": 0}) == ["layer-0-fill"] and _drawn(ids, {"AREA": 10}) == ["layer-0-fill"],
          "minValue and the first max both fall in the first class")
    check(_drawn(ids, {"AREA": 10.0001}) == ["layer-1-fill"] and _drawn(ids, {"AREA": 20}) == ["layer-1-fill"],
          "a value above the first max up to the second max is the second class")
    check(_drawn(ids, {"AREA": 20.5}) == ["layer-2-fill"] and _drawn(ids, {"AREA": -1}) == ["layer-2-fill"],
          "values outside every class draw the default")
    check(_drawn(ids, {"AREA": None}) == ["layer-2-fill"] and _drawn(ids, {}) == ["layer-2-fill"],
          "a null or missing number draws the default and does not raise" + PIN)
    check(_drawn(ids, {"AREA": 5}).count("layer-0-fill") == 1 and len(_drawn(ids, {"AREA": 10})) == 1,
          "no value draws in two classes")
    check(res.accepted == ["R-CB-BOUNDARY"], "the boundary choice is listed as accepted")
    nd = conv(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": 10, "symbol": _sfs()}], field="CODE")), accepted=acc)
    check(_drawn(nd.layers, {"CODE": 99}) == [], "with no defaultSymbol a value above the last max draws nothing")
    raises(lambda: _eval(["<", ["get", "A"], 1], {"A": None}), "the model raises on a null compared with a number")
    check(_eval(["match", ["get", "A"], [1], True, False], {"A": True}) is False
          and _eval(["match", ["get", "A"], [1], True, False], {"A": 1}) is True,
          "the model's match is strictly typed: true is not 1" + PIN)
    check(_eval(["==", ["get", "A"], 1], {"A": True}) is False and _eval(["==", ["get", "A"], 1], {"A": 1}) is True,
          "the model treats a boolean as a different type from the number 1" + PIN.encode().decode())
    refused(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": 10, "symbol": _sfs()}], normalizationType="esriNormalizeByLog")),
            "R-CB-NORMALIZE", "a normalized class break renderer", acc)
    refused(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": 10, "symbol": _sfs()}], field="KIND")), "R-FIELD-TYPE", "class breaks on a string field", acc)
    refused(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": 10, "symbol": _sfs()}], minValue=None)), "R-VALUE", "class breaks with no minValue", acc)
    refused(_lyr("esriGeometryPolygon", _cb([], minValue=0)), "R-VALUE", "class breaks with no classes", acc)
    refused(_lyr("esriGeometryPolygon", _cb("x")), "R-VALUE", "classBreakInfos that is not a list", acc)
    refused(_lyr("esriGeometryPolygon", _cb(["x"])), "R-VALUE", "a class break that is not an object", acc)
    refused(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": "9", "symbol": _sfs()}])), "R-VALUE", "a text classMaxValue", acc)
    refused(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": 10, "classMinValue": 11, "symbol": _sfs()}])), "R-VALUE", "classMinValue above classMaxValue", acc)
    refused(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": 10, "classMinValue": "a", "symbol": _sfs()}])), "R-VALUE", "a text classMinValue", acc)
    refused(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": 10, "symbol": _sfs()}, {"classMaxValue": 10, "symbol": _sfs()}])),
            "R-CB-ORDER", "two classes with the same max", acc)
    refused(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": -5, "symbol": _sfs()}], minValue=0)), "R-CB-ORDER", "a first max below minValue", acc)
    one = conv(_lyr("esriGeometryPolygon", _cb([{"classMaxValue": 0, "symbol": _sfs()}], minValue=0)), accepted=acc)
    check(not one.refusals and _drawn(one.layers, {"AREA": 0}) == ["layer-0-fill"], "a first max equal to minValue is a one value class")

    # ---- the report
    rep = describe(conv(lyr_line))
    check(rep[0] == "REFUSED  R-LINE-CAP-JOIN  renderer.symbol", "a refusal line names the rule and the JSON path")
    check("    to accept: --accept R-LINE-CAP-JOIN" in rep and rep[-1] == "1 refusal(s). No style was produced.",
          "an acceptable rule says how to accept it, and the report ends with the count")
    rep = describe(conv(_simple_pt(_sms(style="esriSMSX"))))
    check(not any(line.startswith("    to accept") for line in rep), "an unacceptable rule offers no --accept")
    check(all(r in RULES for r in ("R-INPUT", "R-VALUE")) and sum(1 for v in RULES.values() if v[0]) == 5,
          "five rules can be accepted")

    # ---- every output is JSON and ASCII
    full = conv(brk, accepted=acc)
    text = json.dumps(full.layers)
    check(json.loads(text) == full.layers and all(ord(ch) < 128 for ch in text), "the output is JSON and pure ASCII")
    uni = _simple_pt()
    uni["drawingInfo"]["renderer"]["label"] = u"caf\u00e9"
    check(json.loads(json.dumps(conv(uni).layers))[0]["metadata"]["renderport"]["label"] == u"caf\u00e9"
          and all(ord(ch) < 128 for ch in json.dumps(conv(uni).layers)), "a non-ASCII label survives as an ASCII escape")

    # ---- the command line, files and the write guard
    tmp = tempfile.mkdtemp(prefix="renderport-")
    try:
        src = os.path.join(tmp, "layer.json")
        with io.open(src, "w", encoding="utf-8") as handle:
            json.dump(_simple_fill(), handle)
        dest = os.path.join(tmp, "out.json")
        status, out, err = _run(["--layer-json", src])
        check(status == 0 and "1 layer(s) converted." in out and "Check only." in out, "a check run converts and writes nothing")
        status, out, err = _run(["--layer-json", src, "--out", dest])
        check(status == 0 and not os.path.exists(dest) and "--apply" in out, "--out without --apply writes nothing and says so" + PIN)
        for prefix in ("--ap", "--app", "--appl", "--appy"):
            status, out, err = _run(["--layer-json", src, "--out", dest, prefix])
            check(status == 2 and not os.path.exists(dest) and "unrecognized arguments" in err,
                  "the abbreviation %s of --apply is refused by the parser" % prefix + PIN)
        status, out, err = _run(["--layer-json", src, "--ou", dest, "--apply"])
        check(status == 2 and not os.path.exists(dest), "the abbreviation --ou of --out is refused too" + PIN)
        status, out, err = _run(["--layer-json", src, "--apply"])
        check(status == 64 and "--out" in err, "--apply without --out is a usage error")
        status, out, err = _run(["--layer-json", src, "--out", dest, "--apply"])
        written = io.open(dest, "r", encoding="utf-8").read()
        check(status == 0 and json.loads(written) == convert(_simple_fill()).layers and "wrote 1 layer(s)" in out,
              "--apply writes the converted layers")
        digest = hashlib.sha256(written.encode("utf-8")).hexdigest()
        status, out, err = _run(["--layer-json", src, "--out", dest, "--apply"])
        again = io.open(dest, "r", encoding="utf-8").read()
        check(status == 2 and "cannot create" in err, "--apply never overwrites an existing file" + PIN)
        check(hashlib.sha256(again.encode("utf-8")).hexdigest() == digest, "the existing file is byte for byte unchanged" + PIN)
        missing = os.path.join(tmp, "no" + os.sep + "such" + os.sep + "out.json")
        status, out, err = _run(["--layer-json", src, "--out", missing, "--apply"])
        check(status == 2 and not os.path.exists(missing), "an unwritable --out path is an I/O failure, exit 2")
        bad = os.path.join(tmp, "bad.json")
        with io.open(bad, "w", encoding="utf-8") as handle:
            handle.write(u"{not json")
        status, out, err = _run(["--layer-json", bad])
        check(status == 2 and "cannot read" in err, "a malformed layer file is exit 2")
        status, out, err = _run(["--layer-json", os.path.join(tmp, "absent.json")])
        check(status == 2 and "cannot read" in err, "a missing layer file is exit 2")
        refuse_src = os.path.join(tmp, "refuse.json")
        with io.open(refuse_src, "w", encoding="utf-8") as handle:
            json.dump(_simple_pt(_sms(style="esriSMSX")), handle)
        status, out, err = _run(["--layer-json", refuse_src, "--out", os.path.join(tmp, "r.json"), "--apply"])
        check(status == 1 and "REFUSED  R-MARKER-SHAPE" in out and not os.path.exists(os.path.join(tmp, "r.json")),
              "a refused conversion exits 1 and writes nothing even under --apply" + PIN)
        line_src = os.path.join(tmp, "line.json")
        with io.open(line_src, "w", encoding="utf-8") as handle:
            json.dump(lyr_line, handle)
        status, out, err = _run(["--layer-json", line_src])
        check(status == 1 and "to accept: --accept R-LINE-CAP-JOIN" in out, "the CLI refuses a line until it is accepted")
        status, out, err = _run(["--layer-json", line_src, "--accept", "R-LINE-CAP-JOIN"])
        check(status == 0 and "ACCEPTED R-LINE-CAP-JOIN" in out, "the CLI converts a line once the rule is accepted")
        status, out, err = _run(["--layer-json", line_src, "--accept", "R-LINE-CAP-JOIN", "--accept", "R-SCALE-RANGE"])
        check(status == 0, "--accept can repeat")
        status, out, err = _run(["--layer-json", line_src, "--accept", "R-LINE-CAP-JOIN,R-SCALE-RANGE"])
        check(status == 0, "--accept takes a comma list")
        status, out, err = _run(["--layer-json", line_src, "--accept", "R-NOPE"])
        check(status == 64 and "R-NOPE" in err, "an unknown rule in --accept is a usage error")
        status, out, err = _run(["--layer-json", line_src, "--accept", "R-MARKER-SHAPE"])
        check(status == 64 and "cannot be accepted" in err, "a rule that cannot be accepted is a usage error" + PIN)
        status, out, err = _run([])
        check(status == 64 and "exactly one" in err, "no input is a usage error")
        status, out, err = _run(["--layer-json", src, "--url", "http://127.0.0.1:1/x"])
        check(status == 64, "both --layer-json and --url is a usage error")
        status, out, err = _run(["--layer-json", src, "--bogus"])
        check(status == 2 and "unrecognized arguments" in err, "an unknown flag is refused by the parser")
    finally:
        shutil.rmtree(tmp)

    # ---- the network path, against a local stub
    layer_body = json.dumps(_simple_fill()).encode("utf-8")
    _Stub.seen = []
    _Stub.routes = {
        "/ok/FeatureServer/0": (200, layer_body),
        "/err/FeatureServer/0": (200, json.dumps({"error": {"code": 499, "message": "Token Required"}}).encode("utf-8")),
        "/html/FeatureServer/0": (200, b"<html>not json</html>"),
        "/big/FeatureServer/0": (200, b"x" * 64),
    }
    server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        status, out, err = _run(["--url", base + "/ok/FeatureServer/0"])
        check(status == 0 and "1 layer(s) converted." in out, "--url fetches a layer and converts it")
        check(_Stub.seen[-1] == "/ok/FeatureServer/0?f=json", "the request asks for f=json")
        _run(["--url", base + "/ok/FeatureServer/0?f=html&x=1"])
        check(_Stub.seen[-1] == "/ok/FeatureServer/0?x=1&f=json", "a caller's own f= is replaced by f=json and other parameters stay" + PIN)
        status, out, err = _run(["--url", base + "/err/FeatureServer/0"])
        check(status == 1 and "R-INPUT" in out and "Token Required" in out, "a 200 answer with an error body is refused, not converted" + PIN)
        status, out, err = _run(["--url", base + "/html/FeatureServer/0"])
        check(status == 2 and "cannot read" in err, "an answer that is not JSON is exit 2")
        status, out, err = _run(["--url", base + "/nothing/FeatureServer/0"])
        check(status == 2 and "404" in err, "an HTTP 404 is exit 2")
        raises(lambda: _fetch(base + "/big/FeatureServer/0", limit=10), "an answer above the size limit is refused")
        raises(lambda: _fetch("file:///etc/passwd"), "a file: URL is refused" + PIN)
        raises(lambda: _fetch("ftp://127.0.0.1/x"), "an ftp: URL is refused")
        status, out, err = _run(["--url", "file:///etc/passwd"])
        check(status == 2 and "only http and https" in err, "the CLI refuses a file: URL with exit 2" + PIN)
        probe_url = base + "/ok/FeatureServer/0"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    status, out, err = _run(["--url", probe_url])
    check(status == 2 and "cannot read" in err, "a stopped service is exit 2")

    return h.footer()


# =============================================================================
# CLI
# =============================================================================

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="renderport.py",
        allow_abbrev=False,
        description="Convert an ArcGIS layer renderer to MapLibre style layers, "
                    "and refuse every approximation.",
        epilog="Nothing is written without --apply. --apply never overwrites.",
    )
    ap.add_argument("--layer-json", dest="layer_json", help="path to the layer JSON (the answer of <layer url>?f=json)")
    ap.add_argument("--url", help="layer URL; the tool requests <url>?f=json (http and https only)")
    ap.add_argument("--source", default="features", help="MapLibre source id for each layer (default features)")
    ap.add_argument("--source-layer", dest="source_layer", default="features",
                    help="vector tile layer name for each layer (default features)")
    ap.add_argument("--id-prefix", dest="id_prefix", default="layer", help="layer id prefix (default layer)")
    ap.add_argument("--accept", action="append", default=[], metavar="RULE[,RULE]",
                    help="accept one approximation (R-MARKER-SIZE, R-LINE-CAP-JOIN, "
                         "R-CB-BOUNDARY, R-SCALE-RANGE, R-LAYER-TRANSPARENCY); repeat or comma separate")
    ap.add_argument("--out", help="file to create with the layers; needs --apply")
    ap.add_argument("--apply", action="store_true", help="write --out (never overwrites a file)")
    ap.add_argument("--self-test", dest="self_test", action="store_true", help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        print("renderport self-test: no arcpy, no database, local stub server only")
        print("-" * 68)
        return self_test()

    if bool(args.layer_json) == bool(args.url):
        print("error: give exactly one of --layer-json or --url.", file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out PATH.", file=sys.stderr)
        return 64
    accepted = [r.strip() for item in args.accept for r in item.split(",") if r.strip()]
    for rule in accepted:
        if rule not in RULES:
            print("error: unknown rule %s." % rule, file=sys.stderr)
            return 64
        if not RULES[rule][0]:
            print("error: %s cannot be accepted." % rule, file=sys.stderr)
            return 64

    try:
        info = _load(args)
    except (OSError, ValueError) as exc:
        print("error: cannot read the layer definition: %s" % exc, file=sys.stderr)
        return 2

    result = convert(info, args.source, args.source_layer, args.id_prefix, accepted)
    for line in describe(result):
        print(line)
    if result.refusals:
        return 1
    if not args.apply:
        print("Check only. Nothing was written.")
        if args.out:
            print("Add --apply to create %s." % args.out)
        return 0
    try:
        _write_new(args.out, result.layers)
    except OSError as exc:
        print("error: cannot create %s: %s" % (args.out, exc), file=sys.stderr)
        return 2
    print("wrote %d layer(s) to %s" % (len(result.layers), args.out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
