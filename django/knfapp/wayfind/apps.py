############################################################
#  [*] wayfind — app registration
#
#  The indoor map: the building graph and its drafts
#  (api/views.py), guided panorama captures (api/captures.py),
#  the stitch worker (stitch.py), the content-addressed file
#  store (store.py), the document compiler and validator
#  (graph.py).
############################################################


from django.apps import AppConfig


class WayfindConfig(AppConfig):
    name = "knfapp.wayfind"
    label = "wayfind"
