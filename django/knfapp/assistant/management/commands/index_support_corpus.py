############################################################
#  [*] index_support_corpus — the knowledge-base indexer
#
#  Cron's daily tick (after the scrapers, so fresh handbook
#  and news rows are already in) around the one shared sync
#  in indexing.py — the admin console's re-index button
#  runs the same function. A gateway failure aborts before
#  any delete — a broken embeddings API must never empty
#  the knowledge base.
#
#    python3 manage.py index_support_corpus          # daily
#    python3 manage.py index_support_corpus --all    # force
#    python3 manage.py index_support_corpus --allow-mass-retire
#
#  --all re-embeds every chunk regardless of hash — the
#  move after changing AI_EMBED_MODEL (rows remember the
#  model that embedded them; search only reads rows of the
#  current one). A run that would retire more than a quarter
#  of the knowledge base retires nothing and says so
#  (exit 2, "retirement BLOCKED"); --allow-mass-retire is the
#  operator's word that the shrink is real (a chunk format
#  changed, a source was withdrawn on purpose).
############################################################


from django.core.management.base import BaseCommand

from knfapp.assistant.gateway import GatewayError
from knfapp.assistant.indexing import run_index


class Command(BaseCommand):
    help = "Embed changed knowledge-base chunks and retire stale ones"

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", dest="reindex_all",
                            help="re-embed every chunk regardless of stored hashes")
        parser.add_argument("--allow-mass-retire", action="store_true", dest="allow_mass_retire",
                            help="retire even when more than a quarter of the stored rows would go")

    def handle(self, *args, **options):
        try:
            counts = run_index(reindex_all=options["reindex_all"],
                               allow_mass_retire=options["allow_mass_retire"])
        except GatewayError as exc:
            self.stderr.write(f"aborted, nothing written: {exc}")
            raise SystemExit(1)

        self.stdout.write(
            f"corpus {counts['corpus']} chunks: {counts['embedded']} embedded, "
            f"{counts['unchanged']} unchanged, {counts['retired']} retired"
        )
        if counts["retireBlocked"]:
            # Loud and non-zero, so cron's mail and the log show it
            self.stderr.write(
                f"retirement BLOCKED: {counts['retireBlocked']} rows would have gone — "
                "a source probably returned nothing; re-run with --allow-mass-retire if it is real"
            )
            raise SystemExit(2)
