############################################################
#  [*] Regression tests — the one password policy
#
#  users/auth.py validate_new_password: every rule as a
#  table, incl. the byte-vs-char boundary bcrypt forces and
#  the local-part noise threshold. One place decides what a
#  password may be — these pin that place.
############################################################


from django.test import SimpleTestCase


from knfapp.users.auth import validate_new_password


class PasswordPolicyTests(SimpleTestCase):

    def test_the_table(self):
        cases = [
            # (password, username, email, expected fragment | None)
            ("12345", "tomas", "t@x.lt", "at least 6"),
            ("123456", "tomas", "t@x.lt", "too common"),
            ("slaptazodis", "tomas", "t@x.lt", "too common"),
            ("geras-ilgas-2026", "tomas", "t@x.lt", None),
            ("xTomas-2026", "Tomas", "t@x.lt", "your username"),
            ("su-migle-2026", "tomas", "Migle@knf.vu.lt", "your email"),
            # Local parts under 3 chars are too noisy to check
            ("su-ab-2026", "tomas", "ab@knf.vu.lt", None),
            # 72 BYTES, not chars — ą is two bytes in UTF-8
            ("ą" * 36, "tomas", "t@x.lt", None),
            ("ą" * 37, "tomas", "t@x.lt", "at most 72"),
            ("x" * 72, "tomas", "t@x.lt", None),
            ("x" * 73, "tomas", "t@x.lt", "at most 72"),
        ]
        for password, username, email, expected in cases:
            error = validate_new_password(password, username, email)
            if expected is None:
                self.assertIsNone(error, password)
            else:
                self.assertIn(expected, error or "", password)
