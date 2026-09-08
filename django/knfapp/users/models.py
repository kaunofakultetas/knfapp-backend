############################################################
#  [*] users — accounts, invitation codes, bearer sessions
#
#  Table and column names match the production database
#  byte-for-byte (db_table/db_column), so the data cutover
#  is a row copy, never a rename. Three shape rules every
#  app's models follow:
#
#    - TEXT primary keys stay TextField(primary_key=True) —
#      every id is a uuid4 string minted in Python
#    - timestamps stay TEXT (ISO-8601 with offset, the
#      shape common/timestamps.py writes); Django DateTime
#      columns would rewrite every stored stamp
#    - the CHECK constraints (role, source, ...) live in
#      Meta.constraints so the database itself enforces the
#      enums
#
#  sessions.token stores the sha256 HEX of the bearer, never
#  the raw token — a DB or backup leak yields nothing usable
#  (users/auth.py owns the hashing).
############################################################


from django.db import models


ROLES = ("student", "teacher", "admin", "curator")
PRIVILEGED_ROLES = ("admin", "curator")


class User(models.Model):
    id = models.TextField(primary_key=True)
    username = models.TextField(unique=True)
    email = models.TextField(unique=True)
    display_name = models.TextField()
    password_hash = models.TextField()
    role = models.TextField(default="student")
    invited = models.IntegerField(default=0)
    avatar_url = models.TextField(null=True, blank=True)
    student_number = models.TextField(null=True, blank=True)
    study_group = models.TextField(null=True, blank=True)
    study_program = models.TextField(null=True, blank=True)
    active = models.IntegerField(default=1)
    chat_push_preview = models.IntegerField(default=1)
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "users"
        constraints = [
            models.CheckConstraint(condition=models.Q(role__in=ROLES), name="users_role_check"),
        ]


class InvitationCode(models.Model):
    id = models.TextField(primary_key=True)
    code = models.TextField(unique=True)
    role = models.TextField(default="student")
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        db_column="created_by", related_name="created_invitation_codes",
    )
    max_uses = models.IntegerField(default=1)
    use_count = models.IntegerField(default=0)
    expires_at = models.TextField()
    created_at = models.TextField()

    class Meta:
        db_table = "invitation_codes"
        constraints = [
            models.CheckConstraint(condition=models.Q(role__in=ROLES), name="invitation_codes_role_check"),
        ]


class Session(models.Model):
    id = models.TextField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id", related_name="sessions")
    token = models.TextField(unique=True)
    created_at = models.TextField()
    expires_at = models.TextField()

    class Meta:
        db_table = "sessions"
