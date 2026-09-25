############################################################
#  [*] Wayfind SVG — the plan drawing sanitiser
#
#  An uploaded floor plan is drawn, never run, so the stored
#  file is NOT the upload: sanitize_plan parses the bytes
#  with lxml and builds a brand-new document out of an
#  explicit allowlist — SVG elements a static drawing needs
#  (shapes, text, groups, paint servers, clipping, a few
#  filter primitives) and the geometry / presentation
#  attributes they carry. Everything else never reaches the
#  output: script, foreignObject, the animation elements
#  (animate / set can write a script URL at run time),
#  metadata and editor namespaces, every on* handler however
#  it is quoted, and every attribute not on the list. <a> is
#  unwrapped into a plain <g> — the shapes stay, the link
#  goes.
#
#  Values are judged AFTER the parser decoded them, so a
#  character reference (&#106;avascript:) is judged as the
#  "javascript:" it is. An href survives only as an internal
#  "#fragment" (and, on <image>, as an inline data: raster);
#  a url(...) in a paint or clip value only as url(#id); a
#  style attribute keeps only allowlisted CSS properties with
#  inert values; a <style> element survives only when its CSS
#  holds no at-rule, no external url() and no escape.
#
#  Parsing never expands an entity, never touches the
#  network and never loads a DTD; a document whose internal
#  subset DECLARES entities is refused outright (a plan has
#  no use for one, and it is the shape of every entity bomb).
#  What will not parse is refused, never repaired: the caller
#  answers 400 with the reason.
#
#  Used by:
#    - api/views.py upload_plan — before the bytes are hashed
############################################################


import re

from lxml import etree


SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# The parser's settings: no entity expansion, no network, no
# DTD load, no comments or processing instructions kept, CDATA
# folded into text, and libxml2's default size limits
# (huge_tree off). A parser object is built per call from
# these — lxml parsers are not thread-safe, and the serving
# process runs requests on several threads
PARSER_OPTIONS = {
    "resolve_entities": False, "no_network": True, "load_dtd": False, "dtd_validation": False,
    "huge_tree": False, "remove_comments": True, "remove_pis": True, "strip_cdata": True,
}

# Elements that pass, by SVG local name. <a> is mapped to <g>
# (UNWRAP) rather than listed — its children stay, its link
# does not
ELEMENTS = frozenset({
    "svg", "g", "defs", "symbol", "use", "switch", "title", "desc",
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "textPath",
    "linearGradient", "radialGradient", "stop", "pattern",
    "clipPath", "mask", "marker", "image", "style",
    "filter", "feGaussianBlur", "feOffset", "feFlood", "feComposite", "feMerge", "feMergeNode",
    "feBlend", "feColorMatrix", "feDropShadow", "feMorphology", "feComponentTransfer",
    "feFuncR", "feFuncG", "feFuncB", "feFuncA",
})
UNWRAP = {"a": "g"}

# Attributes that pass on any allowed element (no namespace).
# Geometry, presentation, text layout, paint-server and
# filter-primitive parameters — nothing that runs, loads or
# navigates. data-* passes too (see _keep_attribute)
ATTRIBUTES = frozenset({
    "id", "class", "style", "transform", "version", "baseProfile", "viewBox", "preserveAspectRatio",
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "fx", "fy", "fr",
    "width", "height", "d", "points", "pathLength",
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "opacity", "color",
    "display", "visibility", "overflow", "paint-order", "vector-effect", "shape-rendering", "text-rendering",
    "image-rendering", "mix-blend-mode", "isolation", "enable-background",
    "font-family", "font-size", "font-weight", "font-style", "font-variant", "font-stretch",
    "text-anchor", "dominant-baseline", "alignment-baseline", "baseline-shift", "letter-spacing",
    "word-spacing", "text-decoration", "writing-mode", "direction", "dx", "dy", "rotate", "textLength",
    "lengthAdjust", "startOffset", "method", "spacing", "side",
    "clip-path", "clip-rule", "clipPathUnits", "mask", "maskUnits", "maskContentUnits",
    "marker-start", "marker-mid", "marker-end", "markerWidth", "markerHeight", "markerUnits",
    "refX", "refY", "orient", "gradientUnits", "gradientTransform", "spreadMethod", "offset",
    "stop-color", "stop-opacity", "patternUnits", "patternContentUnits", "patternTransform",
    "filter", "filterUnits", "primitiveUnits", "in", "in2", "result", "stdDeviation", "edgeMode",
    "flood-color", "flood-opacity", "operator", "k1", "k2", "k3", "k4", "mode", "values", "type",
    "radius", "tableValues", "slope", "intercept", "amplitude", "exponent", "color-interpolation",
    "color-interpolation-filters", "lighting-color", "systemLanguage", "requiredFeatures",
})

# The presentation attributes whose value may carry url(...)
# — a paint server, a clip, a mask, a marker or a filter,
# always by internal fragment
URL_ATTRIBUTES = frozenset({"fill", "stroke", "clip-path", "mask", "filter", "marker-start", "marker-mid", "marker-end"})

# Where an href may stand at all; every one of them is held
# to an internal #fragment, <image> may also inline a raster
HREF_ELEMENTS = frozenset({"use", "textPath", "linearGradient", "radialGradient", "pattern", "image", "filter"})

# The CSS properties a style attribute may keep
STYLE_PROPERTIES = frozenset({
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "opacity", "color",
    "display", "visibility", "overflow", "paint-order", "vector-effect", "shape-rendering", "text-rendering",
    "image-rendering", "mix-blend-mode", "isolation", "enable-background",
    "font", "font-family", "font-size", "font-weight", "font-style", "font-variant", "font-stretch",
    "line-height", "text-anchor", "text-align", "dominant-baseline", "alignment-baseline", "baseline-shift",
    "letter-spacing", "word-spacing", "text-decoration", "writing-mode", "direction",
    "clip-path", "clip-rule", "mask", "marker", "marker-start", "marker-mid", "marker-end",
    "stop-color", "stop-opacity", "flood-color", "flood-opacity", "lighting-color", "filter",
    "color-interpolation", "color-interpolation-filters", "solid-color", "solid-opacity",
})

# Anything that can make CSS run, load or escape: an escape
# sequence (the way round every other check), the IE-era
# script hooks, markup, and any at-rule (@import, @font-face)
CSS_HAZARD_RE = re.compile(r"\\|expression|javascript|vbscript|behavior|binding|[<>@]", re.IGNORECASE)

# Every url( in a value, and the one shape allowed for it
CSS_URL_RE = re.compile(r"url\s*\(", re.IGNORECASE)
LOCAL_URL_RE = re.compile(r"url\s*\(\s*(['\"]?)#[^'\"()\s]*\1\s*\)", re.IGNORECASE)

# An internal reference, and an inline raster for <image>
FRAGMENT_RE = re.compile(r"^#[^\s]*\Z")
DATA_IMAGE_RE = re.compile(r"^data:image/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=]*\Z", re.IGNORECASE)

# The C0 controls and whitespace a scheme may be padded with
# before a browser reads it
PADDING_RE = re.compile(r"[\x00-\x20\x7f]+")








############################################################
# PlanRefused
############################################################
#
# A plan that cannot be made safe by rebuilding it — it will
# not parse, its root is not <svg>, or it declares entities.
# The message is the reason the route answers with.
#
# Used by:
#   - sanitize_plan (below) — raised
#   - api/views.py upload_plan — caught, answered 400
############################################################

class PlanRefused(ValueError):
    pass








############################################################
# sanitize_plan
############################################################
#
# The uploaded bytes → the sanitised SVG text to store, or
# PlanRefused. The output is a fresh document in the SVG
# namespace (a drawing written without an xmlns is taken as
# SVG and comes out with one), serialised deterministically,
# so the same drawing always hashes to the same name.
#
# Used by:
#   - api/views.py upload_plan
############################################################

def sanitize_plan(raw: bytes) -> str:
    # STEP 1: parse without trusting anything — entities stay
    # unexpanded, and a subset that declares any is refused
    # =====================================================
    try:
        root = etree.fromstring(raw, etree.XMLParser(**PARSER_OPTIONS))
    except (etree.XMLSyntaxError, ValueError) as error:
        raise PlanRefused(f"Plan must be a well-formed SVG drawing ({error})") from None
    dtd = root.getroottree().docinfo.internalDTD
    if dtd is not None and any(True for _ in dtd.iterentities()):
        raise PlanRefused("Plan must not declare XML entities")
    namespace, local = _split(root.tag)
    if local != "svg" or namespace not in (SVG_NS, None):
        raise PlanRefused("Plan must be an SVG drawing")


    # STEP 2: rebuild from the allowlist — a no-namespace root
    # makes its no-namespace descendants SVG too
    # =======================================================
    bare = namespace is None
    clean = etree.Element(f"{{{SVG_NS}}}svg", nsmap={None: SVG_NS, "xlink": XLINK_NS})
    _copy_attributes(root, clean, "svg")
    clean.text = root.text
    _copy_children(root, clean, bare)
    return etree.tostring(clean, encoding="unicode")








############################################################
# _split / _is_svg
############################################################
#
# An lxml tag as (namespace, local name) — (None, name) for a
# tag in no namespace; comments and entity nodes (whose tag is
# not a string) answer (None, None) — and whether a node is an
# SVG element under the document's namespace rule.
#
# Used by:
#   - sanitize_plan / _copy_children / _copy_attributes
############################################################

def _split(tag):
    if not isinstance(tag, str):
        return None, None
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return None, tag


def _is_svg(namespace, bare):
    return namespace == SVG_NS or (bare and namespace is None)








############################################################
# _copy_children
############################################################
#
# Walks one element's children into the rebuilt parent: an
# allowed SVG element is copied (attributes judged, text
# kept, children recursed), an <a> becomes a <g>, a <style>
# is kept only when its CSS is inert, and anything else is
# dropped WITH its subtree — its tail text is handed to the
# previous kept sibling (or the parent) so a dropped node
# never swallows the words after it.
#
# Used by:
#   - sanitize_plan (above), recursively
############################################################

def _copy_children(source, target, bare):
    last = None
    for child in source:
        namespace, local = _split(child.tag)
        kept = None
        if local is not None and _is_svg(namespace, bare):
            name = UNWRAP.get(local, local)
            if name in ELEMENTS and (name != "style" or _css_is_inert(child.text or "")):
                kept = etree.SubElement(target, f"{{{SVG_NS}}}{name}")
                _copy_attributes(child, kept, name)
                kept.text = child.text
                if name != "style":
                    _copy_children(child, kept, bare)
        if kept is not None:
            kept.tail = child.tail
            last = kept
        elif child.tail:
            if last is not None:
                last.tail = (last.tail or "") + child.tail
            else:
                target.text = (target.text or "") + child.tail








############################################################
# _copy_attributes
############################################################
#
# The allowed attributes of one element, each value judged
# by what it is: an href (xlink or plain) by its target, a
# url()-bearing presentation attribute by its url()s, a style
# attribute declaration by declaration, xml:space by its two
# legal values, data-* and the rest as inert text.
#
# Used by:
#   - sanitize_plan / _copy_children (above)
############################################################

def _copy_attributes(source, target, element):
    for key, value in source.attrib.items():
        namespace, local = _split(key)
        if local is None:
            continue
        if local == "href" and namespace in (None, XLINK_NS):
            if element in HREF_ELEMENTS and _href_is_safe(value, element):
                target.set(f"{{{XLINK_NS}}}href" if namespace == XLINK_NS else "href", value.strip())
            continue
        if namespace == XML_NS:
            if local == "space" and value in ("default", "preserve"):
                target.set(key, value)
            continue
        if namespace is not None:
            continue
        if local == "style":
            style = _clean_style(value)
            if style:
                target.set("style", style)
            continue
        if local.startswith("data-") and re.match(r"^data-[A-Za-z0-9_.-]+\Z", local):
            target.set(local, value)
            continue
        if local not in ATTRIBUTES:
            continue
        if local in URL_ATTRIBUTES and not _urls_are_local(value):
            continue
        if CSS_HAZARD_RE.search(value) and local not in ("id", "class"):
            continue
        target.set(local, value)








############################################################
# _href_is_safe / _urls_are_local / _css_is_inert /
# _clean_style
############################################################
#
# The value judges. An href, stripped of the padding a
# browser forgives, must be an internal #fragment — or, on
# <image>, an inline base64 raster. A value's url()s must
# every one be url(#id). CSS is inert when it holds no
# hazard (escape, script hook, markup, at-rule) and no url()
# but url(#id). A style attribute keeps the allowlisted
# properties whose values are inert and drops the rest,
# answering "" when nothing survived.
#
# Used by:
#   - _copy_attributes / _copy_children (above)
############################################################

def _href_is_safe(value, element):
    target = PADDING_RE.sub("", value or "")
    if FRAGMENT_RE.match(target):
        return True
    return element == "image" and bool(DATA_IMAGE_RE.match(target))


def _urls_are_local(value):
    return len(CSS_URL_RE.findall(value)) == len(LOCAL_URL_RE.findall(value))


def _css_is_inert(css):
    return not CSS_HAZARD_RE.search(css) and _urls_are_local(css)


def _clean_style(value):
    kept = []
    for declaration in value.split(";"):
        prop, sep, raw = declaration.partition(":")
        prop = prop.strip().lower()
        raw = raw.strip()
        if not sep or prop not in STYLE_PROPERTIES or not raw or not _css_is_inert(raw):
            continue
        kept.append(f"{prop}:{raw}")
    return ";".join(kept)
