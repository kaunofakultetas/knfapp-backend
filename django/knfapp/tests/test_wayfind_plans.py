############################################################
#  [*] Regression tests — wayfind plan drawings (KNF-022)
#
#  The SVG plan store keeps a REBUILT drawing, never the
#  upload: every bypass the old regex sanitiser let through
#  (a script spliced out of a decoy, a character-referenced
#  javascript: href, an unquoted handler, animate / set
#  writing a script URL, a foreignObject iframe, CSS that
#  loads or escapes) is either rebuilt away or refused 400
#  bad_plan before anything is stored; a real editor export
#  (classes, a <style> block, gradients, <use> by fragment,
#  an inline raster, data-name layers, text with tails)
#  survives with its drawing intact; and a served plan
#  answers the sandbox CSP and nosniff, a panorama nosniff.
#  sanitize_plan is also driven directly for the unit-level
#  rules.
############################################################


import json


from knfapp.wayfind.models import WfPlan
from knfapp.wayfind.svg import PlanRefused, sanitize_plan
from .utils import bearer
from .test_wayfind_captures import CaptureTestCase, _file, frame_bytes


# Every payload the audit (and its verifiers) stored through
# the regex sanitiser — each must come back inert
BYPASSES = [
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
    b'<a xlink:href="&#106;avascript:alert(1)"><text y="20">click</text></a></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="xlink:href" to="javascript:alert(1)"/>'
    b'<set attributeName="href" to="javascript:alert(2)"/><rect width="5" height="5"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject width="10" height="10">'
    b'<iframe xmlns="http://www.w3.org/1999/xhtml" src="javascript:alert(1)"/></foreignObject></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect onclick="steal()" ONLOAD=\'x()\' width="1" height="1"'
    b' style="fill:red;background:url(javascript:alert(1));stroke:\\75rl(x)" fill="url(http://evil/p)"/>'
    b'<style>@import url(https://evil/x.css);</style><script>alert(1)</script></svg>',
]

# What must never be in a served plan, whatever went in
FORBIDDEN = ("script", "javascript", "onclick", "onload", "animate", "<set", "foreignobject", "iframe", "@import", "evil")

# An editor export in miniature — what a plan really looks
# like and what must survive the rebuild
REAL_DRAWING = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
    '<!-- Generator: an editor -->'
    '<svg version="1.1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'
    ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" viewBox="0 0 1000 600">'
    '<style type="text/css">.st0{fill:#FFFFFF;stroke:#000000;}</style>'
    '<defs><linearGradient id="g1"><stop offset="0" stop-color="#eee"/></linearGradient>'
    '<rect id="door" width="10" height="4"/></defs>'
    '<g id="layer1" inkscape:label="Pirmas" data-name="Aukštas 1">'
    '<rect class="st0" x="10" y="10" width="200" height="100" fill="url(#g1)"/>'
    '<path d="M0 0 L100 0" style="fill:none;stroke:#333;stroke-width:2"/>'
    '<use xlink:href="#door" x="50" y="50"/>'
    '<image xlink:href="data:image/png;base64,iVBORw0KGgo=" width="4" height="4"/>'
    '<text x="20" y="40" font-size="12">Auditorija <tspan font-weight="bold">101</tspan> ir 102</text>'
    '</g></svg>'
).encode()








############################################################
# PlanSanitiserRouteTests
############################################################
#
# The upload → serve round trip through the real routes.
############################################################

class PlanSanitiserRouteTests(CaptureTestCase):

    def _upload(self, raw):
        return bearer(self.client.post, "/api/wayfind/buildings/b1/plans", self.token,
                      data={"file": _file(raw, "plan.svg")})

    def _served_text(self, response):
        self.assertEqual(response.status_code, 201, response.content)
        served = self.client.get(json.loads(response.content)["url"])
        self.assertEqual(served.status_code, 200)
        return b"".join(served.streaming_content).decode("utf-8")

    def test_every_known_bypass_is_rebuilt_inert(self):
        for raw in BYPASSES:
            text = self._served_text(self._upload(raw)).lower()
            for needle in FORBIDDEN:
                self.assertNotIn(needle, text, (needle, text))
        # The fragment-only paint survived beside the refused one
        text = self._served_text(self._upload(
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(#ok)" stroke="url(http://x/y)"/></svg>'))
        self.assertIn('fill="url(#ok)"', text)
        self.assertNotIn("stroke=", text)

    def test_unparseable_non_svg_and_entity_plans_are_refused_and_nothing_is_stored(self):
        refused = [
            # The decoy-split script: not XML at all
            b'<svg xmlns="http://www.w3.org/2000/svg"><scr<script></script>ipt>alert(1)</script></svg>',
            # An unquoted handler: not XML either
            b'<svg onload=alert(1)></svg>',
            # A document whose root is not <svg>
            b'<html><body><svg xmlns="http://www.w3.org/2000/svg"/></body></html>',
            # Entity declarations — the shape of every entity bomb
            b'<!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
            b'<svg xmlns="http://www.w3.org/2000/svg"><text>&b;</text></svg>',
            b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            b'<svg xmlns="http://www.w3.org/2000/svg"><text>&x;</text></svg>',
        ]
        for raw in refused:
            response = self._upload(raw)
            self.assertEqual(response.status_code, 400, (raw, response.content))
            self.assertEqual(json.loads(response.content)["code"], "bad_plan")
        self.assertEqual(WfPlan.objects.count(), 0)

    def test_a_real_editor_export_keeps_its_drawing(self):
        text = self._served_text(self._upload(REAL_DRAWING))
        for kept in ('viewBox="0 0 1000 600"', ".st0{fill:#FFFFFF;stroke:#000000;}", 'class="st0"',
                     'fill="url(#g1)"', 'stop-color="#eee"', 'xlink:href="#door"',
                     'xlink:href="data:image/png;base64,iVBORw0KGgo="', 'data-name="Aukštas 1"',
                     "style=\"fill:none;stroke:#333;stroke-width:2\"", "Auditorija <tspan", "101</tspan> ir 102"):
            self.assertIn(kept, text)
        # The editor's own namespace, the comment and the DOCTYPE
        # are not part of the drawing
        for gone in ("inkscape", "Generator", "DOCTYPE"):
            self.assertNotIn(gone, text)

    def test_the_same_drawing_hashes_to_the_same_name(self):
        first = json.loads(self._upload(REAL_DRAWING).content)
        second = json.loads(self._upload(REAL_DRAWING).content)
        self.assertEqual(first["url"], second["url"])
        self.assertEqual(WfPlan.objects.count(), 1)

    def test_a_plan_is_served_sandboxed_and_a_panorama_unsniffable(self):
        response = self._upload(b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>')
        served = self.client.get(json.loads(response.content)["url"])
        self.assertEqual(served["X-Content-Type-Options"], "nosniff")
        policy = served["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("sandbox", policy)

        pano = bearer(self.client.post, "/api/wayfind/buildings/b1/panoramas", self.token,
                      data={"file": _file(frame_bytes(0), "p.jpg")})
        self.assertEqual(pano.status_code, 201, pano.content)
        served = self.client.get(json.loads(pano.content)["url"])
        self.assertEqual(served["X-Content-Type-Options"], "nosniff")

    def test_a_served_plan_or_panorama_runs_no_sql(self):
        # KNF-135: the two picture routes never touch the
        # database, so they run outside ATOMIC_REQUESTS — inside
        # the test's transaction an atomic view would still show
        # SAVEPOINT/RELEASE pairs
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        plan = json.loads(self._upload(b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>').content)["url"]
        pano = json.loads(bearer(self.client.post, "/api/wayfind/buildings/b1/panoramas", self.token,
                                 data={"file": _file(frame_bytes(0), "p.jpg")}).content)["url"]
        for url in (plan, pano):
            with CaptureQueriesContext(connection) as ctx:
                served = self.client.get(url)
                b"".join(served.streaming_content)
            self.assertEqual(served.status_code, 200, url)
            self.assertEqual([q["sql"] for q in ctx.captured_queries], [], url)








############################################################
# SanitizePlanUnitTests
############################################################
#
# The rules sanitize_plan applies, one at a time.
############################################################

class SanitizePlanUnitTests(CaptureTestCase):

    def test_a_drawing_without_a_namespace_comes_out_as_svg(self):
        out = sanitize_plan(b'<svg viewBox="0 0 10 10"><rect width="1" height="1"/></svg>')
        self.assertTrue(out.startswith('<svg xmlns="http://www.w3.org/2000/svg"'), out)
        self.assertIn("<rect", out)

    def test_a_link_is_unwrapped_and_a_dropped_node_keeps_the_words_after_it(self):
        out = sanitize_plan(b'<svg xmlns="http://www.w3.org/2000/svg"><a href="https://x"><circle r="2"/></a>'
                            b'<text>Kab. <script>x</script>12</text></svg>')
        self.assertIn("<g><circle", out)
        self.assertNotIn("href", out)
        self.assertIn("<text>Kab. 12</text>", out)

    def test_an_href_is_judged_after_decoding_and_padding(self):
        for value in ("&#106;avascript:x", " \tjavascript:x", "java&#x09;script:x", "//evil/x.svg#a", "data:image/svg+xml;base64,AA=="):
            out = sanitize_plan(f'<svg xmlns="http://www.w3.org/2000/svg"><use href="{value}"/><image href="{value}"/></svg>'.encode())
            self.assertNotIn("href", out, value)

    def test_style_keeps_inert_presentation_only(self):
        out = sanitize_plan(b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="FILL: #fff ; behavior:url(x.htc);'
                            b' stroke:u\\72l(x); font-family:Arial; position:fixed"/></svg>')
        self.assertIn('style="fill:#fff;font-family:Arial"', out)

    def test_refusals_carry_their_reason(self):
        with self.assertRaises(PlanRefused):
            sanitize_plan(b"not xml")
        with self.assertRaisesRegex(PlanRefused, "entities"):
            sanitize_plan(b'<!DOCTYPE svg [<!ENTITY a "a">]><svg xmlns="http://www.w3.org/2000/svg"/>')
