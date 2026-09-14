"""Exam-time face verification against the registration face embedding.

Uses ml/face_id (InsightFace/ArcFace). The reference embedding is captured by
the live face scan at registration and stored in
``StudentProfile.face_embedding``. Accounts that predate the scan — or that lost
their profile row — are lazily backfilled from the registration photo or ID card
upload on the first check.

Every result carries an ``outcome`` so callers can tell a real identity decision
apart from a system fault:

``match``        the face matches the reference
``no_match``     a face was compared to a reference and did not match
``no_face``      no face could be read from the webcam frame
``unavailable``  no comparison was possible (no reference, model missing, error)

Only ``match`` and ``no_match`` are identity decisions. The other two mean the
check has not happened yet and must never, on their own, end an exam.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MATCH = "match"
NO_MATCH = "no_match"
NO_FACE = "no_face"
UNAVAILABLE = "unavailable"

CONCLUSIVE_OUTCOMES = (MATCH, NO_MATCH)


@dataclass
class ExamVerifyResult:
    passed: bool
    id_confidence: float
    face_match_score: float
    errors: list[str] = field(default_factory=list)
    outcome: str = UNAVAILABLE

    @property
    def is_conclusive(self) -> bool:
        """True when a face was actually compared against a reference."""
        return self.outcome in CONCLUSIVE_OUTCOMES


def _unavailable(reason: str) -> ExamVerifyResult:
    return ExamVerifyResult(
        passed=False,
        id_confidence=0.0,
        face_match_score=0.0,
        errors=[reason],
        outcome=UNAVAILABLE,
    )


def reference_photo_bytes(student_profile, user) -> bytes | None:
    """Best available face reference image: profile photo, then ID card scan."""
    candidates = [getattr(user, "profile_photo", None)]
    if student_profile is not None:
        candidates.append(getattr(student_profile, "id_proof_image", None))
    for image_field in candidates:
        if not image_field:
            continue
        try:
            with image_field.open("rb") as handle:
                return handle.read()
        except (OSError, ValueError):
            logger.warning("Reference image %s could not be read.", image_field, exc_info=True)
    return None


def _reference_embedding(student_profile, user) -> list[float] | None:
    """Stored embedding, or a lazy backfill from the reference photo.

    Works for users whose ``StudentProfile`` is missing entirely: the
    registration scan lives on the user, so the reference can still be rebuilt.
    """
    from ml.face_id.service import embed_face

    stored = getattr(student_profile, "face_embedding", None)
    if stored:
        return stored

    photo_bytes = reference_photo_bytes(student_profile, user)
    if photo_bytes is None:
        return None

    embedding = embed_face(photo_bytes)
    if embedding is None:
        return None

    if student_profile is not None:
        student_profile.face_embedding = embedding
        student_profile.save(update_fields=["face_embedding"])
    return embedding


def verify_exam_id_frame(frame_bytes, *, student_profile=None, user=None) -> ExamVerifyResult:
    """Verify the webcam frame face matches the registration face embedding.

    ``student_profile`` may be ``None`` (deleted or never created) as long as
    ``user`` is given, so a missing profile row degrades to "cannot verify"
    rather than to a false identity failure.
    """
    if user is None:
        user = getattr(student_profile, "user", None)
    if user is None:
        return _unavailable("No account on file for this attempt.")

    try:
        from django.conf import settings

        from ml.face_id.service import embed_face, face_id_available, match_score

        if not face_id_available():
            return _unavailable("Face ID model unavailable.")

        if not frame_bytes:
            return ExamVerifyResult(
                passed=False,
                id_confidence=0.0,
                face_match_score=0.0,
                errors=["Webcam frame could not be decoded."],
                outcome=NO_FACE,
            )

        reference = _reference_embedding(student_profile, user)
        if reference is None:
            if reference_photo_bytes(student_profile, user) is None:
                return _unavailable("No face scan or photo on file for this student.")
            return _unavailable("Could not read a face from the registration photo.")

        frame_embedding = embed_face(frame_bytes)
        if frame_embedding is None:
            return ExamVerifyResult(
                passed=False,
                id_confidence=0.0,
                face_match_score=0.0,
                errors=["No face detected in webcam frame."],
                outcome=NO_FACE,
            )

        threshold = settings.PROCTORING.get("FACE_ID_MATCH_THRESHOLD", 0.35)
        score = match_score(reference, frame_embedding)
        passed = score >= threshold

        return ExamVerifyResult(
            passed=passed,
            id_confidence=0.9 if passed else 0.4,
            face_match_score=score,
            errors=[] if passed else [f"Face match score {score:.2f} below threshold {threshold:.2f}."],
            outcome=MATCH if passed else NO_MATCH,
        )
    except Exception as exc:  # noqa: BLE001 — verification must never crash the task
        logger.exception("Face verification raised for user %s", getattr(user, "pk", None))
        return _unavailable(f"Face verification error: {exc}")
