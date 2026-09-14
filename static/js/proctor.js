(function () {
    "use strict";

    const examApp = document.getElementById("exam-app");
    if (!examApp) return;

    const FRAME_INTERVAL = parseInt(examApp.dataset.frameInterval || "4000", 10) || 4000;
    const HEARTBEAT_INTERVAL = 10000;
    const STATUS_POLL_INTERVAL = 5000;
    const AUTOSAVE_INTERVAL = 12000;
    // Rolling clip buffer config (the per-strike frame burst sent to teachers).
    const CLIP_URL = examApp.dataset.clipUrl || null;
    const CLIP_FRAME_COUNT = parseInt(examApp.dataset.clipFrameCount || "6", 10) || 6;
    const CLIP_BUFFER_INTERVAL = parseInt(examApp.dataset.clipBufferInterval || "1000", 10) || 1000;

    const STATUS_URL = examApp.dataset.statusUrl || null;
    const AUTOSAVE_URL = examApp.dataset.autosaveUrl || null;
    const RESUME_URL = examApp.dataset.resumeUrl || null;
    const ID_VERIFY_URL = examApp.dataset.idVerifyUrl || null;
    const INITIAL_ID_STATUS = (examApp.dataset.idVerificationStatus || "pending").toLowerCase();
    const ATTEMPT_ID_VALUE = examApp.dataset.attemptId;
    const STRICTNESS = (examApp.dataset.strictnessLevel || "level_1").toLowerCase();
    const STRICT_NONE = STRICTNESS === "none";
    const STRICT_LEVEL1 = STRICTNESS === "level_1";
    // Lockdown event types are reported by lockdown.js itself and already
    // surface a strike dialog from its JSON response. Server WS warnings for
    // these types would just duplicate the UI, so we ignore them here.
    const LOCKDOWN_WS_TYPES = new Set([
        "tab_switch",
        "visibility_hidden",
        "focus_lost",
        "exit_fullscreen",
    ]);

    let stream = null;
    let ws = null;
    let serverRemaining = parseInt(examApp.dataset.duration || "0", 10) || 0;
    let lastServerSync = Date.now();
    let pendingAutosave = false;

    async function initWebcam() {
        stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        ["webcam-exam", "dashboard-webcam", "id-check-video"].forEach((id) => {
            const video = document.getElementById(id);
            if (video) video.srcObject = stream;
        });
        watchStreamHealth();
    }

    // --- Identity check panel (pre-exam overlay) -----------------------------
    // Mirrors the verification lifecycle into the visible panel so the student
    // sees the check happen: starting -> verifying -> passed/failed/error.
    // A minimum "verifying" display time keeps the step perceivable even when
    // eager-mode verification resolves in under a second.
    const MIN_VERIFY_DISPLAY_MS = 2200;
    let verifyStateShownAt = 0;

    function setIdCheckState(state, badgeText) {
        const panel = document.getElementById("id-check-panel");
        if (!panel) return;
        panel.dataset.state = state;
        if (state === "verifying" && !verifyStateShownAt) verifyStateShownAt = Date.now();
        const badge = document.getElementById("id-check-badge");
        if (badge && badgeText) badge.textContent = badgeText;
    }

    // --- Camera-loss recovery ----------------------------------------------
    // If the webcam track ends mid-exam (unplugged, OS revoked permission),
    // proctoring frames silently stop. Surface it to the student and keep
    // trying to re-acquire the camera so monitoring resumes.
    let cameraLost = false;
    let cameraRetryTimer = null;

    function watchStreamHealth() {
        if (!stream) return;
        stream.getVideoTracks().forEach((track) => {
            track.addEventListener("ended", handleCameraLoss);
        });
    }

    function setCameraLostUI(lost) {
        const tag = document.getElementById("face-status-tag");
        if (tag) {
            if (lost) {
                tag.dataset.status = "failed";
                tag.className = "face-verified-tag face-status-tag--failed";
                tag.textContent = "CAMERA OFF — RECONNECT";
            } else {
                setFaceStatus(idVerified ? "passed" : "pending");
            }
        }
    }

    function handleCameraLoss() {
        if (cameraLost) return;
        cameraLost = true;
        setCameraLostUI(true);
        if (window.strikeAlerts && typeof window.strikeAlerts.show === "function") {
            window.strikeAlerts.show({
                message: "Your camera has stopped. Reconnect your webcam now — proctoring is paused and continued camera loss may end the exam.",
                violationType: "camera_lost",
                info: true,
            });
        }
        if (cameraRetryTimer) return;
        cameraRetryTimer = setInterval(async () => {
            try {
                await initWebcam();
                cameraLost = false;
                clearInterval(cameraRetryTimer);
                cameraRetryTimer = null;
                setCameraLostUI(false);
            } catch (err) {
                // Camera still unavailable — keep retrying.
            }
        }, 4000);
    }

    function captureFrame() {
        const video =
            document.getElementById("id-check-video") ||
            document.getElementById("webcam-exam");
        if (!video || !video.videoWidth) return null;
        const canvas = document.createElement("canvas");
        canvas.width = video.videoWidth || 320;
        canvas.height = video.videoHeight || 240;
        canvas.getContext("2d").drawImage(video, 0, 0);
        return canvas.toDataURL("image/jpeg", 0.7);
    }

    // --- Rolling clip buffer ------------------------------------------------
    // We keep the last CLIP_FRAME_COUNT downscaled JPEGs so that when a strike
    // is reported (which happens a moment AFTER the act, since detection is
    // async on the server) we can upload the lead-up the teacher needs to
    // review. Frames are small/low-quality to keep the payload light.
    const clipBuffer = [];
    const clipsUploaded = new Set();

    function captureBufferFrame() {
        const video = document.getElementById("webcam-exam");
        if (!video || !video.videoWidth) return null;
        const maxW = 320;
        const scale = Math.min(1, maxW / video.videoWidth);
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(video.videoWidth * scale);
        canvas.height = Math.round(video.videoHeight * scale);
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        return canvas.toDataURL("image/jpeg", 0.5);
    }

    function sampleClipFrame() {
        const frame = captureBufferFrame();
        if (!frame) return;
        clipBuffer.push(frame);
        while (clipBuffer.length > CLIP_FRAME_COUNT) clipBuffer.shift();
    }

    async function uploadClip(violationId) {
        if (!CLIP_URL || violationId == null) return;
        if (clipsUploaded.has(violationId)) return;
        clipsUploaded.add(violationId);
        const frames = clipBuffer.slice();
        if (!frames.length) return;
        try {
            await fetch(CLIP_URL, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": CSRF_TOKEN,
                },
                body: JSON.stringify({
                    attempt_id: ATTEMPT_ID_VALUE,
                    violation_id: violationId,
                    frames: frames,
                }),
            });
        } catch (err) {
            // Best effort — the strike itself is already recorded server-side.
            clipsUploaded.delete(violationId);
        }
    }

    let idVerified = INITIAL_ID_STATUS === "passed" || INITIAL_ID_STATUS === "unverified";
    let idVerifyResolve = null;
    let idVerifyReject = null;
    const idVerifyReady = new Promise((resolve, reject) => {
        idVerifyResolve = resolve;
        idVerifyReject = reject;
        if (idVerified) resolve();
    });

    function markIdPassed(badgeText) {
        if (idVerified) return;
        idVerified = true;
        // Hold the "verifying" state on screen briefly so the student sees the
        // check happen before the exam loads (eager mode can pass in <1s).
        const elapsed = verifyStateShownAt ? Date.now() - verifyStateShownAt : MIN_VERIFY_DISPLAY_MS;
        const delay = Math.max(0, MIN_VERIFY_DISPLAY_MS - elapsed);
        setTimeout(() => {
            examApp.dataset.pendingId = "false";
            setFaceStatus("passed");
            setIdCheckState("passed", badgeText || "IDENTITY CONFIRMED");
            document.dispatchEvent(new CustomEvent("id-verification:passed"));
            if (idVerifyResolve) {
                idVerifyResolve();
                idVerifyResolve = null;
                idVerifyReject = null;
            }
        }, delay);
    }

    function markIdFailed() {
        setFaceStatus("failed");
        setIdCheckState("failed", "VERIFICATION FAILED");
        document.dispatchEvent(new CustomEvent("id-verification:failed"));
        if (idVerifyReject) {
            idVerifyReject(new Error("verification failed"));
            idVerifyResolve = null;
            idVerifyReject = null;
        }
    }

    function setIdStatusText(text, ready) {
        const statusEl = document.getElementById("exam-begin-id-status");
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.classList.toggle("exam-begin-id-status--ready", !!ready);
    }

    function showIdCheckAction(which) {
        const actions = document.getElementById("id-check-actions");
        const retryBtn = document.getElementById("id-check-retry-btn");
        const backBtn = document.getElementById("id-check-back-btn");
        if (!actions) return;
        actions.hidden = false;
        if (retryBtn) {
            retryBtn.hidden = which !== "retry";
            retryBtn.disabled = false;
        }
        if (backBtn) backBtn.hidden = which !== "back";
        const beginBtn = document.getElementById("exam-begin-btn");
        if (beginBtn && which) beginBtn.hidden = true;
    }

    function hideIdCheckActions() {
        const actions = document.getElementById("id-check-actions");
        const retryBtn = document.getElementById("id-check-retry-btn");
        const backBtn = document.getElementById("id-check-back-btn");
        if (actions) actions.hidden = true;
        if (retryBtn) retryBtn.hidden = true;
        if (backBtn) backBtn.hidden = true;
        const beginBtn = document.getElementById("exam-begin-btn");
        if (beginBtn) beginBtn.hidden = false;
    }

    function showIdVerifyToast(message) {
        const toast = document.getElementById("id-verify-toast");
        const msg = document.getElementById("id-verify-toast-message");
        if (msg && message) msg.textContent = message;
        if (toast) toast.hidden = false;
    }

    function goToDashboard() {
        const url = examApp.dataset.dashboardUrl || "/dashboard/";
        try {
            history.replaceState(null, "", url);
        } catch (err) {
            /* ignore */
        }
        window.location.replace(url);
    }

    function leaveToResult() {
        const url = examApp.dataset.resultUrl;
        if (!url) return;
        try {
            history.replaceState(null, "", url);
        } catch (err) {
            /* ignore */
        }
        window.location.replace(url);
    }

    function scrubExamPaper() {
        const form = document.getElementById("exam-form");
        if (!form) return;
        form.querySelectorAll("input, textarea, select, button").forEach((el) => {
            el.disabled = true;
        });
        form.querySelectorAll(".question-text, .question-body, .exam-question").forEach((el) => {
            el.textContent = "";
        });
    }

    async function guardAgainstEndedOrBfcache(persisted) {
        // On bfcache restore, always re-check with the server. On normal load,
        // a quick status poll catches ended attempts if the user navigated Back
        // to a still-live take URL that somehow wasn't redirected.
        if (!STATUS_URL) {
            if (persisted && examApp.dataset.resultUrl) leaveToResult();
            return;
        }
        try {
            const res = await fetch(STATUS_URL, { credentials: "same-origin", cache: "no-store" });
            if (!res.ok) return;
            const data = await res.json();
            const status = (data.attempt_status || "").toLowerCase();
            if (
                status === "submitted" ||
                status === "terminated" ||
                status === "expired"
            ) {
                scrubExamPaper();
                leaveToResult();
            } else if (persisted && idVerified === false && INITIAL_ID_STATUS === "failed") {
                goToDashboard();
            }
        } catch (err) {
            if (persisted && examApp.dataset.resultUrl) leaveToResult();
        }
    }

    // Why a round did not confirm the student. Only "no_match" is a verdict on
    // the person in front of the camera; the others are the check itself not
    // happening, so they are worded (and retried) differently.
    const ID_RETRY_COPY = {
        no_match: {
            badge: "FACE NOT RECOGNIZED",
            text: "We could not match your face. Face the camera in good lighting, then retry.",
        },
        no_face: {
            badge: "NO FACE DETECTED",
            text: "We can't see your face. Move into the frame, remove hats or masks, and make sure the room is lit.",
        },
        unavailable: {
            badge: "CHECK UNAVAILABLE",
            text: "The identity check is temporarily unavailable. Trying again…",
        },
    };

    function offerIdRetry(reason) {
        const copy = ID_RETRY_COPY[reason] || ID_RETRY_COPY.no_match;
        setFaceStatus("pending");
        setIdCheckState("failed", copy.badge);
        setIdStatusText(copy.text);
        showIdCheckAction("retry");
        document.dispatchEvent(new CustomEvent("id-verification:retry-available"));
    }

    function announceAutoRetry(reason) {
        const copy = ID_RETRY_COPY[reason] || ID_RETRY_COPY.unavailable;
        setFaceStatus("pending");
        setIdCheckState("verifying", copy.badge);
        setIdStatusText(copy.text);
        hideIdCheckActions();
    }

    // Admitted without a face match because the system could not perform one.
    // The exam proceeds — the session is flagged for staff review server-side.
    function markIdUnverified() {
        hideIdCheckActions();
        setIdStatusText(
            "We could not complete the face check. You may start the exam; this session is flagged for review."
        );
        markIdPassed("IDENTITY NOT CONFIRMED");
    }

    function offerIdGoBack() {
        markIdFailed();
        scrubExamPaper();
        setIdStatusText(
            "Identity verification failed. You can return to the dashboard and try again later."
        );
        showIdCheckAction("back");
        showIdVerifyToast(
            "Identity verification failed. Return to the dashboard and try again later."
        );
        // Replace the take URL so Back from the dashboard cannot reopen this attempt.
        const dash = examApp.dataset.dashboardUrl || "/dashboard/";
        try {
            history.replaceState(null, "", dash);
        } catch (err) {
            /* ignore */
        }
    }

    async function waitForVideoReady(maxMs = 15000) {
        const start = Date.now();
        while (Date.now() - start < maxMs) {
            const video =
                document.getElementById("id-check-video") ||
                document.getElementById("webcam-exam");
            if (video && video.videoWidth > 0) return true;
            await new Promise((r) => setTimeout(r, 200));
        }
        return false;
    }

    async function prepareForExam() {
        if (STRICT_NONE) return;
        try {
            await initWebcam();
            connectWebSocket();
            await waitForVideoReady();
            wireIdCheckActions();
            if (idVerified) {
                setFaceStatus("passed");
                setIdCheckState("passed", "IDENTITY CONFIRMED");
                return;
            }
            setIdCheckState("verifying", "VERIFYING YOUR IDENTITY…");
            await runIdVerification();
        } catch (err) {
            console.warn("Webcam unavailable:", err);
            setIdCheckState("error", "CAMERA UNAVAILABLE");
            document.dispatchEvent(new CustomEvent("id-verification:camera-error"));
        }
    }

    // Exposed so lockdown.js can upload clips, sync strikes, and gate the begin button.
    window.proctor = {
        uploadClip,
        updateStrikes,
        whenIdVerified: () => (idVerified ? Promise.resolve() : idVerifyReady),
        isIdVerified: () => idVerified,
    };

    let heartbeatTimer = null;
    let wsReconnectPending = false;

    function connectWebSocket() {
        if (!ATTEMPT_ID_VALUE) return;
        wsReconnectPending = false;
        const protocol = window.location.protocol === "https:" ? "wss" : "ws";
        ws = new WebSocket(`${protocol}://${window.location.host}/ws/proctoring/${ATTEMPT_ID_VALUE}/`);
        ws.onmessage = (event) => {
            let data = null;
            try {
                data = JSON.parse(event.data);
            } catch (err) {
                return; // malformed payload must not kill the handler
            }
            try {
                handleWsEvent(data);
            } catch (err) {
                console.warn("WS event handling failed:", err);
            }
        };
        ws.onclose = () => {
            // Reconnect quickly so the server can resume the disconnect-paused
            // attempt. Guarded so overlapping close events (or a close during
            // an in-flight reconnect) can't stack multiple sockets.
            if (wsReconnectPending) return;
            wsReconnectPending = true;
            setTimeout(connectWebSocket, 2500);
        };
        // One shared heartbeat for the lifetime of the page — re-creating it
        // per connection leaked an interval on every reconnect.
        if (!heartbeatTimer) {
            heartbeatTimer = setInterval(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: "heartbeat" }));
                }
            }, HEARTBEAT_INTERVAL);
        }
    }

    function updateStrikes(strike, max) {
        const el = document.getElementById("strikes");
        if (!el) return;
        const strikeNum = parseInt(strike, 10) || 0;
        const maxNum = parseInt(max, 10) || parseInt(el.dataset.maxStrikes || "5", 10);
        const remaining = Math.max(0, maxNum - strikeNum);
        el.textContent = `Violations Remaining: ${remaining}/${maxNum}`;
        el.dataset.currentStrikes = String(strikeNum);
        el.dataset.maxStrikes = String(maxNum);
    }

    function setFaceStatus(status) {
        const tag = document.getElementById("face-status-tag");
        if (!tag) return;
        if (tag.dataset.status === status) return;
        tag.dataset.status = status;
        tag.className = `face-verified-tag face-status-tag--${status}`;
        const label = {
            passed: "FACE VERIFIED",
            failed: "VERIFICATION FAILED",
            pending: "VERIFYING…",
        }[status] || "VERIFYING…";
        tag.textContent = label;
    }

    let idVerifySubmitting = false;
    let idRoundActive = false;
    let idVerifyPollTimer = null;

    function needsIdVerification(status) {
        const s = (status || "").toLowerCase();
        return s === "pending" || s === "" || s === "retry";
    }

    async function fetchSessionStatus() {
        if (!STATUS_URL) return null;
        try {
            const res = await fetch(STATUS_URL, { credentials: "same-origin" });
            if (!res.ok) return null;
            return await res.json();
        } catch (err) {
            return null;
        }
    }

    function handleIdVerificationResolved(status, reason) {
        if (status === "passed") {
            hideIdCheckActions();
            markIdPassed();
            return true;
        }
        if (status === "unverified") {
            markIdUnverified();
            return true;
        }
        if (status === "failed") {
            offerIdGoBack();
            return true;
        }
        if (status === "retry") {
            offerIdRetry(reason);
            return true;
        }
        return false;
    }

    // A one-off, higher-quality still than the proctoring loop uses: this is
    // the frame the face embedding is built from.
    function captureIdFrame() {
        const video =
            document.getElementById("id-check-video") ||
            document.getElementById("webcam-exam");
        if (!video || !video.videoWidth) return null;
        const canvas = document.createElement("canvas");
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext("2d").drawImage(video, 0, 0);
        return canvas.toDataURL("image/jpeg", 0.92);
    }

    async function submitIdVerifyFrame() {
        if (!ID_VERIFY_URL || idVerifySubmitting) return null;
        await waitForVideoReady(8000);
        const frame = captureIdFrame();
        // No usable frame is a camera problem, not a failed identity check.
        if (!frame) return { id_verification_status: "retry", id_verification_reason: "no_face" };
        idVerifySubmitting = true;
        try {
            const res = await fetch(ID_VERIFY_URL, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": CSRF_TOKEN,
                },
                body: JSON.stringify({
                    attempt_id: ATTEMPT_ID_VALUE,
                    frame_base64: frame,
                }),
            });
            const payload = await res.json().catch(() => ({}));
            if (res.ok) return payload;
            // A rejected frame (400) or transport error still leaves the
            // student unverified — retry rather than stalling on the spinner.
            if (payload.id_verification_status) return payload;
            return {
                id_verification_status: "retry",
                id_verification_reason: res.status === 400 ? "no_face" : "unavailable",
            };
        } catch (err) {
            return { id_verification_status: "retry", id_verification_reason: "unavailable" };
        } finally {
            idVerifySubmitting = false;
        }
    }

    function startIdVerificationPolling() {
        if (idVerifyPollTimer) return;
        idVerifyPollTimer = setInterval(async () => {
            const data = await fetchSessionStatus();
            if (!data) return;
            if (data.id_verification_status) {
                if (
                    handleIdVerificationResolved(
                        data.id_verification_status,
                        data.id_verification_reason
                    )
                ) {
                    clearInterval(idVerifyPollTimer);
                    idVerifyPollTimer = null;
                }
            }
        }, 2000);
    }

    function stopIdVerificationPolling() {
        if (!idVerifyPollTimer) return;
        clearInterval(idVerifyPollTimer);
        idVerifyPollTimer = null;
    }

    const RESOLVED_ID_STATUSES = new Set(["passed", "failed", "retry", "unverified"]);
    // Faults (camera showed no face, model unreachable) are retried for the
    // student automatically; only a real mismatch needs a decision from them.
    const AUTO_RETRY_REASONS = new Set(["no_face", "unavailable"]);
    const MAX_AUTO_ID_RETRIES = 3;
    const AUTO_RETRY_DELAY_MS = 1800;

    async function waitForIdVerifyOutcome(timeoutMs = 20000) {
        const start = Date.now();
        while (Date.now() - start < timeoutMs) {
            const data = await fetchSessionStatus();
            const status = (data?.id_verification_status || "").toLowerCase();
            if (RESOLVED_ID_STATUSES.has(status)) {
                return { status, reason: data.id_verification_reason };
            }
            await new Promise((r) => setTimeout(r, 800));
        }
        return null;
    }

    async function runSingleIdVerifyRound(autoRetriesLeft = MAX_AUTO_ID_RETRIES) {
        idRoundActive = true;
        try {
            return await idVerifyRound(autoRetriesLeft);
        } finally {
            idRoundActive = false;
        }
    }

    async function idVerifyRound(autoRetriesLeft) {
        hideIdCheckActions();
        setFaceStatus("pending");
        setIdCheckState("verifying", "VERIFYING YOUR IDENTITY…");
        setIdStatusText("Checking your face against your registration scan…");

        let outcome = null;
        const result = await submitIdVerifyFrame();
        const submitted = (result?.id_verification_status || "").toLowerCase();
        if (RESOLVED_ID_STATUSES.has(submitted)) {
            outcome = { status: submitted, reason: result.id_verification_reason };
        } else {
            // Queued worker path (or empty eager response): poll for a decision.
            startIdVerificationPolling();
            outcome = await waitForIdVerifyOutcome();
            stopIdVerificationPolling();
        }

        // No answer at all within the window — the server never got to decide.
        if (!outcome) outcome = { status: "retry", reason: "unavailable" };

        if (
            outcome.status === "retry" &&
            AUTO_RETRY_REASONS.has(outcome.reason) &&
            autoRetriesLeft > 0
        ) {
            announceAutoRetry(outcome.reason);
            await new Promise((r) => setTimeout(r, AUTO_RETRY_DELAY_MS));
            return idVerifyRound(autoRetriesLeft - 1);
        }

        handleIdVerificationResolved(outcome.status, outcome.reason);
        return outcome.status;
    }

    async function runIdVerification() {
        if (STRICT_NONE || !ID_VERIFY_URL) {
            setFaceStatus("passed");
            return;
        }

        const initial = await fetchSessionStatus();
        const status = (initial?.id_verification_status || "").toLowerCase();
        if (status === "passed" || status === "failed" || status === "unverified") {
            handleIdVerificationResolved(status, initial.id_verification_reason);
            return;
        }
        // A mismatch is already on record: a reload must not silently spend the
        // student's last attempt, so wait for them to press Retry.
        if (status === "retry" && initial.id_verification_reason === "no_match") {
            offerIdRetry("no_match");
            return;
        }

        await runSingleIdVerifyRound();
    }

    function wireIdCheckActions() {
        const retryBtn = document.getElementById("id-check-retry-btn");
        const backBtn = document.getElementById("id-check-back-btn");
        if (retryBtn && !retryBtn.dataset.bound) {
            retryBtn.dataset.bound = "1";
            retryBtn.addEventListener("click", async () => {
                retryBtn.disabled = true;
                await runSingleIdVerifyRound();
            });
        }
        if (backBtn && !backBtn.dataset.bound) {
            backBtn.dataset.bound = "1";
            backBtn.addEventListener("click", goToDashboard);
        }
    }

    function handleWsEvent(data) {
        if (data.type === "id_status") {
            const status = data.status;
            // A round is already driving the UI (and will auto-retry faults);
            // let it finish rather than racing it with a duplicate handler.
            if (status === "retry" && idRoundActive) return;
            if (!handleIdVerificationResolved(status, data.reason)) {
                setFaceStatus(status === "passed" ? "passed" : status === "failed" ? "failed" : "pending");
            }
            return;
        }
        if (data.type === "warning") {
            const maxStrikes = data.max_strikes ?? parseInt(examApp.dataset.maxStrikes || "5", 10);
            updateStrikes(data.strike, maxStrikes);
            if (STRICT_LEVEL1 && !LOCKDOWN_WS_TYPES.has(data.violation_type)) {
                if (window.strikeAlerts && typeof window.strikeAlerts.show === "function") {
                    window.strikeAlerts.show({
                        violationId: data.violation_id,
                        message: data.message,
                        strike: data.strike,
                        maxStrikes: maxStrikes,
                        violationType: data.violation_type,
                    });
                }
                uploadClip(data.violation_id);
            }
            if (data.action === "terminate" || data.terminated) {
                const reason = data.message || "Maximum violations exceeded.";
                if (window.lockdown && typeof window.lockdown.endExam === "function") {
                    window.lockdown.endExam("server_terminate", "Exam ended — " + reason);
                }
            }
        }
        if (data.type === "terminate") {
            // Server is the source of truth. Delegate to lockdown's overlay+
            // redirect path so the user sees a consistent end-state.
            const reason = data.reason || "Exam terminated.";
            if (window.lockdown && typeof window.lockdown.endExam === "function") {
                window.lockdown.endExam("server_terminate", "Exam ended — " + reason);
            } else {
                scrubExamPaper();
                leaveToResult();
            }
        }
        if (data.type === "connected" && data.resumed) {
            // Server resumed our paused attempt; nothing to do client-side.
        }
    }

    async function verifyIdentity() {
        return runIdVerification();
    }

    async function postFrame() {
        const frame = captureFrame();
        if (!frame) return;
        try {
            const res = await fetch("/api/v1/proctoring/frame/", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": CSRF_TOKEN,
                },
                body: JSON.stringify({
                    attempt_id: ATTEMPT_ID_VALUE,
                    frame_base64: frame,
                    timestamp: new Date().toISOString(),
                    client_events: [],
                }),
            });
            if (res.ok) {
                const data = await res.json().catch(() => null);
                if (data && data.max_strikes != null) {
                    updateStrikes(data.session_strikes || 0, data.max_strikes);
                }
            }
        } catch (err) {
            // Network blip — the next interval tick retries; status polling
            // covers strike-count sync in the meantime.
        }
    }

    let lastSeenStrikeCount = parseInt(
        document.getElementById("strikes")?.dataset.currentStrikes || "0",
        10
    ) || 0;

    const VIOLATION_MESSAGES = {
        face_obstructed: "do not look away from the screen",
        phone: "put your phone away",
        book: "remove books from view",
        notes: "remove notes and secondary devices from view",
        absent: "stay visible to the camera",
        multiple_faces: "you must be alone during the exam",
        tab_switch: "stay on the exam tab",
        focus_lost: "keep the exam window focused",
        exit_fullscreen: "remain in fullscreen",
    };

    function notifyNewViolations(violations, maxStrikes) {
        if (!Array.isArray(violations) || !window.strikeAlerts) return;
        for (const v of violations) {
            if ((v.strike_number || 0) <= lastSeenStrikeCount) continue;
            window.strikeAlerts.show({
                violationId: v.id,
                message: v.message || `Strike ${v.strike_number}: ${VIOLATION_MESSAGES[v.violation_type] || "follow the exam rules"}`,
                strike: v.strike_number,
                maxStrikes: maxStrikes,
                violationType: v.violation_type,
            });
            if (window.proctor && typeof window.proctor.uploadClip === "function") {
                window.proctor.uploadClip(v.id);
            }
        }
    }

    async function pollStatus() {
        if (!STATUS_URL) return;
        try {
            const res = await fetch(STATUS_URL, { credentials: "same-origin" });
            if (!res.ok) return;
            const data = await res.json();
            if (typeof data.remaining_seconds === "number") {
                serverRemaining = data.remaining_seconds;
                lastServerSync = Date.now();
            }
            if (data.id_verification_status) {
                const idStatus = data.id_verification_status;
                if (idStatus === "passed" || idStatus === "unverified") {
                    if (!idVerified) markIdPassed();
                    else setFaceStatus("passed");
                } else {
                    setFaceStatus(idStatus);
                }
            }
            if (data.max_strikes != null) {
                const strikeCount = data.strike_count || 0;
                if (strikeCount > lastSeenStrikeCount) {
                    notifyNewViolations(data.violations, data.max_strikes);
                    lastSeenStrikeCount = strikeCount;
                }
                updateStrikes(strikeCount, data.max_strikes);
            }
            if (data.attempt_status === "terminated" || data.attempt_status === "expired" || data.attempt_status === "submitted") {
                // Flush any un-synced answers before leaving the page so a
                // server-side termination doesn't drop the student's work.
                try {
                    await autosave();
                } catch (err) { /* best effort */ }
                scrubExamPaper();
                leaveToResult();
            }
        } catch (err) {
            console.warn("Status poll failed:", err);
        }
    }

    function buildAutosaveBody({ withCsrf = false } = {}) {
        const form = document.getElementById("exam-form");
        if (!form) return null;
        const formData = new FormData(form);
        // The token is sent via header for fetch; sendBeacon can't set headers
        // so the unload path must keep it in the body instead.
        formData.delete("csrfmiddlewaretoken");
        const params = new URLSearchParams();
        for (const [k, v] of formData.entries()) {
            if (v == null) continue;
            // Empty text answers ARE sent: clearing a written answer must
            // persist, otherwise the server keeps the stale value.
            params.append(k, v);
        }
        if (withCsrf && [...params.keys()].length > 0) {
            params.append("csrfmiddlewaretoken", CSRF_TOKEN);
        }
        return params;
    }

    async function autosave() {
        if (!AUTOSAVE_URL || pendingAutosave) return;
        const body = buildAutosaveBody();
        if (!body || [...body.keys()].length === 0) return;
        pendingAutosave = true;
        try {
            const res = await fetch(AUTOSAVE_URL, {
                method: "POST",
                headers: {
                    "X-CSRFToken": CSRF_TOKEN,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                credentials: "same-origin",
                body: body.toString(),
            });
            if (!res.ok) {
                console.warn("Autosave rejected with HTTP", res.status);
            }
        } catch (err) {
            // Network blip — next interval will retry.
        } finally {
            pendingAutosave = false;
        }
    }

    function startExamLoop() {
        // The proctor frame loop is only useful when strictness > NONE; at
        // NONE the server ignores frames anyway and we save bandwidth + battery.
        if (!STRICT_NONE) {
            setInterval(() => postFrame(), FRAME_INTERVAL);
            setInterval(() => sampleClipFrame(), CLIP_BUFFER_INTERVAL);
        }
        setInterval(() => pollStatus(), STATUS_POLL_INTERVAL);
        setInterval(() => autosave(), AUTOSAVE_INTERVAL);
        // Save once on unload so a final tab close still captures pending work.
        // sendBeacon can't set the X-CSRFToken header, so the token must ride
        // in the body or Django rejects the request and the save is lost.
        window.addEventListener("beforeunload", () => {
            const body = buildAutosaveBody({ withCsrf: true });
            if (!body || [...body.keys()].length === 0 || !navigator.sendBeacon || !AUTOSAVE_URL) return;
            navigator.sendBeacon(AUTOSAVE_URL, body);
        });
    }

    let timeUpSubmitted = false;

    function startTimer() {
        const el = document.getElementById("timer");
        function render() {
            // Server is the source of truth; we just smooth between polls.
            const driftSeconds = Math.floor((Date.now() - lastServerSync) / 1000);
            let remaining = Math.max(0, serverRemaining - driftSeconds);
            const h = Math.floor(remaining / 3600);
            const m = Math.floor((remaining % 3600) / 60);
            const s = remaining % 60;
            if (el) {
                el.textContent = h > 0
                    ? `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
                    : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
            }
            if (remaining === 0 && !timeUpSubmitted) {
                // Submit exactly once — every subsequent 1s tick also sees 0.
                timeUpSubmitted = true;
                document.getElementById("exam-form")?.submit();
            }
        }
        render();
        setInterval(render, 1000);
    }

    // Frame loop and timer start only after the student clicks Begin on the
    // lockdown overlay. Face verification runs earlier via prepareForExam().
    document.addEventListener("exam:ready", () => {
        if (!ATTEMPT_ID_VALUE) return;
        startExamLoop();
        startTimer();
        pollStatus();
    });

    // On submit, replace the take history entry so Back cannot restore the paper.
    const examForm = document.getElementById("exam-form");
    if (examForm) {
        examForm.addEventListener("submit", () => {
            // Scrub only AFTER the browser has serialized the form for the
            // POST. Disabling inputs synchronously here excluded every field
            // — including the hidden CSRF token — from the submission, so the
            // server rejected it with "CSRF token missing" and the attempt
            // stayed in progress. Form data is captured synchronously right
            // after this handler returns, so a 0ms defer is safe.
            setTimeout(scrubExamPaper, 0);
            const url = examApp.dataset.resultUrl;
            if (url) {
                try {
                    history.replaceState(null, "", url);
                } catch (err) {
                    /* ignore */
                }
            }
        });
    }

    // bfcache / Back: if the take page is restored from memory, re-check status
    // and bounce to results when the attempt has already ended.
    window.addEventListener("pageshow", (event) => {
        guardAgainstEndedOrBfcache(!!event.persisted);
    });
    // Also run once on load in case a stale take tab is refreshed after submit.
    guardAgainstEndedOrBfcache(false);

    if (!STRICT_NONE) {
        prepareForExam();
    }
})();
