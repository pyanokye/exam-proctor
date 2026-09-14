import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from apps.exams.models import ExamAttempt
from apps.exams.permissions import user_can_view_attempts


class ProctoringConsumer(AsyncJsonWebsocketConsumer):
    LIVE_STATUSES = (
        ExamAttempt.Status.PENDING_ID,
        ExamAttempt.Status.IN_PROGRESS,
        ExamAttempt.Status.PAUSED,
    )

    async def connect(self):
        self.attempt_id = self.scope["url_route"]["kwargs"]["attempt_id"]
        self.group_name = f"proctoring_{self.attempt_id}"
        self.is_student = False

        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return

        # Ownership check: the connected user must be the student sitting this
        # attempt, or the teacher who authored/owns the exam (a proctor watching
        # the session). Admins are excluded — live frames are student work.
        allowed = await self._user_can_view_attempt(user)
        if not allowed:
            await self.close(code=4403)
            return

        # Students may only hold a socket while the attempt is still live.
        # Teachers may observe ended sessions for audit.
        self.is_student = bool(getattr(user, "is_student_user", False))
        if self.is_student:
            live = await self._attempt_is_live()
            if not live:
                await self.close(code=4403)
                return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # If the student is reconnecting to a disconnect-paused attempt, resume it.
        resumed = False
        if self.is_student:
            resumed = await self._resume_if_disconnect_paused()

        await self.send_json(
            {"type": "connected", "attempt_id": self.attempt_id, "resumed": resumed}
        )

    @database_sync_to_async
    def _user_can_view_attempt(self, user):
        try:
            attempt = ExamAttempt.objects.select_related(
                "exam__course__teacher__user"
            ).get(pk=self.attempt_id)
        except ExamAttempt.DoesNotExist:
            return False
        if getattr(user, "is_student_user", False):
            return attempt.student_id == user.id
        return user_can_view_attempts(user, attempt.exam)

    @database_sync_to_async
    def _attempt_is_live(self):
        try:
            status = ExamAttempt.objects.values_list("status", flat=True).get(
                pk=self.attempt_id
            )
        except ExamAttempt.DoesNotExist:
            return False
        return status in self.LIVE_STATUSES

    @database_sync_to_async
    def _pause_on_disconnect(self):
        try:
            attempt = ExamAttempt.objects.get(pk=self.attempt_id)
        except ExamAttempt.DoesNotExist:
            return
        if attempt.status != ExamAttempt.Status.IN_PROGRESS:
            return
        attempt.status = ExamAttempt.Status.PAUSED
        attempt.timer_paused_at = timezone.now()
        attempt.pause_reason = ExamAttempt.PauseReason.DISCONNECT
        attempt.save(
            update_fields=["status", "timer_paused_at", "pause_reason"]
        )

    @database_sync_to_async
    def _resume_if_disconnect_paused(self):
        try:
            attempt = ExamAttempt.objects.select_related("exam").get(pk=self.attempt_id)
        except ExamAttempt.DoesNotExist:
            return False
        if attempt.status != ExamAttempt.Status.PAUSED:
            return False
        if attempt.pause_reason != ExamAttempt.PauseReason.DISCONNECT:
            # Don't override admin freeze pauses.
            return False
        # If they were offline longer than the grace, the attempt expires.
        if attempt.timer_paused_at:
            offline = (timezone.now() - attempt.timer_paused_at).total_seconds()
            if offline > (attempt.disconnect_grace_seconds or 0):
                attempt.status = ExamAttempt.Status.EXPIRED
                attempt.ended_at = timezone.now()
                attempt.save(update_fields=["status", "ended_at"])
                from apps.exams.models import grade_attempt

                grade_attempt(attempt)
                return False
        attempt.status = ExamAttempt.Status.IN_PROGRESS
        attempt.timer_paused_at = None
        attempt.pause_reason = ""
        attempt.save(
            update_fields=["status", "timer_paused_at", "pause_reason"]
        )
        return True

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        # When the student tab closes, pause the attempt so the timer stops
        # advancing. Reconnecting (or admin unfreeze) flips it back.
        if getattr(self, "is_student", False):
            await self._pause_on_disconnect()

    async def receive_json(self, content):
        if content.get("type") == "heartbeat":
            await self._handle_heartbeat()

    async def _handle_heartbeat(self):
        from apps.proctoring.models import ProctoringSession

        try:
            session = await self._get_session()
            session.last_heartbeat_at = timezone.now()
            await self._save_session(session)
            await self.send_json({"type": "heartbeat", "ok": True})
        except ProctoringSession.DoesNotExist:
            await self.send_json({"type": "heartbeat", "ok": False})

    async def proctoring_event(self, event):
        await self.send_json(event["payload"])

    async def _get_session(self):
        from channels.db import database_sync_to_async
        from apps.proctoring.models import ProctoringSession

        @database_sync_to_async
        def fetch():
            return ProctoringSession.objects.select_related("attempt").get(
                attempt_id=self.attempt_id
            )

        return await fetch()

    async def _save_session(self, session):
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def save():
            session.save(update_fields=["last_heartbeat_at"])
            # NOTE: Missed-heartbeat detection used to live here but could never
            # fire (we just bumped last_heartbeat_at). Disconnect-pause now
            # lives in ProctoringConsumer.disconnect().

        await save()
