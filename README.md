# Exam Proctor

Django-based online examination platform with AI proctoring, question banks, admin approval workflows, and teacher audit tools.

## Requirements

- Python 3.11+
- Redis (for Celery + Channels in production-like proctoring mode)
- Tesseract OCR (registration ID validation): `brew install tesseract` on macOS
- `insightface` + `onnxruntime` (in requirements.txt) for face identity verification; the `buffalo_l` model pack (~280MB) auto-downloads to `~/.insightface` on first use

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # if present; otherwise set env vars below
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Proctoring / ML

Real computer-vision proctoring uses **YOLOv5n** (object detection) and **YOLOv8n-pose** (skeleton overlay) via Ultralytics on **CPU**.

Environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `USE_REDIS` | `false` | `true` for async Celery + Redis Channels |
| `PROCTORING_USE_MOCK_ML` | `false` | `true` skips Torch/Ultralytics (dev without ML deps) |
| `PROCTORING_YOLO_WEIGHTS` | `yolov5n.pt` | Object detection weights |
| `PROCTORING_POSE_WEIGHTS` | `yolov8n-pose.pt` | Pose model weights |
| `PROCTORING_MAX_ID_VERIFICATION_FAULTS` | `3` | Rounds where no face comparison was possible before the student is admitted and flagged |
| `PROCTORING_ADMIT_WHEN_UNVERIFIABLE` | `true` | `false` keeps unverifiable students retrying instead of admitting them |

`manage.py runserver` serves WebSockets because `daphne` is installed and listed
first in `INSTALLED_APPS`; without it Django runs WSGI only and every `/ws/`
request 404s.

### Identity check troubleshooting

If the pre-exam check reports that a face cannot be matched, confirm the student
still has a profile with a reference face:

```bash
python manage.py repair_student_profiles --dry-run
python manage.py repair_student_profiles --user <username> --student-id <id>
```

The registration scan is stored on the user, so a profile deleted in Django admin
can be rebuilt from it.

### Start Redis-backed worker

Terminal 1 — Django (ASGI for WebSockets):

```bash
python manage.py runserver
```

Terminal 2 — Celery worker (loads ML models once at startup):

```bash
export USE_REDIS=true
celery -A config worker -Q yolo_inference,id_verification -c 1 -l info
```

Terminal 3 — Redis (if not already running):

```bash
redis-server
```

On first inference, Ultralytics auto-downloads model weights.

## Tests

```bash
PROCTORING_USE_MOCK_ML=true python manage.py test apps.proctoring.tests
```

## Documentation

- [docs/PROJECT_DOCUMENTATION.md](docs/PROJECT_DOCUMENTATION.md) — **Full project report** (Chapters 1–5, KNUST template)
- [docs/KNUST_FORMATTING_GUIDE.md](docs/KNUST_FORMATTING_GUIDE.md) — Submission formatting (fonts, spacing, margins)
- [docs/THESIS_CHAPTERS_1_AND_2.md](docs/THESIS_CHAPTERS_1_AND_2.md) — Standalone literature chapters (Harvard refs)
- [docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md) — architecture and API reference
- [docs/PLAN.md](docs/PLAN.md) — UI conventions
