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
$ python renderport.py --layer-json valves.json
REFUSED  R-MARKER-SHAPE  renderer.uniqueValueInfos[1].symbol
    a MapLibre circle layer draws only circles -- style "esriSMSTriangle"
REFUSED  R-MARKER-SHAPE  renderer.uniqueValueInfos[2].symbol
    a MapLibre circle layer draws only circles -- style "esriSMSX"
2 refusal(s). No style was produced.
```

Every refusal is reported in one run, not only the first. Five rules describe an approximation that is often
acceptable, such as a line cap. Those you can accept one at a time with `--accept`. The accepted rule is printed
and is written into each layer's `metadata`.

## The existing alternative

Searched on 2026-10-22 with `gh search repos` for `renderport`, `esri maplibre`, `esri mapbox style`,
`arcgis mapbox-gl`, `esri2mapbox`, `esri-to-mapbox` and `arcgis renderer`. Nothing with this name or purpose came
back. These are the nearest, and I read only their READMEs and descriptions, not their source:

- [bojko108/ol-esri-style](https://github.com/bojko108/ol-esri-style) turns an Esri style definition into an
  OpenLayers style function, in the browser, from a layer URL in one call. It also handles labels. It targets
  OpenLayers, not MapLibre. I did not check how it treats a symbol it cannot draw.
- [muimsd/esri-gl](https://github.com/muimsd/esri-gl) and
  [frontiersi/mapbox-gl-esri-sources](https://github.com/frontiersi/mapbox-gl-esri-sources) let mapbox-gl or
  maplibre-gl read Esri services as sources. They solve the data side. They are not a symbology converter.
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
| `R-MARKER-OUTLINE` | accept | MapLibre draws a circle stroke outside `circle-radius`. The Esri reference does not say whether the size includes the outline. |
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
| `R-SCALE-SYMBOLS` | no | `scaleSymbols: true` means symbols scale with the map. |
| `R-SCALE-RANGE` | accept | The reference does not define the direction of `minScale` and `maxScale` or the meaning of 0. |
| `R-LAYER-TRANSPARENCY` | accept | MapLibre alpha is per feature. The reference does not say how layer transparency composites. |

What does convert without a rule: a circle marker with no outline, a solid line or solid fill (the line rules
above still apply), and a null line, null fill or null outline, which draw nothing in both systems. A unique
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
| `drawingInfo.transparency` runs from 0 (none) to 100 (fully transparent). `scaleSymbols` says whether symbols keep the same screen size. | [drawingInfo](https://developers.arcgis.com/web-map-specification/objects/drawingInfo/) |
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
221 assertions, 0 failed
```

Windows Python 3.13.2 and Linux Python 3.12.3 print the same 221 assertions, and the full output of the two
hosts is byte for byte identical (compared with `diff`). Branch coverage is 100 percent:
`coverage run --branch renderport.py --self-test` reports 881 statements, 238 branches, 0 missed. The only line
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
| The tool does not crash on malformed JSON. | Not in the self-test. A scratch fuzz run mutated four fixture layers 8000 times each, with and without every acceptable rule (32000 conversions), and raised nothing. |
| The self-test can fail. | A scratch run changed one line at a time in 17 places (unit factor, radius, `allow_abbrev`, the exclusive-create write, the URL scheme test, a class bound, the error-body test, integer parsing, duplicate detection, the typed `match`, labels, the zoom constant, the offset test, the exit code, a null guard, the size test, the `--apply` guard). All 17 failed the self-test. |

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
  An absent `scaleSymbols` is read as fixed screen size.
- Field names match exactly, including case.
- `definitionExpression`, time settings and other layer-level keys outside `drawingInfo` are not read.
- A secured service needs a saved copy: the tool has no credential flag. Save the JSON and use `--layer-json`.
- The conversion constant for scale assumes 96 dpi and the standard web mercator scheme. Zoom is clamped to 0
  to 24. MapLibre hides a layer at `maxzoom` itself, so a layer is hidden one step early at that exact zoom.
- Python 3.9 was not run. The file was parsed with `ast` using `feature_version=(3, 9)` and uses no later
  syntax, but the self-test ran on 3.12 and 3.13 only.
- Marker `angle` is ignored because a circle looks the same turned. Rotation expressions are refused.

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
