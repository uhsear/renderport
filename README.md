# renderport

Convert an ArcGIS layer renderer to MapLibre style layers, and refuse every symbology approximation instead of drawing it.

A utility team moves its valve map from an ArcGIS web app to MapLibre. The layer draws gate valves as red circles,
check valves as blue triangles and blow-offs as black crosses. The team runs a converter. The converter has no
triangle and no cross, so it draws a circle for each one and prints nothing. The new map loads, the legend is
right, and every symbol looks plausible. Three months later a crew digs for a check valve that the map shows as
a gate valve, because the only difference between the two symbols was a shape the converter dropped. Nobody
caught it. The conversion did what it was told, and it never said what it left out.

This tool does the opposite. Each place where the MapLibre style specification cannot carry an Esri symbol
property, or where the Esri reference does not say what a value means, is a named rule. A conversion that hits a
rule stops, prints the rule and the JSON path, exits 1 and produces no style:

```
$ python renderport.py --layer-json valves.json --accept R-MARKER-SIZE
REFUSED  R-MARKER-SHAPE  renderer.uniqueValueInfos[1].symbol
    a MapLibre circle layer draws only circles -- style "esriSMSTriangle"
REFUSED  R-MARKER-SHAPE  renderer.uniqueValueInfos[2].symbol
    a MapLibre circle layer draws only circles -- style "esriSMSX"
ACCEPTED R-MARKER-SIZE: the Esri reference does not say whether a marker size is a diameter or includes the outline, and MapLibre draws a circle stroke outside circle-radius
2 refusal(s). No style was produced.
```

Without `--accept R-MARKER-SIZE` the same layer reports 5 refusals, because every marker needs that rule.

Every refusal is reported in one run, not only the first. Five rules describe an approximation that is often
acceptable, such as a line cap. Those you can accept one at a time with `--accept`. The accepted rule is printed
and is written into each layer's `metadata`.

## The existing alternative

Searched on 2026-10-22 with `gh search repos` for `renderport`, `esri maplibre`, `esri mapbox style`,
`arcgis mapbox-gl`, `esri2mapbox`, `esri-to-mapbox` and `arcgis renderer maplibre`. Several unrelated repositories
share the name renderport (portfolio sites), and this repository is one of the hits. No result described a
converter that refuses what it cannot draw, but I read only descriptions and READMEs, never source, so I cannot rule
one out. These are the nearest:

- [bojko108/ol-esri-style](https://github.com/bojko108/ol-esri-style) turns an Esri style definition into an
  OpenLayers style function, in the browser, from a layer URL in one call. It also handles labels. It targets
  OpenLayers, not MapLibre. I did not check how it treats a symbol it cannot draw.
- [muimsd/esri-gl](https://github.com/muimsd/esri-gl) and
  [frontiersi/mapbox-gl-esri-sources](https://github.com/frontiersi/mapbox-gl-esri-sources) let mapbox-gl or
  maplibre-gl read Esri services as sources. They solve the data side. They are not a symbology converter.
- [ZhouGYong/Esri2MapboxGL](https://github.com/ZhouGYong/Esri2MapboxGL) and
  [timkeane/esri-mapbox-style](https://github.com/timkeane/esri-mapbox-style) match the search terms by name. Neither
  has a description. I read only the first README, which lists a `style.json`. I did not check what either does with a
  symbol it cannot draw.
- ArcGIS's own tools publish vector tile packages with an Esri style. That keeps the Esri look inside Esri
  clients, which is the better choice when you stay on ArcGIS.

renderport does one narrow job: it converts a renderer you hand it, and it treats a silent approximation as a
failure.

## Requirements

Python 3.9 or newer. The standard library only. No arcpy, no network for the self-test beyond a stub server on
127.0.0.1.

```
git clone https://github.com/uhsear/renderport.git
python renderport.py --self-test
```

## Usage

Check first. Nothing is written without `--apply`.

```
python renderport.py --layer-json valves.json
python renderport.py --layer-json valves.json --out layers.json --apply
python renderport.py --url https://host/arcgis/rest/services/Utilities/FeatureServer/0
python renderport.py --layer-json lines.json --accept R-LINE-CAP-JOIN,R-SCALE-RANGE
```

The input is the JSON of one layer, which is the answer of `<layer url>?f=json`. The output is a JSON array of
MapLibre layer objects. Put them in your style's `layers` list next to a `vector` source that carries the same
attribute names.

| Flag | Default | What it does |
|---|---|---|
| `--layer-json` | none | Path to the saved layer JSON. Give this or `--url`. |
| `--url` | none | Layer URL. The tool requests `<url>?f=json`. http and https only. |
| `--source` | `features` | MapLibre source id written into each layer. |
| `--source-layer` | `features` | Vector tile layer name written into each layer. |
| `--id-prefix` | `layer` | Layer ids are `<prefix>-<class number>-<circle, line or fill>`. |
| `--accept` | none | Accept an approximation by rule id. Repeat the flag or use commas. |
| `--out` | none | File to create. Needs `--apply`. |
| `--apply` | off | Create `--out`. It never overwrites a file. |
| `--self-test` | off | Run the offline assertions and exit. |

Exit codes: 0 converted, 1 refused, 2 input or output failure, 64 usage error.

The parser sets `allow_abbrev=False`. The prefixes `--ap`, `--app`, `--appl` and `--ou` are refused, so a typo
never writes a file.

## The rules

Rules marked "accept" can be passed to `--accept`. The others always refuse, because no setting makes them safe.

| Rule | Accept | Refused because |
|---|---|---|
| `R-INPUT` | no | The input is not a layer definition, or the service answered 200 with an `error` body. |
| `R-GEOMETRY` | no | The geometry type is not point, multipoint, polyline or polygon. |
| `R-RENDERER` | no | The renderer is missing or is not simple, uniqueValue or classBreaks. |
| `R-SYMBOL-TYPE` | no | The symbol is not the simple marker, line or fill that matches the geometry. |
| `R-PICTURE` | no | Picture markers and fills need a sprite image. |
| `R-MARKER-SHAPE` | no | A circle layer draws only circles. Cross, diamond, square, triangle and X are refused. |
| `R-MARKER-SIZE` | accept | MapLibre draws a circle stroke outside `circle-radius`. The Esri reference does not say whether the size is a diameter or includes the outline. Every marker needs this rule. |
| `R-OFFSET` | no | The Esri reference does not say which direction a positive offset moves. |
| `R-LINE-DASH` | no | The Esri reference does not define the dash lengths of its named styles. |
| `R-LINE-CAP-JOIN` | accept | The REST line symbol has no cap or join. Accepting emits `round` for both. |
| `R-FILL-HATCH` | no | Hatch fills need a sprite pattern. |
| `R-COLOR` | no | The color is null or is not four numbers from 0 to 255. |
| `R-VALUE` | no | A value is missing, malformed or ambiguous. |
| `R-UNREAD-KEY` | no | A symbol, renderer or `drawingInfo` has a property this tool does not read. |
| `R-VISUAL-VARIABLE` | no | Visual variables and rotation expressions change symbols per feature. |
| `R-UV-ARCADE`, `R-UV-GROUPS`, `R-UV-MULTIFIELD` | no | Arcade value expressions, `uniqueValueGroups` and matching on `field2` or `field3`. |
| `R-FIELD-TYPE` | no | The renderer field is not in `fields`, or its type is not string, integer or float. |
| `R-CB-NORMALIZE` | no | Normalized class breaks. |
| `R-CB-BOUNDARY` | accept | The Esri reference does not say whether a class bound is inclusive. |
| `R-CB-ORDER` | no | Class maximums must rise strictly from `minValue`. |
| `R-LABELS` | no | Label placement and label expressions. |
| `R-SCALE-SYMBOLS` | no | Any `scaleSymbols` value, true or false. The property name says symbols scale, and the Esri page says they keep one screen size. The two readings are opposite. |
| `R-SCALE-RANGE` | accept | The reference does not define the direction of `minScale` and `maxScale` or the meaning of 0. |
| `R-LAYER-FILTER` | no | A non-empty `definitionExpression`, or a `defaultVisibility` other than true. The service hides features, and the emitted layers would draw them. |
| `R-LAYER-TRANSPARENCY` | accept | MapLibre alpha is per feature. The reference does not say how layer transparency composites. |

What does convert without a rule: a solid fill, a null line, a null fill and a null fill outline, which draw nothing
in both systems. A marker always needs `--accept R-MARKER-SIZE`, and a line needs `--accept R-LINE-CAP-JOIN`.
A marker whose outline is null is refused as `R-VALUE`. A unique
value or class break renderer becomes one layer per class, each with a `filter`.

## What was checked, and against what

Each fact below was read from the cited page on 2026-10-22. Where the page is silent, the tool refuses.

| Fact | Source |
|---|---|
| Simple marker `size`, `xoffset` and `yoffset` are in points. The page does not say which way a positive offset moves. Styles include `esriSMSTriangle`. | [esriSMS](https://developers.arcgis.com/web-map-specification/objects/esriSMS_symbol/) |
| Simple line `width` is in points. Style names include short and long variants. The page does not define their dash lengths. | [esriSLS](https://developers.arcgis.com/web-map-specification/objects/esriSLS_symbol/) |
| Color is `[r, g, b, a]`, each 0 to 255. A symbol with no color has a null color. | [Symbol objects](https://developers.arcgis.com/rest/services-reference/enterprise/symbol-objects/) |
| Class break bounds: the page documents `minValue` and `classMaxValue` and gives no inclusive or exclusive wording. | [Renderer objects](https://developers.arcgis.com/rest/services-reference/enterprise/renderer-objects/), [classBreaksRenderer](https://developers.arcgis.com/web-map-specification/objects/classBreaksRenderer/) |
| Unique value renderers have `field1` to `field3`, `valueExpression` (Arcade) and `uniqueValueGroups`. | [uniqueValueRenderer](https://developers.arcgis.com/web-map-specification/objects/uniqueValueRenderer/) |
| `drawingInfo.transparency` runs from 0 (none) to 100 (fully transparent). `scaleSymbols` is described as whether symbols "stay the same size in screen units as you zoom in", which the property name contradicts. | [drawingInfo](https://developers.arcgis.com/web-map-specification/objects/drawingInfo/) |
| `minScale` and `maxScale` are described with wording that does not settle direction. The page does not say what 0 means. | [layerDefinition](https://developers.arcgis.com/web-map-specification/objects/layerDefinition/) |
| A layer answers `?f=json`. | [Feature layer](https://developers.arcgis.com/rest/services-reference/enterprise/layer-feature-service/) |
| The JavaScript line symbol defaults `cap` and `join` to `round`. | [SimpleLineSymbol](https://developers.arcgis.com/javascript/latest/references/core/symbols/SimpleLineSymbol/) |
| 1 pt is 1/72 in and 1 px is 1/96 in. | [CSS Values 3, section 5.2](https://www.w3.org/TR/css-values-3/) |
| Web mercator level 0 is scale 591657527.591555 at 256 pixel tiles and 96 dpi. | [Esri web map zoom levels](https://www.esri.com/arcgis-blog/products/product/mapping/web-map-zoom-levels-updated) |
| `circle-radius` and `line-width` are pixels. Strokes sit outside `circle-radius`. `line-dasharray` is in line widths. `minzoom` is visible and `maxzoom` is hidden. | [MapLibre layers](https://maplibre.org/maplibre-style-spec/layers/) |
| `match` and `==` are strictly typed. A `match` label array holds all strings or all numbers. `all` and `any` short-circuit. `<` on a non-number is an error. | [MapLibre expressions](https://maplibre.org/maplibre-style-spec/expressions/) |
| Alpha is applied per feature and accumulates where features overlap. `line-layer-opacity` composites a whole layer and is in MapLibre GL JS 6.0.0 and later only. | [MapLibre layers](https://maplibre.org/maplibre-style-spec/layers/) |

The conversions that follow from those facts:

- Size and width: pixels = points x 96/72. A marker `size` is a diameter, so the radius is size x 96/72 / 2.
- Scale to zoom (only under `--accept R-SCALE-RANGE`): zoom = log2(295828763.795777 / scale). That constant is the
  level 0 scale above, halved, because a 512 pixel MapLibre level is one level below the matching 256 pixel one.
  The tool reads `minScale` as the zoomed-out limit and 0 as no limit. The reference does not confirm either.
- A unique value class on an integer or double field matches a number, not the text. A MapLibre `match` against
  the text `"1"` never matches the number 1.
- Class breaks (only under `--accept R-CB-BOUNDARY`) are read as (previous max, max], and the first as
  [minValue, max]. A null value and a value outside every class draw the default symbol if one exists.

## Self-test

```
$ python renderport.py --self-test
renderport self-test: no arcpy, no database, local stub server only
--------------------------------------------------------------------
PASS  the harness counts passes and failures, names each failure and exits 1
PASS  _fmt trims zeros and keeps 4 places at most
PASS  _num rejects bool, str, nan and inf  <-- pinned defect
...
PASS  an integer attribute 1 draws class '1' and 3 draws the default  <-- pinned defect
...
PASS  the abbreviation --ap of --apply is refused by the parser  <-- pinned defect
...
PASS  a 200 answer with an error body is refused, not converted  <-- pinned defect
...
--------------------------------------------------------------------
286 assertions, 0 failed
```

Windows Python 3.13.2, Linux Python 3.12.3, and Windows Python 3.9.25 and 3.10.20 print the same 286 assertions.
The Windows output has CRLF line ends and the Linux output has LF, so the two files match after you strip the CR
(compared with `cmp`). Branch coverage is 100 percent:
`coverage run --branch renderport.py --self-test` reports 1045 statements, 264 branches, 0 missed. The only line
that the run does not execute is the `if __name__ == "__main__"` guard, marked `# pragma: no cover`.
Linux has no `coverage` module installed, so coverage was measured on Windows only.

## What each claim ran against

| Claim | Where it ran |
|---|---|
| A refusal exits 1 and writes nothing, even under `--apply`. | Self-test, "a refused conversion exits 1 and writes nothing even under --apply". |
| Nothing writes without `--apply`. | Self-test, "--out without --apply writes nothing and says so", and the same run on the command line. |
| A prefix of `--apply` is refused. | Self-test, four prefixes plus `--ou`, each checked for exit 2 and no file. |
| `--apply` never overwrites. | Self-test, the second run exits 2 and the file hash is unchanged. |
| Every rule fires. | Self-test, one fixture per rule, and per style where a rule covers several (six marker shapes, seven line styles, six hatch styles). |
| All refusals appear in one run. | Self-test, a layer with two bad classes reports two rules. |
| Accepted approximations are recorded. | Self-test, the layer `metadata` and the report line `ACCEPTED`. |
| A numeric field matches numbers. | Self-test, an evaluator model checks attribute 1, "1" and 3. |
| A class break bound lands in one class. | Self-test, values on and beside each bound draw in exactly one layer. |
| A null attribute does not raise. | Self-test, the evaluator model draws the default and does not raise. |
| `--url` reads a layer, adds `f=json`, keeps other parameters. | Self-test, a stub `http.server` on 127.0.0.1 records the request path. |
| A 200 answer holding `error` is refused. | Self-test, the stub answers `{"error": {"code": 499}}`. |
| A non-JSON answer, a 404, a stopped service and an oversize answer are exit 2 or an error. | Self-test, the stub and a shut-down stub. |
| `file:` and `ftp:` URLs are refused. | Self-test. |
| The tool does not crash on malformed JSON. | Not in the self-test, and the claim is false for absurd numbers (see Limits). A scratch fuzz run mutated four fixture layers 8000 times each (32000 conversions) before the latest rules were added. The scratch script is not in the repository, so you cannot rerun it. |
| A layer with a filter or a scaleSymbols value is refused. | Self-test, `R-LAYER-FILTER` and `R-SCALE-SYMBOLS` fixtures for each value, and clean cases for absent, null, empty and true. |
| The self-test can fail. | A scratch run, not kept in the repository, changed one line at a time in 17 places (unit factor, radius, `allow_abbrev`, the exclusive-create write, the URL scheme test, a class bound, the error-body test, integer parsing, duplicate detection, the typed `match`, labels, the zoom constant, the offset test, the exit code, a null guard, the size test, the `--apply` guard). All 17 failed the self-test. A later sweep of the decision core found two lines no assertion pinned, and the self-test now pins both. |

## Limits

- The output is not run in MapLibre. The self-test checks every filter with a small model of the expressions this
  tool emits (`get`, `==`, `!=`, `<`, `<=`, `>`, `>=`, `all`, `any`, `!`, `match`). I wrote that model from the
  specification text above. It is not MapLibre. The specification page does not say whether `null` is a valid
  input to `==`; the class break filters use it as a guard, as widely used styles do.
- Only the REST JSON symbols are read. CIM symbols, `.lyrx` files and vector tile style files are not.
  [cimscan](https://github.com/uhsear/cimscan) reads `.lyrx` data sources without a licence, but it does not
  convert symbols.
- The tiles must keep the attribute types listed in `fields`. A tile pipeline that turns every attribute into text
  breaks the numeric matches, and no check here can see that.
- An absent `defaultSymbol` is read as "draw nothing for unmatched features". The REST reference does not say so.
  An absent or null `scaleSymbols` is not refused, and the tool draws fixed-size circles and lines.
- Field names match exactly, including case.
- Layer-level keys outside `drawingInfo` are not read, except that a non-empty `definitionExpression` and a
  `defaultVisibility` other than true are refused as `R-LAYER-FILTER`. Time settings and other keys convert clean.
- A secured service needs a saved copy: the tool has no credential flag. Save the JSON and use `--layer-json`.
- The conversion constant for scale assumes 96 dpi and the standard web mercator scheme. Zoom is clamped to 0
  to 24. MapLibre hides a layer at `maxzoom` itself, so a layer is hidden one step early at that exact zoom.
- The self-test also ran green with the same count on Windows CPython 3.9.25 and 3.10.20.
- Marker `angle` is ignored because a circle looks the same turned. Rotation expressions are refused.
- A JSON number of about 309 digits or more (a marker size, a class bound or a `minScale` written as `10**400`)
  raises `OverflowError` in the converter and ends in a traceback with exit 1.
- A marker size or line width above about 1.3e308 points converts without a refusal and writes the invalid JSON
  token `Infinity`. A marker size below about 0.00008 points rounds to a circle radius of 0, so nothing draws, and no
  rule refuses it.
- A renderer label that holds the JSON literal `NaN` or `Infinity` is copied into layer metadata, so `--apply` can
  write a file that is not valid JSON.
- An unread symbol, renderer or `drawingInfo` key with non-ASCII characters is printed raw by `R-UNREAD-KEY`. On a
  Windows pipe or redirect that uses cp1252 this raises `UnicodeEncodeError`, and the traceback exits 1.
- Any uncaught exception exits 1, which is also the exit code of a refusal, so a caller cannot tell a crash from a
  clean refusal by the exit code alone.
- Exit code 2 covers input and output failures and parser errors (an unknown or abbreviated flag). Only the tool's
  own usage checks return 64.
- Output paths that are devices or streams behave by platform. `--out NUL` on Windows reports `wrote 1 layer(s)` and
  discards the data, `--out con` ends in a `ValueError` traceback, and `--out name.json:stream` creates an empty
  `name.json` plus an NTFS alternate stream.
- A write that fails part way, such as a full disk, leaves a partial `--out` file, because the file is created before
  it is written, and the tool exits 2.
- A layer file saved with a UTF-8 BOM or as UTF-16 (the Windows PowerShell 5 default) is refused with exit 2. The BOM
  message names the fix. The UTF-16 message does not. Resave the file as plain UTF-8.
- `drawingInfo.showLabels` and `fixedSymbols` are accepted without a refusal, and no label layers are emitted, so a
  layer that shows labels converts with them missing when `labelingInfo` is null.
- The Esri reference is silent on how a null attribute matches a unique value class. A class value of empty text or
  `<Null>` is read as that literal text, so a feature with a null attribute draws the default symbol, and no rule
  refuses it.
- A float class value written as `1_000` is accepted as 1000.0, because Python `float()` allows underscores. An
  integer class value with a trailing newline (`5\n`) is accepted as 5, because the integer pattern ends with `$`.
- JSON objects with duplicate keys keep the last value, with no refusal.
- `--url` follows redirects, including an http to ftp redirect that `urllib` permits, so the fetch can reach a URL
  taken from the server's reply. The query token is not copied to the new URL.
- `--url` rebuilds the query with `parse_qsl`, so parameters with an empty value, such as `where=`, are dropped from
  the request.
- A token in the `--url` query goes over plain http if the URL says http, and it stays in shell history and the
  process list. Credentials written as `user:password@host` are not supported: the whole userinfo string goes to the
  system resolver as part of the host name, and some userinfo text is echoed in the "cannot read" error.
- The first 200 characters of a service error body are printed on an `R-INPUT` refusal, so anything the server echoes
  back in that body appears in the output.
- A marker outline object without an explicit `"type": "esriSLS"`, or a null or absent marker outline, is refused as
  `R-VALUE`, so some saved layer files need a hand edit before they convert.
- The claim that `line-layer-opacity` exists only in MapLibre GL JS 6.0.0 and later depends on the cited
  specification page staying current. The external citations in this file were not fetched again after the first
  build.
- Linux has no `coverage` module, so the 100 percent branch figure was measured on Windows only. `pyflakes` was also
  run on Windows only.
- The self-test opens about a dozen loopback connections. On a Windows host with no free ephemeral ports, two network
  assertions can fail with `WinError 10048`, and a rerun passes.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio:

- [cimscan](https://github.com/uhsear/cimscan) - report every data source in `.aprx`, `.lyrx` and `.mapx` files without a licence
- [safe-republish](https://github.com/uhsear/safe-republish) - the same refusal habit applied to a nightly truncate and append
