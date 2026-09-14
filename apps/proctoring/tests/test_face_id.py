"""Tests for the InsightFace identity verification layer (ml/face_id).

Runs without insightface installed: the analyzer and embed/match functions
are mocked at module level. Covers cosine matching, face-scan decoding,
registration form validation, and the exam-time verify + lazy backfill.
"""

from __future__ import annotations

import base64
import shutil
import tempfile
from io import BytesIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from ml.face_id.service import match_score
from ml.id_verification.exam_verify import NO_FACE, NO_MATCH, UNAVAILABLE


def _jpeg_bytes(color=(120, 100, 90), size=(64, 64)) -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _data_url(raw: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode()


class MatchScoreTests(SimpleTestCase):
    def test_identical_embeddings_score_one(self):
        emb = [0.6, 0.8, 0.0]
        self.assertAlmostEqual(match_score(emb, emb), 1.0, places=5)

    def test_orthogonal_embeddings_score_zero(self):
        self.assertAlmostEqual(match_score([1.0, 0.0], [0.0, 1.0]), 0.0, places=5)

    def test_zero_vector_scores_zero(self):
        self.assertEqual(match_score([0.0, 0.0], [1.0, 0.0]), 0.0)

    def test_unnormalized_inputs_are_renormalized(self):
        self.assertAlmostEqual(match_score([2.0, 0.0], [5.0, 0.0]), 1.0, places=5)


class EmbedFaceTests(SimpleTestCase):
    def _face(self, bbox, embedding):
        face = mock.Mock()
        face.bbox = bbox
        face.normed_embedding = embedding
        return face

    def test_returns_none_when_model_unavailable(self):
        from ml.face_id import service

        with mock.patch.object(service, "get_face_analyzer", return_value=None):
            self.assertIsNone(service.embed_face(_jpeg_bytes()))

    def test_returns_none_when_no_face_found(self):
        from ml.face_id import service

        analyzer = mock.Mock()
        analyzer.get.return_value = []
        with mock.patch.object(service, "get_face_analyzer", return_value=analyzer):
            self.assertIsNone(service.embed_face(_jpeg_bytes()))

    def test_returns_none_on_undecodable_image(self):
        from ml.face_id import service

        analyzer = mock.Mock()
        with mock.patch.object(service, "get_face_analyzer", return_value=analyzer):
            self.assertIsNone(service.embed_face(b"not-an-image"))
        analyzer.get.assert_not_called()

    def test_picks_largest_face(self):
        from ml.face_id import service

        small = self._face([0, 0, 10, 10], [0.0, 1.0])
        large = self._face([0, 0, 50, 50], [1.0, 0.0])
        analyzer = mock.Mock()
        analyzer.get.return_value = [small, large]
        with mock.patch.object(service, "get_face_analyzer", return_value=analyzer):
            self.assertEqual(service.embed_face(_jpeg_bytes()), [1.0, 0.0])


class DecodeFaceScanTests(SimpleTestCase):
    def test_decodes_data_url(self):
        from apps.accounts.forms import decode_face_scan

        raw = _jpeg_bytes()
        self.assertEqual(decode_face_scan(_data_url(raw)), raw)

    def test_decodes_bare_base64(self):
        from apps.accounts.forms import decode_face_scan

        raw = _jpeg_bytes()
        self.assertEqual(decode_face_scan(base64.b64encode(raw).decode()), raw)

    def test_rejects_invalid_base64(self):
        from apps.accounts.forms import decode_face_scan

        with self.assertRaises(ValidationError):
            decode_face_scan("data:image/jpeg;base64,@@not-base64@@")

    def test_rejects_empty_payload(self):
        from apps.accounts.forms import decode_face_scan

        with self.assertRaises(ValidationError):
            decode_face_scan("data:image/jpeg;base64,")

    def test_rejects_oversized_payload(self):
        from apps.accounts import forms as accounts_forms

        raw = b"x" * (accounts_forms.FACE_SCAN_MAX_BYTES + 1)
        with self.assertRaises(ValidationError):
            accounts_forms.decode_face_scan(base64.b64encode(raw).decode())


_MEDIA_ROOT = tempfile.mkdtemp(prefix="face_id_test_media_")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class StudentRegistrationFaceScanTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def _form(self, face_scan=None):
        from apps.accounts.forms import StudentRegistrationForm

        data = {
            "username": "kwame",
            "email": "kwame@example.com",
            "first_name": "Kwame",
            "last_name": "Mensah",
            "student_id_number": "20812345",
            "programme": "BSc Computer Science",
            "face_scan_data": face_scan if face_scan is not None else _data_url(_jpeg_bytes()),
            "password1": "s3cure-Pass-word!",
            "password2": "s3cure-Pass-word!",
        }
        files = {
            "id_proof_image": SimpleUploadedFile(
                "id_card.jpg", _jpeg_bytes(color=(20, 40, 60)), content_type="image/jpeg"
            ),
        }
        return StudentRegistrationForm(data, files)

    def _mock_ocr(self):
        ocr_result = mock.Mock(
            is_valid=True, matched_fields={}, ocr_text="", errors=[], warnings=[]
        )
        return mock.patch(
            "apps.accounts.forms.verify_knust_student_id", return_value=ocr_result
        )

    def test_missing_scan_is_required(self):
        form = self._form(face_scan="")
        with self._mock_ocr():
            self.assertFalse(form.is_valid())
        self.assertIn("face_scan_data", form.errors)

    def test_no_face_detected_hard_fails(self):
        form = self._form()
        with self._mock_ocr(), mock.patch(
            "ml.face_id.service.face_id_available", return_value=True
        ), mock.patch("ml.face_id.service.embed_face", return_value=None):
            self.assertFalse(form.is_valid())
        self.assertIn("face_scan_data", form.errors)
        self.assertIn("No face detected", form.errors["face_scan_data"][0])

    def test_valid_scan_stores_photo_and_embedding(self):
        embedding = [0.1] * 512
        form = self._form()
        with self._mock_ocr(), mock.patch(
            "ml.face_id.service.face_id_available", return_value=True
        ), mock.patch("ml.face_id.service.embed_face", return_value=embedding):
            self.assertTrue(form.is_valid(), form.errors)
            user = form.save()

        profile = user.student_profile
        self.assertEqual(profile.face_embedding, embedding)
        self.assertTrue(user.profile_photo)
        with user.profile_photo.open("rb") as handle:
            self.assertEqual(handle.read(), _jpeg_bytes())

    def test_model_unavailable_soft_fails_to_empty_embedding(self):
        form = self._form()
        with self._mock_ocr(), mock.patch(
            "ml.face_id.service.face_id_available", return_value=False
        ):
            self.assertTrue(form.is_valid(), form.errors)
            user = form.save()

        self.assertEqual(user.student_profile.face_embedding, [])
        self.assertTrue(user.profile_photo)  # photo still stored for lazy backfill


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class ExamVerifyTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        from apps.accounts.models import StudentProfile, User

        self.user = User.objects.create_user(
            username="ama",
            email="ama@example.com",
            password="pass-Word-123",
            role=User.Role.STUDENT,
        )
        self.profile = StudentProfile.objects.create(
            user=self.user, student_id_number="20867890"
        )

    def _verify(self, frame=b"frame", *, available=True, frame_embedding=None, score=None):
        from ml.id_verification.exam_verify import verify_exam_id_frame

        patches = [
            mock.patch("ml.face_id.service.face_id_available", return_value=available),
            mock.patch("ml.face_id.service.embed_face", return_value=frame_embedding),
        ]
        if score is not None:
            patches.append(mock.patch("ml.face_id.service.match_score", return_value=score))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return verify_exam_id_frame(frame, student_profile=self.profile)

    # Whether an unverifiable student is admitted is a policy decision made in
    # apps/proctoring/tasks.py, where it can be counted and flagged. This layer
    # only reports that no comparison was possible.
    def test_model_unavailable_reports_unavailable(self):
        result = self._verify(available=False)
        self.assertEqual(result.outcome, UNAVAILABLE)
        self.assertFalse(result.is_conclusive)
        self.assertIn("unavailable", result.errors[0])

    def test_no_reference_on_file_reports_unavailable(self):
        result = self._verify(frame_embedding=[1.0, 0.0])
        self.assertEqual(result.outcome, UNAVAILABLE)
        self.assertFalse(result.is_conclusive)
        self.assertIn("No face scan or photo on file", result.errors[0])

    def test_stored_embedding_match_passes(self):
        self.profile.face_embedding = [1.0, 0.0]
        self.profile.save()
        result = self._verify(frame_embedding=[1.0, 0.0], score=0.72)
        self.assertTrue(result.passed)
        self.assertAlmostEqual(result.face_match_score, 0.72)

    def test_stored_embedding_below_threshold_fails(self):
        self.profile.face_embedding = [1.0, 0.0]
        self.profile.save()
        result = self._verify(frame_embedding=[0.0, 1.0], score=0.12)
        self.assertFalse(result.passed)
        self.assertIn("below threshold", result.errors[0])

    def test_no_face_in_frame_fails(self):
        self.profile.face_embedding = [1.0, 0.0]
        self.profile.save()
        # embed_face returning None for the frame means no face detected.
        result = self._verify(frame_embedding=None)
        self.assertFalse(result.passed)
        self.assertEqual(result.outcome, NO_FACE)
        self.assertFalse(result.is_conclusive)
        self.assertIn("No face detected", result.errors[0])

    def test_a_mismatch_is_a_conclusive_identity_decision(self):
        self.profile.face_embedding = [1.0, 0.0]
        self.profile.save()
        result = self._verify(frame_embedding=[0.0, 1.0], score=0.12)
        self.assertEqual(result.outcome, NO_MATCH)
        self.assertTrue(result.is_conclusive)

    def test_lazy_backfill_from_profile_photo(self):
        from django.core.files.base import ContentFile

        self.user.profile_photo.save("ref.jpg", ContentFile(_jpeg_bytes()), save=True)
        self.assertEqual(self.profile.face_embedding, [])

        embedding = [0.5] * 4
        with mock.patch(
            "ml.face_id.service.face_id_available", return_value=True
        ), mock.patch(
            "ml.face_id.service.embed_face", return_value=embedding
        ), mock.patch(
            "ml.face_id.service.match_score", return_value=0.9
        ):
            from ml.id_verification.exam_verify import verify_exam_id_frame

            result = verify_exam_id_frame(b"frame", student_profile=self.profile)

        self.assertTrue(result.passed)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.face_embedding, embedding)

    def test_reference_photo_without_face_fails(self):
        from django.core.files.base import ContentFile

        self.user.profile_photo.save("ref.jpg", ContentFile(_jpeg_bytes()), save=True)

        def embed(image_bytes):
            return None  # no face found anywhere

        with mock.patch(
            "ml.face_id.service.face_id_available", return_value=True
        ), mock.patch("ml.face_id.service.embed_face", side_effect=embed):
            from ml.id_verification.exam_verify import verify_exam_id_frame

            result = verify_exam_id_frame(b"frame", student_profile=self.profile)

        self.assertFalse(result.passed)
        self.assertIn("registration photo", result.errors[0])
