"""Restore student profiles and face embeddings that went missing.

Deleting a ``StudentProfile`` (or creating a student account by script) leaves
the account without the reference face the pre-exam identity check compares
against. The registration scan itself lives on the user, so both the profile row
and the embedding can be rebuilt from it.

    manage.py repair_student_profiles --dry-run
    manage.py repair_student_profiles --create-missing
    manage.py repair_student_profiles --user pyanokye --student-id 20800011
"""

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import StudentProfile, User


class Command(BaseCommand):
    help = "Recreate missing student profiles and rebuild missing face embeddings."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Limit to one username.")
        parser.add_argument(
            "--student-id",
            help="Student ID for a profile being created (requires --user).",
        )
        parser.add_argument(
            "--create-missing",
            action="store_true",
            help="Create profiles for students that have none, using a generated ID.",
        )
        parser.add_argument(
            "--refresh-embeddings",
            action="store_true",
            help="Recompute embeddings even where one is already stored.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options):
        username = options["user"]
        student_id = options["student_id"]
        create_missing = options["create_missing"] or bool(student_id)
        refresh = options["refresh_embeddings"]
        dry_run = options["dry_run"]

        if student_id and not username:
            raise CommandError("--student-id requires --user.")

        students = User.objects.filter(role=User.Role.STUDENT)
        if username:
            students = students.filter(username=username)
            if not students.exists():
                raise CommandError(f"No student account named {username!r}.")

        created = embedded = skipped = 0
        for user in students.order_by("pk"):
            profile = getattr(user, "student_profile", None)

            if profile is None:
                if not create_missing:
                    self.stdout.write(
                        f"  {user.username}: no profile (re-run with --create-missing)"
                    )
                    skipped += 1
                    continue
                number = student_id or f"AUTO-{user.pk}"
                if StudentProfile.objects.filter(student_id_number=number).exists():
                    self.stderr.write(f"  {user.username}: student ID {number} already in use")
                    skipped += 1
                    continue
                self.stdout.write(f"  {user.username}: creating profile with ID {number}")
                if not dry_run:
                    profile = StudentProfile.objects.create(
                        user=user, student_id_number=number
                    )
                created += 1
                if dry_run:
                    continue

            if profile.face_embedding and not refresh:
                continue

            embedding = self._embed_reference(profile, user)
            if embedding is None:
                self.stderr.write(
                    f"  {user.username}: no face could be read from the account photo"
                )
                skipped += 1
                continue
            self.stdout.write(f"  {user.username}: rebuilt face embedding")
            if not dry_run:
                profile.face_embedding = embedding
                profile.save(update_fields=["face_embedding"])
            embedded += 1

        verb = "Would repair" if dry_run else "Repaired"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb}: {created} profile(s) created, {embedded} embedding(s) rebuilt, "
                f"{skipped} skipped."
            )
        )

    def _embed_reference(self, profile, user):
        from ml.face_id.service import embed_face, face_id_available
        from ml.id_verification.exam_verify import reference_photo_bytes

        if not face_id_available():
            raise CommandError(
                "The face ID model is unavailable — install insightface/onnxruntime first."
            )
        photo_bytes = reference_photo_bytes(profile, user)
        if photo_bytes is None:
            return None
        return embed_face(photo_bytes)
