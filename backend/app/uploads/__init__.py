############################################################
#  [*] Uploads — package marker for the image upload module
#
#  routes.py is the whole module: authenticated image
#  uploads (5 MB, magic bytes checked, then re-encoded by
#  Pillow into a metadata-free jpg/png/gif/webp) stored under
#  UPLOAD_DIR as "<uuid4 hex>.<ext>" with an uploads row
#  naming the owner, answered as a RELATIVE /api/uploads/...
#  url, plus the public file server and the owner/admin
#  delete. Avatars, post images and chat images all come
#  through here.
#
#    POST   /api/uploads            — upload one image
#                                     (require_auth, 201)
#    GET    /api/uploads/<filename> — serve one stored file
#                                     (anonymous)
#    DELETE /api/uploads/<filename> — drop one stored file
#                                     (owner or admin)
#
#  uploads_bp is re-exported below, but create_app() imports
#  it from app.uploads.routes directly — nothing imports it
#  from the package at the moment. Importing the package
#  still pulls routes.py in (and through it app.auth.routes
#  and app.database), so it is not a free import. The two
#  cross-package entry points, delete_upload() and
#  sweep_orphan_uploads(), are imported from
#  app.uploads.routes as well.
#
#  Used by:
#    - app/__init__.py — registers the blueprint at
#      /api/uploads (via app.uploads.routes)
#    - auth/routes.py, chat/routes.py — delete_upload, behind
#      lazy imports (via app.uploads.routes)
#    - mobile services/api/uploads.ts — uploadImageApi (POST);
#      services/api/client.ts getUploadUrl builds the GET URLs
############################################################


from app.uploads.routes import uploads_bp

# The package's public name — see the header for who actually
# imports it (nobody, today)
__all__ = ["uploads_bp"]
