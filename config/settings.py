import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-key-change-in-production",
)

DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() == "true"

ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    # Must precede staticfiles: it replaces `runserver` with the ASGI one, and
    # without it Django serves WSGI only and every /ws/ request 404s.
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "channels",
    "django_ratelimit",
    "apps.accounts",
    "apps.courses",
    "apps.exams",
    "apps.proctoring",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.accounts.middleware.HtmxAuthRedirectMiddleware",
    "apps.accounts.middleware.TeacherApprovalMiddleware",
    "apps.accounts.middleware.StudentIDReviewMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.portal_sidebar",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

if os.getenv("USE_POSTGRES", "").lower() == "true":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "exam_proctor"),
            "USER": os.getenv("POSTGRES_USER", "postgres"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", "postgres"),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "apps.accounts.backends.EmailOrUsernameBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CSRF_FAILURE_VIEW = "apps.accounts.views.csrf_failure"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# --- Redis-backed infra (Channels + Celery) ---
# In dev (USE_REDIS=false) we run Channels with the in-memory layer and Celery
# in eager mode (tasks execute synchronously inside the request), so the
# whole app boots without Redis installed.
# In production / proctoring testing, set USE_REDIS=true and start a worker:
#   celery -A config worker -Q yolo_inference,id_verification -l info
USE_REDIS = os.getenv("USE_REDIS", "false").lower() == "true"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

if USE_REDIS:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        },
    }
    CELERY_BROKER_URL = REDIS_URL
    CELERY_RESULT_BACKEND = REDIS_URL
    CELERY_TASK_ALWAYS_EAGER = False
    # Use Redis as the cache backing django-ratelimit so the limit is shared
    # across workers (the only correct configuration for prod).
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        },
    }
else:
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }
    # No broker — eager mode runs tasks inline. Good for dev / demos.
    CELERY_BROKER_URL = "memory://"
    CELERY_RESULT_BACKEND = "cache+memory://"
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True
    # django-ratelimit warns/errors that LocMem isn't shared across processes.
    # In single-process dev that's fine; silence the system checks so `manage.py
    # check` stays green. The production branch above uses RedisCache.
    SILENCED_SYSTEM_CHECKS = ["django_ratelimit.E003", "django_ratelimit.W001"]

if USE_REDIS:
    CELERY_TASK_ROUTES = {
        "apps.proctoring.tasks.process_proctor_frame": {"queue": "yolo_inference"},
        "apps.proctoring.tasks.verify_id_card": {"queue": "id_verification"},
    }
else:
    CELERY_TASK_ROUTES = {}

CELERY_WORKER_PREFETCH_MULTIPLIER = 1

def _env_number(name, default, cast, min_value, max_value):
    """Parse a numeric env var defensively: bad values fall back to the default
    (with a loud warning) and valid values are clamped into [min, max], so a
    typo'd .env can't crash boot or set an unsafe threshold."""
    import logging

    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "Ignoring invalid %s=%r; using default %s", name, raw, default
        )
        return default
    clamped = max(min_value, min(max_value, value))
    if clamped != value:
        logging.getLogger(__name__).warning(
            "%s=%s out of range [%s, %s]; clamped to %s",
            name, value, min_value, max_value, clamped,
        )
    return clamped


def _env_int(name, default, min_value=1, max_value=1_000_000_000):
    return _env_number(name, default, int, min_value, max_value)


def _env_float(name, default, min_value=0.0, max_value=1.0):
    return _env_number(name, default, float, min_value, max_value)


PROCTORING = {
    "MAX_STRIKES": 5,
    # Default 1s capture: phone/book strikes are single-frame (no sustained timer),
    # so a shorter interval shrinks the blind window. Look-away uses wall-clock
    # time in the task layer, not frame count. Override via PROCTORING_FRAME_INTERVAL.
    "FRAME_INTERVAL_SECONDS": _env_int("PROCTORING_FRAME_INTERVAL", 1, 1, 60),
    # Continuous seconds a student must look away (head turn or iris gaze
    # off-screen) before it counts as a strike. Brief 2-3s glances are
    # tolerated; the episode timer resets the moment they look back.
    "LOOK_AWAY_SECONDS": _env_float("PROCTORING_LOOK_AWAY_SECONDS", 5.5, 1.0, 300.0),
    # Per-strike "clip": how many buffered frames the browser uploads, how often
    # it samples them, and the max accepted size of each frame.
    "CLIP_FRAME_COUNT": _env_int("PROCTORING_CLIP_FRAME_COUNT", 6, 1, 30),
    "CLIP_BUFFER_INTERVAL_MS": _env_int("PROCTORING_CLIP_BUFFER_INTERVAL_MS", 1000, 100, 10_000),
    "CLIP_MAX_FRAME_BYTES": _env_int("PROCTORING_CLIP_MAX_FRAME_BYTES", 300_000, 10_000, 10_000_000),
    # Max accepted size of a single live/ID-verify webcam frame (decoded bytes).
    "MAX_FRAME_BYTES": _env_int("PROCTORING_MAX_FRAME_BYTES", 1_500_000, 100_000, 20_000_000),
    "DISCONNECT_GRACE_SECONDS": 120,
    # Re-verify the student's face before they may *continue* an already-started
    # exam (reopened tab, "Continue Exam", refresh). Closes the "verify, then
    # hand the laptop to someone else" gap. The immediate reload right after a
    # pass is not treated as a resume: any take_exam load within
    # REVERIFY_GRACE_SECONDS of the admission timestamp skips re-verification.
    "REVERIFY_ON_RESUME": os.getenv("PROCTORING_REVERIFY_ON_RESUME", "true").lower() == "true",
    "REVERIFY_GRACE_SECONDS": _env_int("PROCTORING_REVERIFY_GRACE_SECONDS", 25, 5, 600),
    "ID_CONFIDENCE_THRESHOLD": 0.85,
    # ArcFace cosine similarity (ml/face_id). Same-person pairs typically
    # score >= 0.5, different people < 0.2; 0.35 balances impostor rejection
    # against webcam lighting/angle variance.
    "FACE_ID_MATCH_THRESHOLD": _env_float("PROCTORING_FACE_ID_MATCH_THRESHOLD", 0.35, 0.05, 0.95),
    # Two tries: the automatic first frame, then one student-triggered retry.
    # A second failure permanently fails ID verification for the attempt.
    # Only conclusive comparisons count — see MAX_ID_VERIFICATION_FAULTS.
    "MAX_ID_VERIFICATION_ATTEMPTS": 2,
    # Rounds where no comparison was possible (no face in frame, model or
    # reference face unavailable). These are system/environment problems, so
    # they are retried rather than counted against the student.
    "MAX_ID_VERIFICATION_FAULTS": _env_int("PROCTORING_MAX_ID_VERIFICATION_FAULTS", 3, 1, 20),
    # Once the fault budget is spent, admit the student and flag the session
    # for staff review. Set false to keep them retrying instead — stricter, but
    # a missing reference face then locks a student out of their own exam.
    "ADMIT_WHEN_UNVERIFIABLE": os.getenv(
        "PROCTORING_ADMIT_WHEN_UNVERIFIABLE", "true"
    ).lower() == "true",
    "VIOLATION_COOLDOWN_SECONDS": 10,
    # ML pipeline (Phase 3). Set USE_MOCK_ML=true to skip Torch/Ultralytics locally.
    "USE_MOCK_ML": os.getenv("PROCTORING_USE_MOCK_ML", "false").lower() == "true",
    "YOLO_WEIGHTS": os.getenv("PROCTORING_YOLO_WEIGHTS", "yolov5nu.pt"),
    "POSE_WEIGHTS": os.getenv("PROCTORING_POSE_WEIGHTS", "yolov8n-pose.pt"),
    "YOLO_CONFIDENCE": _env_float("PROCTORING_YOLO_CONFIDENCE", 0.40, 0.05, 0.99),
    "POSE_CONFIDENCE": _env_float("PROCTORING_POSE_CONFIDENCE", 0.50, 0.05, 0.99),
    "YOLO_IMG_SIZE": _env_int("PROCTORING_YOLO_IMG_SIZE", 416, 160, 1920),
    # COCO ids YOLOv5 may report. Violation-bearing: phone(67), book(73),
    # laptop(63)/keyboard(66) (notes when near a person). Contextual/benign,
    # detected for audit + hard-negative training but no strike: tv(62),
    # mouse(64), remote(65). See ml/yolo_service/detector.py COCO_NAMES.
    "YOLO_CLASSES": [0, 62, 63, 64, 65, 66, 67, 73],
    "ABSENT_CONSECUTIVE_FRAMES": _env_int("PROCTORING_ABSENT_FRAMES", 2, 1, 60),
    "MULTIPLE_PERSON_MIN": 2,
    # Consecutive frames that must show 2+ persons before a multiple-person
    # strike fires. Damps single-frame false positives (e.g. a raised phone
    # splitting one body into two YOLO person boxes).
    "MULTIPLE_PERSON_CONSECUTIVE_FRAMES": _env_int("PROCTORING_MULTI_PERSON_FRAMES", 2, 1, 60),
    "HEAD_TURN_NOSE_OFFSET_RATIO": _env_float("PROCTORING_HEAD_TURN_RATIO", 0.15, 0.01, 1.0),
    # Iris gaze (MediaPipe FaceMesh, ml/gaze_service). Detects eyes tracking
    # off-screen while the head stays forward; fused with the pose head-turn
    # signal into the same sustained LOOK_AWAY_SECONDS rule. Ratios: ~0.5 is a
    # centered gaze; horizontal outside [MIN, MAX] or vertical above V_MAX
    # counts as off-screen. Tune with the ml/lab tool before tightening.
    "USE_IRIS_GAZE": os.getenv("PROCTORING_USE_IRIS_GAZE", "true").lower() == "true",
    "FACE_LANDMARKER_MODEL": os.getenv(
        "PROCTORING_FACE_LANDMARKER_MODEL", "face_landmarker.task"
    ),
    "IRIS_H_RATIO_MIN": _env_float("PROCTORING_IRIS_H_RATIO_MIN", 0.25, 0.0, 0.5),
    "IRIS_H_RATIO_MAX": _env_float("PROCTORING_IRIS_H_RATIO_MAX", 0.75, 0.5, 1.0),
    "IRIS_V_RATIO_MAX": _env_float("PROCTORING_IRIS_V_RATIO_MAX", 0.80, 0.5, 1.5),
    "SNAPSHOT_JPEG_QUALITY": 72,
}
