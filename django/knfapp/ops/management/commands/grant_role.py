############################################################
#  [*] grant_role — the first-boot role bootstrap
#
#  manage.py grant_role <username> <role>
#
#  The one sanctioned way to the FIRST admin: registration
#  can only mint students (a privileged invitation needs an
#  existing admin to mint it), every admin route requires an
#  admin, and there is no other account backend — so an
#  empty database has no path to its first administrator
#  except this command. It goes through the model layer, so
#  the role CHECK holds, the stamp keeps the users app's
#  aware shape, and the grant lands in the audit trail like
#  every other role change.
#
#  Used by:
#    - the first-boot runbook (django/README.md, Data) — run
#      via docker exec after the owner registers in the app
############################################################


from django.core.management.base import BaseCommand, CommandError


from knfapp.admin.audit import write_audit
from knfapp.common.timestamps import utc_now
from knfapp.users.models import ROLES, User


class Command(BaseCommand):
    help = "Grant a role to an existing account (the first-boot path to the first admin)"

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("role", choices=ROLES)

    def handle(self, *args, **options):
        # Case-insensitive lookup, mirroring how login matches
        # the identifier
        user = User.objects.filter(username__iexact=options["username"]).values("id", "username", "role").first()
        if user is None:
            raise CommandError(f"No account named '{options['username']}'")

        new_role = options["role"]
        if user["role"] == new_role:
            self.stdout.write(f"{user['username']} already holds the {new_role} role — nothing to do")
            return

        User.objects.filter(id=user["id"]).update(role=new_role, updated_at=utc_now())

        # actor None: there is no signed-in admin at first boot
        # — the payload says where the grant came from
        write_audit(None, "user.role", user["id"],
                    {"from": user["role"], "to": new_role, "via": "grant_role"})

        self.stdout.write(f"{user['username']}: {user['role']} -> {new_role}")
